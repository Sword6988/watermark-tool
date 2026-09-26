"""加密文件（DLP / 透明加密）探测与解密适配层。

背景：公司内网的加密软件（如天锐绿盾）把文件以密文形式写在磁盘上，工具直接
``Image.open`` / ``fitz.open`` 会失败。本模块负责在**文件被打开之前**把"密文路径"
换成一个"可读的明文路径"，让渲染与批处理链路完全不感知加密这件事。

四条设计铁律（与 ``docs/integration-lddec.md`` 一致，不得违反）：

1. **先探测，再解密** —— 只读取文件头几个字节做魔数比对，非加密文件零额外开销，
   行为与接入前**完全一致**。
2. **不做进程伪装** —— 不改名、不注入、不冒充白名单进程名。内置通道只是让
   **系统自带的** ``cmd.exe`` / ``powershell.exe`` 去读文件：驱动按进程决定是否
   返回明文，没授权时读到的仍是密文，会被判为"解密失败"。也就是说能否解密
   完全由管理员的策略决定，本模块不绕过任何策略。第三方解密器（LDDec）不随
   源码硬编码，由部署脚本放进程序目录后被自动发现。
3. **绝不覆盖、不改写原文件** —— 交给解密器的永远是**临时目录里的密文副本**，
   原文件在整个过程中只被只读打开。
4. **明文只落受控临时目录**，用完立即删除；失败、异常、进程退出都不留残留（
   ``atexit`` 兜底）。

典型调用方是 :meth:`wm.ui.app.App._add_paths` —— "选择文件"按钮与拖拽投放**都**
汇到那里，所以解密只需挂载一处，两条入口行为一致。
"""

from __future__ import annotations

import atexit
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import uuid
from dataclasses import dataclass
from typing import List, Optional, Tuple

#: 天锐绿盾加密文件头（与文件格式无关：PDF / 图片一视同仁，见集成方案文档）。
#: 可用环境变量 ``WM_DLP_MAGIC``（十六进制串，如 ``"88 7d 1c"``）覆盖。
DEFAULT_MAGIC = b"\x88\x7d\x1c"

#: 探测时读取的字节数（够放魔数即可，不碰文件其余内容）
_PROBE_READ_LEN = 8
#: 校验解密产物时读取的字节数（PDF 允许头部有垃圾前缀，要放宽搜索范围）
_VERIFY_READ_LEN = 1024

#: 单个文件的解密超时（秒）。驱动无响应时不能把整个添加过程挂死。
DEFAULT_TIMEOUT = 30.0

#: ``cmd /c type`` 通道的超时。大 PDF 走 type 是逐字节复制，比普通命令慢。
CMDTYPE_TIMEOUT = 60.0

#: PowerShell 通道的超时。冷启动就要 1~2 秒，再留足读大文件的余量。
POWERSHELL_TIMEOUT = 90.0

#: 解析结果状态：未加密 / 已可直接读 / 已解密 / 失败
STATE_PLAIN = "plain"
STATE_DIRECT = "direct"
STATE_DECRYPTED = "decrypted"
STATE_FAILED = "failed"

#: 各类文件的合法文件头（用于判断"解密产物是否真的是个能用的文件"）。
#: PDF 单独处理：PyMuPDF 容忍 ``%PDF`` 之前有垃圾字节。
_SIGNATURES = {
    ".png": (b"\x89PNG\r\n\x1a\n",),
    # JPEG 只校验 SOI（FFD8）：下一个字节理论上总是 FF，但宽松一点更不容易误杀
    ".jpg": (b"\xff\xd8",),
    ".jpeg": (b"\xff\xd8",),
    ".bmp": (b"BM",),
    ".gif": (b"GIF87a", b"GIF89a"),
    ".tif": (b"II*\x00", b"MM\x00*"),
    ".tiff": (b"II*\x00", b"MM\x00*"),
}

#: Windows 下起子进程时不要弹出控制台窗口
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class DecryptError(Exception):
    """解密器不可用 / 调用失败 / 产物不可用。

    消息会**直接展示给用户**（"xxx.png：解密超时"），所以要写成中文短句，
    不带 traceback。
    """


# ---------------------------------------------------------------------------
# 加密探测
# ---------------------------------------------------------------------------

def magic() -> bytes:
    """当前生效的加密魔数（默认 ``88 7D 1C``，可用环境变量覆盖）。"""
    raw = (os.environ.get("WM_DLP_MAGIC") or "").strip().replace(" ", "").replace("0x", "")
    if not raw:
        return DEFAULT_MAGIC
    try:
        return bytes.fromhex(raw)
    except ValueError:
        # 配错了也不能让工具起不来：退回默认值（最多是探测不到，不会误伤）
        return DEFAULT_MAGIC


def is_encrypted(path: str) -> bool:
    """文件头是否命中加密魔数；读不到 / 路径非法时一律返回 ``False``。

    ``False`` 有两种含义："确实没加密"和"读不了" —— 后者随后会在正常打开时报错，
    与接入前一致，所以这里不需要区分。
    """
    tag = magic()
    if not tag:
        return False
    try:
        with open(path, "rb") as handle:
            head = handle.read(max(_PROBE_READ_LEN, len(tag)))
    except OSError:
        return False
    return head[:len(tag)] == tag


def verify_plaintext(path: str) -> Optional[str]:
    """校验解密产物是否可用；可用返回 ``None``，否则返回**给用户看的原因**。

    两道校验缺一不可：
      * 仍带加密魔数 —— 解密器没能拿到明文（常见原因：当前用户对该文件无授权）；
      * 文件头与扩展名不符 —— 解密出错或文件本身已损坏，此时绝不能把它当成
        正常文件交给后续流程（否则用户看到的会是"预览失败"而不是"解密失败"）。
    """
    try:
        with open(path, "rb") as handle:
            head = handle.read(_VERIFY_READ_LEN)
    except OSError as exc:
        return f"无法读取解密产物（{exc}）"
    if not head:
        return "解密产物为空（解密器没有输出任何内容）"
    if is_encrypted(path):
        return "解密后仍是密文（当前环境可能没有解密授权）"
    ext = os.path.splitext(path)[1].lower()
    if ext == ".pdf":
        # %PDF 之前允许有垃圾前缀（部分 PDF 与部分解密器都这样）
        if b"%PDF" not in head:
            return "解密产物不是有效的 PDF（文件头缺少 %PDF）"
        return None
    if ext == ".webp":
        if not (head.startswith(b"RIFF") and head[8:12] == b"WEBP"):
            return "解密产物不是有效的 WEBP 文件（文件头损坏）"
        return None
    signatures = _SIGNATURES.get(ext)
    if signatures is None:
        return None  # 未知类型：交给后续打开阶段判定，不在适配层误杀
    if not any(head.startswith(sig) for sig in signatures):
        return f"解密产物不是有效的 {ext.lstrip('.').upper()} 文件（文件头损坏）"
    return None


# ---------------------------------------------------------------------------
# 明文临时目录（受控、可清理）
# ---------------------------------------------------------------------------

_state_lock = threading.RLock()
_plain_dir: Optional[str] = None


def _ascii_temp_root() -> Optional[str]:
    """挑一个**纯 ASCII** 的临时根；找不到返回 ``None``。

    为什么在意这件事：LDDec 的 WinDec 版是用 ``system('type "文件" > "临时文件"')``
    读文件的，cmd 走 ANSI，路径里一旦有中文就会读不到 —— 表现为「dec.exe 返回 0
    但没有任何产物」。而 ``%TEMP%`` 常常就是 ``C:\\Users\\张三\\AppData\\Local\\Temp``
    （中文用户名很常见），所以优先挑一个不含非 ASCII 的位置。
    """
    candidates = [tempfile.gettempdir()]
    if os.name == "nt":
        win_dir = os.environ.get("SystemRoot") or os.environ.get("windir") or r"C:\Windows"
        candidates.append(os.path.join(win_dir, "Temp"))
    for path in candidates:
        if path and path.isascii() and os.path.isdir(path):
            return path
    return None


def plaintext_dir() -> str:
    """明文临时目录（懒创建）。用 ``mkdtemp``：权限仅当前用户，且可被系统清理。"""
    global _plain_dir
    with _state_lock:
        if _plain_dir is None or not os.path.isdir(_plain_dir):
            _plain_dir = tempfile.mkdtemp(prefix="wm-dlp-", dir=_ascii_temp_root())
        return _plain_dir


def new_plaintext_path(src: str) -> str:
    """为 ``src`` 生成一个**随机名**的明文落点（不复用原文件名）。

    随机名是刻意的：明文目录可能被某些清理逻辑按目录整删，复用原文件名会让
    "同名不同文件"互相覆盖；同时避免明文以原名出现在临时目录里。
    """
    ext = os.path.splitext(src)[1]
    return os.path.join(plaintext_dir(), "p%s%s" % (uuid.uuid4().hex, ext))


def release(path: Optional[str]) -> None:
    """删除一条明文；路径为空 / 已在别处清理过都安全。"""
    if not path:
        return
    try:
        if os.path.isfile(path):
            os.remove(path)
    except OSError:
        # 删不掉（被占用 / 已删）没有补偿动作：atexit 与系统临时目录清理会兜底
        pass


def release_all() -> None:
    """清空整个明文目录（进程退出 / 关闭时调用）。"""
    global _plain_dir
    with _state_lock:
        directory, _plain_dir = _plain_dir, None
    if directory:
        shutil.rmtree(directory, ignore_errors=True)


atexit.register(release_all)


# ---------------------------------------------------------------------------
# 解密提供者
# ---------------------------------------------------------------------------

class DecryptProvider:
    """解密提供者接口：把**密文**变成**明文**。

    ``decrypt(src, dst)`` 的契约（实现方必须遵守）：

    * ``src`` 是**只读**的原文件 —— 不得覆盖、改写、删除它；
    * 必须把明文写到 ``dst``；
    * 失败一律抛 :class:`DecryptError`，消息会直接展示给用户。
    """

    name = "base"

    def available(self) -> bool:
        """当前环境是否真的可用（缺依赖 / 缺配置时返回 ``False``）。"""
        return False

    def decrypt(self, src: str, dst: str) -> None:
        raise DecryptError("该解密提供者未实现")


class CommandProvider(DecryptProvider):
    """按命令模板调用外部解密器（唯一面向真实部署的提供者）。

    模板是**命令行字符串**，支持占位符：

    * ``{src}`` —— **临时目录里的密文副本**（绝不是用户的原文件）；
    * ``{dst}`` —— 期望的明文输出路径；
    * ``{ext}`` —— 不带点的扩展名。

    之所以先复制一份密文再交给解密器：像 LDDec 这类工具是"就地覆盖"语义，把
    **副本**交给它，覆盖的也只会是副本，原文件的加密状态从头到尾不受影响。
    """

    name = "command"

    def __init__(self, template: str, timeout: float = DEFAULT_TIMEOUT) -> None:
        self.template = template
        self.timeout = float(timeout) if timeout and timeout > 0 else DEFAULT_TIMEOUT
        self._argv = _split_command(template)

    def available(self) -> bool:
        return bool(self._argv)

    def decrypt(self, src: str, dst: str) -> None:
        if not self._argv:
            raise DecryptError("解密命令为空")
        ext = os.path.splitext(src)[1]
        work = os.path.join(plaintext_dir(), "s%s%s" % (uuid.uuid4().hex, ext))
        try:
            shutil.copy2(src, work)
        except OSError as exc:
            raise DecryptError(f"无法读取源文件（{exc}）") from exc
        try:
            argv = [part.format(src=work, dst=dst, ext=ext.lstrip("."))
                    for part in self._argv]
            try:
                completed = subprocess.run(
                    argv, cwd=plaintext_dir(), timeout=self.timeout,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    creationflags=_NO_WINDOW)
            except FileNotFoundError as exc:
                raise DecryptError(f"解密器不存在：{self._argv[0]}") from exc
            except subprocess.TimeoutExpired:
                raise DecryptError(f"解密超时（超过 {self.timeout:.0f} 秒）")
            except OSError as exc:
                raise DecryptError(f"调用解密器失败：{exc}") from exc
            if completed.returncode != 0:
                detail = (_tail(completed.stderr) or _tail(completed.stdout)
                          or f"退出码 {completed.returncode}")
                raise DecryptError(f"解密器返回失败（{detail}）")
            produced = _locate_plaintext(work, dst)
            if produced is None:
                raise DecryptError("解密器没有产生任何明文文件")
            if os.path.abspath(produced) != os.path.abspath(dst):
                try:
                    shutil.move(produced, dst)
                except OSError as exc:
                    raise DecryptError(f"无法接管解密产物（{exc}）") from exc
            if os.path.getsize(dst) <= 0:
                raise DecryptError("解密产物为空（0 字节）")
        finally:
            # 密文副本与其衍生文件一律清掉，只留 dst
            for leftover in (work, work + ".dec_", work + "_dec"):
                if os.path.abspath(leftover) != os.path.abspath(dst):
                    release(leftover)


class TransparentReadProvider(DecryptProvider):
    """「借系统受信任进程读文件」通道的公共骨架。

    原理（与 price-tool 的 ``lddec_core`` 一致，也是 LDDec 的 WinDec 版所用的）：
    透明加密驱动按**进程**决定是否返回明文。系统自带的 ``cmd.exe`` /
    ``powershell.exe`` 常常就在管理员配的信任名单里，于是让它们去读文件，读到的
    就是明文 —— **不需要任何第三方二进制**。

    这里**没有**任何进程伪装：不改名、不注入、不冒充白名单进程名。能不能读到
    明文完全取决于管理员的策略；没授权时读到的仍是密文，随后会被
    :func:`verify_plaintext` 判为"解密失败"。

    两个实现细节不能省：

    * **必须先复制到临时副本再读** —— 用户原文件路径常含中文，而 ``cmd`` 走
      ANSI，读不到就静默返回空（这也是 :func:`_ascii_temp_root` 存在的原因）；
    * **副本要保持原扩展名** —— 驱动只对受保护的类型做透明解密，把 ``.pdf``
      副本改名成 ``.enc`` 之类会被当普通文件、原样吐出密文。
    """

    name = "read"

    #: 通道标签（给用户看的失败信息用）
    label = "系统读取"

    def __init__(self, timeout: float = DEFAULT_TIMEOUT) -> None:
        self.timeout = float(timeout) if timeout and timeout > 0 else DEFAULT_TIMEOUT

    def available(self) -> bool:
        raise NotImplementedError

    def _read_plaintext(self, work: str, dst: str) -> None:
        """把 ``work`` 的明文读到 ``dst``；失败抛 :class:`DecryptError`。"""
        raise NotImplementedError

    def decrypt(self, src: str, dst: str) -> None:
        if not self.available():
            raise DecryptError(f"{self.label}通道在当前系统不可用")
        if not os.path.isfile(src):
            raise DecryptError("源文件不存在")
        ext = os.path.splitext(src)[1]
        work = os.path.join(plaintext_dir(), "s%s%s" % (uuid.uuid4().hex, ext))
        try:
            shutil.copy2(src, work)
        except OSError as exc:
            raise DecryptError(f"无法读取源文件（{exc}）") from exc
        try:
            self._read_plaintext(work, dst)
            if not os.path.isfile(dst):
                raise DecryptError(f"{self.label}没有产生任何输出")
            if os.path.getsize(dst) <= 0:
                raise DecryptError(f"{self.label}读到 0 字节（路径可能含非 ASCII 字符）")
        finally:
            release(work)


class CmdTypeProvider(TransparentReadProvider):
    """借 ``cmd /c type`` 读明文（LDDec 的 WinDec 版就是这么做的）。

    已实测：现代 Windows 的 ``type`` 对二进制**字节保真**（0x1A 不再被当 EOF），
    所以图片 / PDF 走这条路不会变形。

    保留 price-tool 的三种调用形式并依次重试：不同调用方式对引号的处理不一致，
    而失败往往表现为"返回 0 但没产物"，多试一次的成本远低于排查一次。
    """

    name = "cmd-type"
    label = "cmd 读取"

    def __init__(self, timeout: float = CMDTYPE_TIMEOUT) -> None:
        super().__init__(timeout)

    def available(self) -> bool:
        return os.name == "nt" and bool(_cmd_executable())

    def _read_plaintext(self, work: str, dst: str) -> None:
        cmd = _cmd_executable()
        attempts = (
            [cmd, "/c", "type", work],                 # 独立参数（标准）
            [cmd, "/c", "type", '"' + work + '"'],     # 手动加引号
            cmd + " /c type " + '"' + work + '"',      # 整条字符串
        )
        problems: List[str] = []
        for args in attempts:
            release(dst)
            try:
                with open(dst, "wb") as sink:
                    completed = subprocess.run(
                        args, stdout=sink, stderr=subprocess.PIPE,
                        timeout=self.timeout, creationflags=_NO_WINDOW)
            except subprocess.TimeoutExpired:
                problems.append("超时")
                continue
            except OSError as exc:
                problems.append(str(exc))
                continue
            if completed.returncode == 0 and os.path.isfile(dst) and os.path.getsize(dst) > 0:
                return
            problems.append("退出码 %s" % completed.returncode)
        raise DecryptError("%s失败（%s）" % (self.label, "；".join(problems[-2:]) or "无输出"))


class PowershellProvider(TransparentReadProvider):
    """借 PowerShell 的 ``[IO.File]::ReadAllBytes`` 读明文。

    与 ``type`` 相比：PowerShell 走 Unicode，**不受中文路径影响**，而且是纯字节
    读写、不经任何文本转换 —— 放在 ``type`` 之后兜底正合适。代价是冷启动比 cmd
    慢一两秒。
    """

    name = "powershell"
    label = "PowerShell 读取"

    def __init__(self, timeout: float = POWERSHELL_TIMEOUT) -> None:
        super().__init__(timeout)

    def available(self) -> bool:
        return os.name == "nt" and bool(_powershell_executable())

    def _read_plaintext(self, work: str, dst: str) -> None:
        # 路径进单引号字符串：只需把单引号本身翻倍；其余字符（含中文、空格、
        # 方括号、$）在单引号里都是字面量，无需再转义。
        script = (
            "$b=[IO.File]::ReadAllBytes('" + work.replace("'", "''") + "');"
            "$s=[IO.File]::OpenWrite('" + dst.replace("'", "''") + "');"
            "$s.Write($b,0,$b.Length);$s.Close()"
        )
        release(dst)
        try:
            completed = subprocess.run(
                [_powershell_executable(), "-NoProfile", "-NonInteractive",
                 "-Command", script],
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                timeout=self.timeout, creationflags=_NO_WINDOW)
        except subprocess.TimeoutExpired:
            raise DecryptError(f"{self.label}超时（超过 {self.timeout:.0f} 秒）")
        except OSError as exc:
            raise DecryptError(f"{self.label}调用失败：{exc}")
        if completed.returncode != 0:
            detail = _tail(completed.stderr)
            raise DecryptError(
                f"{self.label}失败（{detail or '退出码 %s' % completed.returncode}）")


class ChainProvider(DecryptProvider):
    """多通道回退：按优先级依次尝试，**任一通道产出可用明文即成功**。

    为什么需要它：透明加密的环境千差万别 —— 有的机器装了 LDDec、有的只信任
    cmd、有的只信任 PowerShell。单通道一旦不匹配就整个加不进去；多通道只是把
    每种可能各试一次，代价仅在于失败时多花几秒。

    每一环都以 :func:`verify_plaintext` 为准（**不是**"命令返回 0"）：返回 0 但
    读到的仍是密文的情况非常常见，只有内容对了才算成功。
    """

    name = "chain"

    def __init__(self, providers: List[DecryptProvider]) -> None:
        self.providers = [p for p in providers if p is not None]
        self.last_used: Optional[DecryptProvider] = None

    def available(self) -> bool:
        return any(p.available() for p in self.providers)

    def describe(self) -> str:
        ready = [p for p in self.providers if p.available()]
        return "、".join(_channel_label(p) for p in ready) or "无可用通道"

    def decrypt(self, src: str, dst: str) -> None:
        if not self.providers:
            raise DecryptError("没有配置任何解密通道")
        notes: List[str] = []
        for provider in self.providers:
            if not provider.available():
                continue
            label = _channel_label(provider)
            try:
                provider.decrypt(src, dst)
            except DecryptError as exc:
                notes.append(f"{label}：{exc}")
                release(dst)
                continue
            error = verify_plaintext(dst)
            if error is None:
                self.last_used = provider
                return
            notes.append(f"{label}：{error}")
            release(dst)
        if not notes:
            raise DecryptError("没有可用的解密通道")
        if len(notes) == 1:
            raise DecryptError(notes[0])
        # 全列会淹没界面：给总数 + 前两条，足够定位
        raise DecryptError("已尝试 %d 种方式均失败 —— %s" % (
            len(notes), "；".join(notes[:2])))


def _channel_label(provider: DecryptProvider) -> str:
    """通道的用户可见名（优先 ``describe()``，LDDec 会带上 exe 名）。"""
    describe = getattr(provider, "describe", None)
    if callable(describe):
        try:
            return str(describe())
        except Exception:
            pass
    return str(getattr(provider, "label", None) or getattr(provider, "name", provider))


def _cmd_executable() -> Optional[str]:
    """cmd.exe 的绝对路径（优先 ``COMSPEC`` —— 它在任何 Windows 上都有）。"""
    if os.name != "nt":
        return None
    comspec = (os.environ.get("COMSPEC") or "").strip()
    if comspec and os.path.isfile(comspec):
        return comspec
    found = shutil.which("cmd") or shutil.which("cmd.exe")
    return found if found and os.path.isfile(found) else None


def _powershell_executable() -> Optional[str]:
    """powershell.exe 的绝对路径；没有返回 ``None``（精简版系统可能不带）。"""
    if os.name != "nt":
        return None
    found = shutil.which("powershell") or shutil.which("powershell.exe")
    return found if found and os.path.isfile(found) else None


# ---------------------------------------------------------------------------
# LDDec（天锐绿盾解密工具）内置适配器
# ---------------------------------------------------------------------------
#
# 契约全部来自 LDDec 源码（``Dec/main_dec.cpp`` / ``Common/Setting.cpp`` /
# ``Faker/main_faker.cpp``），不是猜的：
#
# 仓库里其实有**两套**实现，二进制都叫 dec.exe，命令行用法相同（``dec.exe <文件或目录>``），
# 但内部机制完全不同。适配层两者都支持，因为它们对外表现一致：
#
# **TCP 版**（``Dec/main_dec.cpp``，Qt）
#   * 同目录要有 ``config.json``（``{"port": 34500, "faker": "notepad.exe"}``）；
#     dec.exe 起 TCP server，拉起 Faker 进程读文件（驱动信任它的进程名 → 明文），
#     10KB 分块回传。
#   * 产物：``<file>.dec_`` → remove + rename 覆盖。
#   * ``-view`` **未实现**（源码只取 ``args.at(1)``，从不看第二个参数）。
#
# **cmd 版**（``WinDec/main.cpp``，无 Qt 依赖 —— **仓库里提交的那份 dec.exe 就是它**）
#   * 用 ``system('type "文件" > "文件_dec"')`` 借 cmd.exe 读文件（驱动若信任
#     cmd.exe 就返回明文），然后 remove + rename 覆盖。
#   * **不需要** config.json / Faker / TCP；支持 ``-view``（只判断不解）。
#   * 走 ANSI，路径含非 ASCII 时 ``type`` 会读不到 —— 见 ``_ascii_temp_root``。
#
# **共同点**（适配层真正依赖的契约）：
#   * 自己判断加密：头 3 字节 != ``88 7D 1C`` 的文件直接跳过；
#   * 产物先落 ``<file>.dec_`` 或 ``<file>_dec``，再 **remove + rename 就地覆盖**；
#   * 单文件模式**不打任何输出**，成功与否只能靠产物判断。
#
# 因此适配层做三件事把它的语义"掰"回本项目的契约：
#
#   1. **只把临时目录里的密文副本交给 dec.exe** —— 它的就地覆盖永远只覆盖副本；
#   2. **只传文件、绝不传目录** —— 目录模式会把整个目录的加密文件都解密并覆盖；
#   3. **串行调用** —— dec.exe 固定监听 34500，并发必然端口冲突。

#: dec.exe / dec_linux 的文件名（按平台）
LDDEC_EXE_NAMES = ("dec.exe",) if os.name == "nt" else ("dec_linux", "dec.exe")

#: 相对程序目录的自动搜索位置（按优先级）
LDDEC_SEARCH_DIRS = ("LDDec", os.path.join("tools", "LDDec"), "",
                     os.path.join("tools", "lddec"))

#: LDDec 一次调用要起 TCP server + 等 Faker 连接（源码固定 5s）+ 传完整个文件，
#: 比普通外部命令慢，超时给得宽松些。
LDDEC_TIMEOUT = 60.0

#: dec.exe 的退出码 → 中文解释。它的失败信息只有 qDebug 输出，常常拿不到，
#: 靠退出码兜底能给用户一个可行动的提示。
LDDEC_EXIT_HINTS = {
    -1: "Faker 进程没能连上（检查 dec.exe 同目录的 config.json、"
        "本机是否已装绿盾客户端、34500 端口是否被占用）",
    4294967295: "Faker 进程没能连上（检查 dec.exe 同目录的 config.json、"
                "本机是否已装绿盾客户端、34500 端口是否被占用）",
}

#: dec.exe 固定监听的端口（源码 Setting 默认值）；仅用于串行化说明与诊断
LDDEC_PORT = 34500

#: LDDec 通道的串行锁：同一时刻只跑一个 dec.exe（端口 34500 会冲突）
_lddec_lock = threading.RLock()


def app_dir() -> str:
    """程序目录：冻结后是解包目录 ``sys._MEIPASS``，源码运行是项目根目录。"""
    if getattr(sys, "frozen", False):
        return getattr(sys, "_MEIPASS", None) or os.path.dirname(sys.executable)
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def find_lddec(explicit: Optional[str] = None) -> Optional[str]:
    """定位 LDDec 可执行文件；找不到返回 ``None``。

    搜索顺序：显式参数 → ``WM_LDDEC_EXE`` → 程序目录下的
    ``LDDec/``、``tools/LDDec/``、程序目录本身、``tools/lddec/``。
    显式参数与环境变量都可以是**文件或所在目录**。
    """
    candidates: List[str] = []
    if explicit:
        candidates.append(explicit)
    candidates.append((os.environ.get("WM_LDDEC_EXE") or "").strip())
    base = app_dir()
    for sub in LDDEC_SEARCH_DIRS:
        for name in LDDEC_EXE_NAMES:
            candidates.append(os.path.join(base, sub, name))
    for path in candidates:
        if not path:
            continue
        if os.path.isfile(path):
            return os.path.abspath(path)
        if os.path.isdir(path):
            for name in LDDEC_EXE_NAMES:
                hit = os.path.join(path, name)
                if os.path.isfile(hit):
                    return os.path.abspath(hit)
    return None


class LddecProvider(DecryptProvider):
    """LDDec 的**内置**适配器：程序目录里有 ``dec.exe`` 就自动生效，零配置。

    只负责"按 LDDec 的方式调用它并接管产物"，**不重新实现**它的任何内部机制
    （TCP 协议、Faker 进程等一概不碰）。
    """

    name = "lddec"

    def __init__(self, exe: str, timeout: float = LDDEC_TIMEOUT) -> None:
        self.exe = str(exe)
        self.timeout = float(timeout) if timeout and timeout > 0 else LDDEC_TIMEOUT

    def available(self) -> bool:
        return bool(self.exe) and os.path.isfile(self.exe)

    def describe(self) -> str:
        return "LDDec（%s）" % os.path.basename(self.exe)

    def decrypt(self, src: str, dst: str) -> None:
        if not self.available():
            raise DecryptError(f"未找到 LDDec 可执行文件：{self.exe}")
        exe_dir = os.path.dirname(self.exe)
        # config.json **只是 TCP 版需要**（里面是 port 与 faker 进程名）；cmd 版
        # （WinDec）压根不读它。所以这里**不**据此拒绝调用 —— 否则只放了一个
        # dec.exe 的机器会被误判成"不可用"。它只在报错文案里作为诊断信息出现。
        has_config = os.path.isfile(os.path.join(exe_dir, "config.json"))
        if not os.path.isfile(src):
            raise DecryptError("源文件不存在")
        ext = os.path.splitext(src)[1]
        # **只把副本交给 dec.exe**：它的语义是就地覆盖，覆盖副本 = 原文件毫发无损
        work = os.path.join(plaintext_dir(), "s%s%s" % (uuid.uuid4().hex, ext))
        try:
            shutil.copy2(src, work)
        except OSError as exc:
            raise DecryptError(f"无法读取源文件（{exc}）") from exc
        try:
            with _lddec_lock:  # 端口 34500 固定，绝不能并发
                try:
                    completed = subprocess.run(
                        [self.exe, work], cwd=exe_dir, timeout=self.timeout,
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                        creationflags=_NO_WINDOW)
                except subprocess.TimeoutExpired:
                    raise DecryptError(
                        f"LDDec 解密超时（超过 {self.timeout:.0f} 秒）")
                except OSError as exc:
                    raise DecryptError(f"调用 dec.exe 失败：{exc}") from exc
            if completed.returncode != 0:
                hint = LDDEC_EXIT_HINTS.get(completed.returncode)
                detail = _tail(completed.stderr) or _tail(completed.stdout)
                raise DecryptError("dec.exe 返回失败（%s）" % (
                    hint or detail or f"退出码 {completed.returncode}"
                    or ("" if has_config else "（TCP 版需要同目录 config.json）")))
            produced = _locate_plaintext(work, dst)
            if produced is None:
                # 最常见的原因：cmd 版用 `type` 读文件时路径里有中文/空格以外的
                # 非 ASCII 字符，cmd 读不到就把副本删了、改名也失败。
                raise DecryptError(
                    "dec.exe 没有产生明文文件（副本已被删除，原文件未受影响）；"
                    "若路径含中文，请确认本机临时目录为纯 ASCII 路径")
            if os.path.abspath(produced) != os.path.abspath(dst):
                try:
                    shutil.move(produced, dst)
                except OSError as exc:
                    raise DecryptError(f"无法接管解密产物（{exc}）") from exc
            if os.path.getsize(dst) <= 0:
                raise DecryptError("解密产物为空（0 字节）")
        finally:
            for leftover in (work, work + ".dec_", work + "_dec"):
                if os.path.abspath(leftover) != os.path.abspath(dst):
                    release(leftover)


def _split_command(template: str) -> List[str]:
    """把命令模板切成 argv。

    用非 posix 模式切：posix 模式会把 Windows 路径里的反斜杠当转义符吃掉
    （``C:\\Tools\\dec.exe`` → ``C:Toolsdec.exe``）。代价是它**保留**引号，
    所以这里再手工剥掉成对的外层双引号 —— 否则带空格的程序路径会连引号一起被
    当成文件名。
    """
    tokens = shlex.split(template, posix=False)
    return [t[1:-1] if len(t) > 1 and t.startswith('"') and t.endswith('"') else t
            for t in tokens]


def _locate_plaintext(work: str, dst: str) -> Optional[str]:
    """在解密器可能的输出位置里找明文。

    顺序：显式 ``{dst}`` → ``<副本>.dec_`` → ``<副本>_dec`` → 副本本身（就地覆盖型
    解密器，如 LDDec 的 ``dec.exe``）。
    """
    for candidate in (dst, work + ".dec_", work + "_dec", work):
        try:
            if os.path.isfile(candidate) and os.path.getsize(candidate) > 0:
                return candidate
        except OSError:
            continue
    return None


def _tail(raw: object, limit: int = 200) -> str:
    """取子进程输出的末尾若干字符（错误信息往往在最后）。"""
    if not isinstance(raw, bytes):
        return ""
    text = raw.decode("utf-8", "replace").strip().replace("\r", " ")
    return text[-limit:]


# ---------------------------------------------------------------------------
# 提供者装配（配置注入）
# ---------------------------------------------------------------------------

_provider: Optional[DecryptProvider] = None
_provider_resolved = False


def get_provider() -> Optional[DecryptProvider]:
    """当前生效的解密提供者；未配置返回 ``None``（此时加密文件无法解密）。"""
    global _provider, _provider_resolved
    with _state_lock:
        if not _provider_resolved:
            _provider = _build_provider()
            _provider_resolved = True
        return _provider


def set_provider(provider: Optional[DecryptProvider]) -> None:
    """覆盖当前提供者（**测试与集成用**）；传 ``None`` 表示"没有解密器"。"""
    global _provider, _provider_resolved
    with _state_lock:
        _provider = provider
        _provider_resolved = True


def reset_provider() -> None:
    """丢弃缓存的提供者，下次 :func:`get_provider` 重新按环境装配。"""
    global _provider, _provider_resolved
    with _state_lock:
        _provider = None
        _provider_resolved = False


def _build_provider() -> Optional[DecryptProvider]:
    """装配解密通道，优先级：显式命令 > **内置回退链** > 无。

    * ``WM_DLP_ENABLE``      —— ``0/off/false`` 一键关停全部解密能力。
    * ``WM_DLP_DECRYPT_CMD`` —— 通用命令模板（形如 ``"…dec.exe" "{src}" "{dst}"``），
      想接别的解密器时用；填了它就**只**用这条，不启用内置链。

    不填时启用内置回退链（与 price-tool 的 ``lddec_core`` 同构），按序尝试：

    1. **LDDec**（程序目录下有 ``dec.exe`` 时）—— 面向天锐绿盾的专用工具；
    2. **cmd 读取**（``cmd /c type``）—— 系统自带，零部署；
    3. **PowerShell 读取**（``[IO.File]::ReadAllBytes``）—— 系统自带，不受
       中文路径影响。

    后两条让工具在**完全没有第三方二进制**的机器上也有机会解出明文（前提是
    管理员把 cmd.exe / powershell.exe 放进了信任名单）。三者都不可用时返回
    ``None``，加密文件按"无法解密"处理。

    * ``WM_LDDEC_EXE``   —— 显式指定 dec.exe 路径（或其所在目录）。
    * ``WM_DLP_TIMEOUT`` —— 单文件解密超时秒数（默认 30；LDDec 通道默认 60）。
    """
    flag = (os.environ.get("WM_DLP_ENABLE") or "1").strip().lower()
    if flag in ("0", "false", "no", "off"):
        return None
    template = (os.environ.get("WM_DLP_DECRYPT_CMD") or "").strip()
    if template:
        try:
            timeout = float((os.environ.get("WM_DLP_TIMEOUT") or "").strip()
                            or DEFAULT_TIMEOUT)
        except ValueError:
            timeout = DEFAULT_TIMEOUT
        return CommandProvider(template, timeout)
    chain: List[DecryptProvider] = []
    exe = find_lddec()
    if exe:
        chain.append(LddecProvider(exe))
    chain.append(CmdTypeProvider())
    chain.append(PowershellProvider())
    return ChainProvider(chain)


# ---------------------------------------------------------------------------
# 对外主入口
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Resolution:
    """一个源文件的解析结果。

    ``path`` 为 ``None`` 表示**应跳过该文件**（``message`` 是给用户看的原因）；
    否则 ``path`` 是加进列表 / 用于输出命名的**原路径**，``read_path`` 是实际
    读取路径（解密成功时指向临时明文，其余情况与 ``path`` 相同）。
    """

    path: Optional[str]
    read_path: Optional[str]
    state: str
    message: str = ""


def resolve(path: str) -> Resolution:
    """把一条路径解析成"可读路径"，必要时先解密。

    判定顺序（刻意保守，宁可多读一次也不误杀）：

    1. 头 3 字节不是加密魔数 → **原样返回**，零额外开销；
    2. 命中魔数 → 先**试着按原路径打开一次**：能打开说明当前进程读到的就是明文
       （白名单已生效，或只是魔数误判），同样原样返回；
    3. 打不开且**没有解密器** → 失败，原因写明"未配置解密器"；
    4. 打不开且有解密器 → 解密到临时目录 → 校验产物 → 成功返回临时明文路径，
       失败则清理产物并返回原因。
    """
    if not is_encrypted(path):
        return Resolution(path, path, STATE_PLAIN)
    if probe_readable(path) is None:
        # 疑似加密但读得开：可能是透明解密已生效，也可能只是魔数撞上了
        return Resolution(path, path, STATE_DIRECT)
    provider = get_provider()
    if provider is None or not provider.available():
        return Resolution(
            None, None, STATE_FAILED,
            "文件受加密软件保护，当前环境无法解密（本机没有可用的解密组件）；"
            "请联系信息安全部门为水印工具授权")
    dst = new_plaintext_path(path)
    try:
        provider.decrypt(path, dst)
    except DecryptError as exc:
        release(dst)
        return Resolution(None, None, STATE_FAILED, str(exc))
    except Exception as exc:  # 解密器实现可能有未预料的行为，不能让它带崩添加流程
        release(dst)
        return Resolution(None, None, STATE_FAILED, f"解密异常：{type(exc).__name__}: {exc}")
    error = verify_plaintext(dst)
    if error:
        release(dst)
        return Resolution(None, None, STATE_FAILED, error)
    return Resolution(path, dst, STATE_DECRYPTED)


def resolve_all(paths: List[str]) -> Tuple[List[Resolution], List[Resolution]]:
    """批量解析：返回 ``(可用结果, 失败结果)`` —— 失败项**不影响**其它文件。"""
    usable: List[Resolution] = []
    failed: List[Resolution] = []
    for path in paths:
        try:
            result = resolve(path)
        except Exception as exc:  # resolve 自身不该抛；真抛了也不能连累整批
            failed.append(Resolution(None, None, STATE_FAILED,
                                     f"加密探测异常：{type(exc).__name__}: {exc}"))
            continue
        (usable if result.path else failed).append(result)
    return usable, failed


def probe_readable(path: str) -> Optional[str]:
    """试着按原路径打开一次文件；能打开返回 ``None``，否则返回原因。

    放在这里而不是调用方，是为了让"能不能直读"这件事只有一种判法（与
    :class:`wm.media.Document` 完全一致），避免两处逻辑漂移。
    """
    try:
        from . import media
    except Exception as exc:
        return f"文件解析模块不可用（{exc}）"
    try:
        document = media.Document(path)
    except Exception as exc:
        return str(exc) or "无法打开该文件"
    try:
        document.close()
    except Exception:
        pass
    return None


__all__ = [
    "DEFAULT_MAGIC",
    "DEFAULT_TIMEOUT",
    "LDDEC_EXE_NAMES",
    "LDDEC_SEARCH_DIRS",
    "LDDEC_TIMEOUT",
    "LDDEC_PORT",
    "LddecProvider",
    "app_dir",
    "find_lddec",
    "STATE_PLAIN",
    "STATE_DIRECT",
    "STATE_DECRYPTED",
    "STATE_FAILED",
    "CMDTYPE_TIMEOUT",
    "POWERSHELL_TIMEOUT",
    "DecryptError",
    "DecryptProvider",
    "CommandProvider",
    "TransparentReadProvider",
    "CmdTypeProvider",
    "PowershellProvider",
    "ChainProvider",
    "Resolution",
    "magic",
    "is_encrypted",
    "verify_plaintext",
    "plaintext_dir",
    "new_plaintext_path",
    "release",
    "release_all",
    "get_provider",
    "set_provider",
    "reset_provider",
    "resolve",
    "resolve_all",
    "probe_readable",
]
