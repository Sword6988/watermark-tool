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
import time
import uuid
from dataclasses import dataclass
from typing import Dict, List, Optional, Set, Tuple

#: 天锐绿盾加密文件头（与文件格式无关：PDF / 图片一视同仁，见集成方案文档）。
#: 可用环境变量 ``WM_DLP_MAGIC``（十六进制串，如 ``"88 7d 1c"``）覆盖。
DEFAULT_MAGIC = b"\x88\x7d\x1c"

#: 探测时读取的字节数（够放魔数即可，不碰文件其余内容）
_PROBE_READ_LEN = 8
#: 校验解密产物时读取的字节数（PDF 允许头部有垃圾前缀，要放宽搜索范围）
_VERIFY_READ_LEN = 1024

#: 单个文件的解密超时（秒）。驱动无响应时不能把整个添加过程挂死。
DEFAULT_TIMEOUT = 30.0

#: **单个文件的解密总预算**（秒）。它是「每通道超时」之上的一道总闸：
#: 三条通道各自 60/60/90 秒时，最坏会卡 330 秒且中途无法取消。有了总预算，
#: 每条通道只拿到「剩余预算」，超时即放弃该文件转下一个。可用
#: ``WM_DLP_BUDGET`` 覆盖（0 或负数表示不设总闸）。
DEFAULT_BUDGET = 120.0

#: 临时明文的总量上限（字节）。明文必须常驻到文件从列表移除为止，所以**不能**
#: LRU 淘汰（淘汰了就没得读了）；改用「到达上限后停止继续解密并提示分批」，
#: 避免一次拖入几百个大文件把磁盘撑爆。可用 ``WM_DLP_MAX_BYTES`` 覆盖。
DEFAULT_MAX_PLAIN_BYTES = 4 * 1024 * 1024 * 1024

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

def _warn(message: str) -> None:
    """记一条诊断信息（不打扰用户：解密链路的旁支不该弹框）。

    写 stderr 而不是日志框架：本模块可能被无头脚本直接调用，且在冻结版里
    stderr 已被 ``main._FileWriter`` 接到 runtime.log，排查时找得到。
    """
    try:
        print("[WARN][dlp] " + message, file=sys.stderr)
    except Exception:
        pass


#: 逐通道跟踪开关的环境变量名。打开后，每条通道的「尝试 / 成功 / 失败」与耗时
#: 都会写 stderr（冻结版进 ``runtime-<pid>.log``），用来回答内网的两个未知：
#: **到底是哪条通道解的**、**单文件花了多久**。默认关闭 —— 常规使用不该刷屏。
TRACE_ENV = "WM_DLP_TRACE"

#: ``None`` = 还没读过环境变量；读一次就缓存（进程内开关不该中途变）。
_trace_on: Optional[bool] = None


def _trace(message: str) -> None:
    """跟踪日志：只有 ``WM_DLP_TRACE=1`` 时才写，否则完全安静。

    为什么不做成日志级别：本模块要能被无头脚本（``tools/probe_dlp_channels.py``）
    和冻结版同时用，而冻结版的 stderr 已被 ``main`` 接到 runtime.log —— 走 stderr
    是唯一一条两条路都通的渠道。
    """
    global _trace_on
    if _trace_on is None:
        flag = (os.environ.get(TRACE_ENV) or "").strip().lower()
        _trace_on = flag in ("1", "true", "yes", "on")
    if not _trace_on:
        return
    try:
        print("[TRACE][dlp] " + message, file=sys.stderr)
        sys.stderr.flush()
    except Exception:
        pass


def _safe_size(path: str) -> int:
    """文件大小；读不到就是 0（跟踪日志不该因为 stat 失败把主流程带崩）。"""
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def magic() -> bytes:
    """当前生效的加密魔数（默认 ``88 7D 1C``，可用环境变量覆盖）。"""
    raw = (os.environ.get("WM_DLP_MAGIC") or "").strip().replace(" ", "").replace("0x", "")
    if not raw:
        return DEFAULT_MAGIC
    try:
        return bytes.fromhex(raw)
    except ValueError:
        # 配错了也不能让工具起不来：退回默认值 —— 但**必须留痕**，否则「魔数
        # 配错导致一律探测不到」和「本机根本没有加密文件」在用户看来一模一样。
        _warn("WM_DLP_MAGIC 不是合法十六进制串（%r），已回退默认魔数 %s"
              % (raw, DEFAULT_MAGIC.hex(" ")))
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
    # 回落到 None 意味着「明知有坑还要踩」：cmd 版 type 走 ANSI，非 ASCII 路径
    # 会读不到文件。此时报错文案会让人「确认临时目录为纯 ASCII」，而代码自己
    # 刚放弃了这个保证 —— 所以必须留痕，否则排查时会原地打转。
    _warn("找不到纯 ASCII 的临时目录（候选：%s），"
          "cmd 版解密器可能因非 ASCII 路径读不到文件" % "、".join(map(str, candidates)))
    return None


#: 明文目录的名字前缀（启动期 GC 靠它识别「本工具留下的目录」）
PLAIN_DIR_PREFIX = "wm-dlp-"

#: 目录里记录的持有者 pid 文件名：GC 靠它区分「别的实例在用」与「孤儿目录」
_PID_FILE = "owner.pid"

_gc_done = False


def plaintext_dir() -> str:
    """明文临时目录（懒创建）。用 ``mkdtemp``：权限仅当前用户，且可被系统清理。"""
    global _plain_dir, _gc_done
    with _state_lock:
        if _plain_dir is None or not os.path.isdir(_plain_dir):
            _plain_dir = tempfile.mkdtemp(prefix=PLAIN_DIR_PREFIX, dir=_ascii_temp_root())
            if not _plain_dir.isascii():
                # 已实测：现代 Windows 的 `type` 走 Unicode API，中文 / emoji 路径
                # 都能正常读（本机模拟验证见 _smoke/probe_cn_path.py）。所以这里
                # **不阻断**，只留痕 —— 真出问题时日志里一眼能看到，不用猜。
                _warn("临时明文目录含非 ASCII 字符：%s（若解密异常，请把程序放在"
                      "纯 ASCII 路径下，或确认临时目录为纯 ASCII）" % _plain_dir)
            _write_owner_pid(_plain_dir)
            if not _gc_done:
                _gc_done = True
                _gc_orphan_dirs(_plain_dir)
        return _plain_dir


def _write_owner_pid(directory: str) -> None:
    """在明文目录里写下持有者 pid（供其它实例判断「还活着吗」）。"""
    try:
        with open(os.path.join(directory, _PID_FILE), "w", encoding="ascii") as handle:
            handle.write(str(os.getpid()))
    except OSError:
        pass


def _pid_alive(pid: int) -> bool:
    """``pid`` 是否还在运行；无法判断时**保守返回 True**（宁可漏删也不误删）。"""
    if pid <= 0:
        return False
    if pid == os.getpid():
        return True
    if os.name == "nt":
        try:
            import ctypes
            handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION
            if not handle:
                return False
            ctypes.windll.kernel32.CloseHandle(handle)
            return True
        except Exception:
            return True  # 查不了就当活着
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    except Exception:
        return True
    return True


def _gc_orphan_dirs(keep: str) -> None:
    """清理**上一轮崩溃 / 被强杀**残留的明文目录（启动期跑一次）。

    ``atexit`` 只管正常退出；任务管理器结束进程、``os._exit``、断电都不会触发它，
    那些目录里的明文会一直躺在 ``%TEMP%`` —— 明文是本项目最不该留下的东西，
    所以下次启动时补一次清扫。

    判定标准（**宁可漏删也不误删**）：名字匹配 ``wm-dlp-*``、不是当前目录、
    且 ``owner.pid`` 缺失或对应进程已不存在。
    """
    roots = [tempfile.gettempdir()]
    if os.name == "nt":
        win_dir = os.environ.get("SystemRoot") or os.environ.get("windir") or r"C:\Windows"
        roots.append(os.path.join(win_dir, "Temp"))
    for root in roots:
        if not root or not os.path.isdir(root):
            continue
        try:
            names = os.listdir(root)
        except OSError:
            continue
        for name in names:
            if not name.startswith(PLAIN_DIR_PREFIX):
                continue
            path = os.path.join(root, name)
            if os.path.abspath(path) == os.path.abspath(keep):
                continue
            if not os.path.isdir(path):
                continue
            pid = -1
            try:
                with open(os.path.join(path, _PID_FILE), encoding="ascii") as handle:
                    pid = int(handle.read().strip() or -1)
            except (OSError, ValueError):
                pid = -1
            if pid > 0 and _pid_alive(pid):
                continue  # 别的实例正在用，绝不碰
            try:
                shutil.rmtree(path, ignore_errors=True)
                _warn("已清理上次异常退出残留的明文目录：%s" % path)
            except OSError as exc:
                _warn("残留明文目录清理失败（%s）：%s" % (path, exc))


def new_plaintext_path(src: str) -> str:
    """为 ``src`` 生成一个**随机名**的明文落点（不复用原文件名）。

    随机名是刻意的：明文目录可能被某些清理逻辑按目录整删，复用原文件名会让
    "同名不同文件"互相覆盖；同时避免明文以原名出现在临时目录里。
    """
    ext = os.path.splitext(src)[1]
    return os.path.join(plaintext_dir(), "p%s%s" % (uuid.uuid4().hex, ext))


def release(path: Optional[str]) -> None:
    """删除一条明文；路径为空 / 已在别处清理过都安全。

    删不掉**必须留痕**：明文是本项目最敏感的中间产物，静默失败会让它在磁盘上
    躺到系统清理为止，而排查时连线索都没有（Windows 上最常见的原因是文件仍被
    占用 —— 往往是某个 PIL/fitz 句柄没关，见 ``wm.media`` 的 close 路径）。
    """
    global _plain_bytes
    if not path:
        return
    # 先把账目扣回去：总量上限（M7）若只增不减，用户删掉一批文件后额度也不会
    # 恢复，表现是「明明删光了却还是提示超过上限」。
    with _state_lock:
        spent = _plain_sizes.pop(os.path.abspath(path), 0)
        _plain_bytes = max(0, _plain_bytes - spent)
    try:
        if os.path.isfile(path):
            os.remove(path)
        elif os.path.isdir(path):
            shutil.rmtree(path, ignore_errors=True)
        return
    except OSError as exc:
        _warn("删除临时明文失败（%s）：%s" % (path, exc))
    # 兜底：确实删不掉时至少确认一下它到底还在不在，把结果写进日志
    try:
        if os.path.exists(path):
            _warn("临时明文仍然存在，需人工/系统清理：%s" % path)
    except OSError:
        pass


def release_all() -> None:
    """清空整个明文目录（进程退出 / 关闭时调用）。"""
    global _plain_dir, _plain_bytes
    with _state_lock:
        directory, _plain_dir = _plain_dir, None
        _plain_sizes.clear()
        _plain_bytes = 0
    if not directory:
        return
    shutil.rmtree(directory, ignore_errors=True)
    if os.path.exists(directory):  # 删完校验：静默失败在这里必须变成一条日志
        _warn("明文目录未能清空（可能仍有文件被占用）：%s" % directory)


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

    def decrypt(self, src: str, dst: str, deadline: Optional[float] = None) -> None:
        """解密 ``src`` 到 ``dst``。

        ``deadline`` 是 :func:`time.monotonic` 刻度上的**绝对时限**：由调用方按
        「单文件总预算」算出，各通道据此裁自己的超时，避免三条通道各等满一次
        把添加过程拖到几分钟且无法取消。``None`` 表示不限。
        """
        raise DecryptError("该解密提供者未实现")

    def problem(self) -> str:
        """不可用时**给用户看的原因**（可用则返回空串）。

        「没有解密器」和「解密器配错了」在用户看来都是「加不进去」，但前者要找
        管理员、后者改一行配置就行 —— 必须区分。
        """
        return ""


def _supports_deadline(provider: "DecryptProvider") -> bool:
    """``provider.decrypt`` 是否接受 ``deadline`` 参数。

    内置的都接受；**第三方 / 测试替身**可能仍是老的两参数签名。按签名探测比
    "先试着传、捕获 TypeError" 稳妥 —— 后者会把 decrypt 内部抛的 TypeError
    误判成"不支持"再跑一次，副作用不可控。
    """
    try:
        import inspect
        return "deadline" in inspect.signature(provider.decrypt).parameters
    except (TypeError, ValueError):
        return False


def _call_decrypt(provider: "DecryptProvider", src: str, dst: str,
                  deadline: Optional[float]) -> None:
    """按提供者能力调用 decrypt（老签名自动降级为不带 deadline）。"""
    if _supports_deadline(provider):
        provider.decrypt(src, dst, deadline=deadline)
    else:
        # 老签名（第三方 / 测试替身）：只给两个位置参数，代价是它拿不到总预算
        provider.decrypt(src, dst)


def _budget_timeout(timeout: float, deadline: Optional[float], floor: float = 1.0) -> float:
    """把「本通道期望超时」与「剩余总预算」取小；预算耗尽则返回 ``floor``。"""
    if deadline is None:
        return timeout
    remaining = deadline - time.monotonic()
    if remaining <= floor:
        return floor
    return min(timeout, remaining)


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

    def problem(self) -> str:
        if self._argv:
            return ""
        return ("WM_DLP_DECRYPT_CMD 无法解析成命令行（%s）—— 多半是引号未闭合"
                % (self.template or "空"))

    def decrypt(self, src: str, dst: str, deadline: Optional[float] = None) -> None:
        if not self._argv:
            raise DecryptError(self.problem())
        ext = os.path.splitext(src)[1]
        work = os.path.join(plaintext_dir(), "s%s%s" % (uuid.uuid4().hex, ext))
        try:
            shutil.copy2(src, work)
        except OSError as exc:
            raise DecryptError(f"无法读取源文件（{exc}）") from exc
        before = _snapshot_dir()
        try:
            argv = [part.format(src=work, dst=dst, ext=ext.lstrip("."))
                    for part in self._argv]
            timeout = _budget_timeout(self.timeout, deadline)
            try:
                # 串行：外部解密器常常独占端口/临时文件名，并发必然互相踩
                with _call_lock:
                    completed = subprocess.run(
                        argv, cwd=plaintext_dir(), timeout=timeout,
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                        creationflags=_NO_WINDOW)
            except FileNotFoundError as exc:
                raise DecryptError(f"解密器不存在：{self._argv[0]}") from exc
            except subprocess.TimeoutExpired:
                raise DecryptError(f"解密超时（超过 {timeout:.0f} 秒）")
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
            # 解密器产生的**一切**中间产物都清掉，只留 dst（目录快照差集）
            _cleanup_new_files(before, dst)
            # 副本本身也在差集之外（快照是在它之后拍的）：解密器没动它时，
            # 每个文件都会在这里永久留一份密文副本，直到进程退出。
            release(work)


#: **所有**外部解密调用的串行锁。
#:
#: 不只是 LDDec：任何「固定端口 / 固定临时文件名」的解密器并发都会互相踩
#: （表现为「返回 0 但没有产物」这类极难排查的失败）。解密本身是秒级的，
#: 串行的代价远小于并发带来的不确定性。
_call_lock = threading.RLock()


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

    def decrypt(self, src: str, dst: str, deadline: Optional[float] = None) -> None:
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
        before = _snapshot_dir()
        try:
            self._read_plaintext(work, dst, _budget_timeout(self.timeout, deadline))
            if not os.path.isfile(dst):
                raise DecryptError(f"{self.label}没有产生任何输出")
            if os.path.getsize(dst) <= 0:
                raise DecryptError(f"{self.label}读到 0 字节（路径可能含非 ASCII 字符）")
        finally:
            _cleanup_new_files(before, dst)
            # 同上：快照拍在副本之后，必须显式删，否则每解一个文件留一份副本
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

    def _read_plaintext(self, work: str, dst: str, timeout: Optional[float] = None) -> None:
        cmd = _cmd_executable()
        timeout = timeout if timeout and timeout > 0 else self.timeout
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
                        timeout=timeout, creationflags=_NO_WINDOW)
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

    def _read_plaintext(self, work: str, dst: str, timeout: Optional[float] = None) -> None:
        timeout = timeout if timeout and timeout > 0 else self.timeout
        # 路径进单引号字符串：只需把单引号本身翻倍；其余字符（含中文、空格、
        # 方括号、$）在单引号里都是字面量，无需再转义。
        #
        # 落盘必须用 **WriteAllBytes**（内部 FileMode.Create，会截断）。早先用的
        # ``OpenWrite`` 不截断：一旦目标文件已存在且比本次产物长（例如上一次的
        # 明文因占用没删掉），尾部就会残留上一个文件的内容，而
        # ``verify_plaintext`` 只校验头部 —— 污染明文会被当成成功交付。
        script = (
            "[IO.File]::WriteAllBytes('" + dst.replace("'", "''") + "',"
            "[IO.File]::ReadAllBytes('" + work.replace("'", "''") + "'))"
        )
        release(dst)
        try:
            completed = subprocess.run(
                [_powershell_executable(), "-NoProfile", "-NonInteractive",
                 "-Command", script],
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                timeout=timeout, creationflags=_NO_WINDOW)
        except subprocess.TimeoutExpired:
            raise DecryptError(f"{self.label}超时（超过 {timeout:.0f} 秒）")
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

    def problem(self) -> str:
        if self.available():
            return ""
        reasons = [p.problem() for p in self.providers if not p.available()]
        return "；".join(r for r in reasons if r) or "所有解密通道在当前系统均不可用"

    def describe(self) -> str:
        ready = [p for p in self.providers if p.available()]
        return "、".join(_channel_label(p) for p in ready) or "无可用通道"

    def decrypt(self, src: str, dst: str, deadline: Optional[float] = None) -> None:
        if not self.providers:
            raise DecryptError("没有配置任何解密通道")
        notes: List[str] = []
        for provider in self.providers:
            if not provider.available():
                continue
            # 总预算已耗尽：后面的通道不用再试了，直接给出结论
            if deadline is not None and deadline - time.monotonic() <= 0:
                notes.append("已用完全部解密时间预算")
                break
            label = _channel_label(provider)
            started = time.monotonic()
            _trace("尝试 %s（剩余预算 %s）" % (
                label, "无限" if deadline is None
                else "%.1fs" % max(0.0, deadline - started)))
            try:
                _call_decrypt(provider, src, dst, deadline)
            except DecryptError as exc:
                notes.append(f"{label}：{exc}")
                _trace("  %s 失败（%.2fs）：%s" % (label, time.monotonic() - started, exc))
                release(dst)
                continue
            error = verify_plaintext(dst)
            if error is None:
                self.last_used = provider
                _trace("  %s **成功**（%.2fs）-> 本文件走这条通道"
                       % (label, time.monotonic() - started))
                return
            notes.append(f"{label}：{error}")
            _trace("  %s 产物不可用（%.2fs）：%s"
                   % (label, time.monotonic() - started, error))
            release(dst)
        if not notes:
            raise DecryptError(self.problem() or "没有可用的解密通道")
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

    def decrypt(self, src: str, dst: str, deadline: Optional[float] = None) -> None:
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
        before = _snapshot_dir()
        timeout = _budget_timeout(self.timeout, deadline)
        try:
            with _call_lock:  # 端口 34500 固定 / 临时产物同名，绝不能并发
                try:
                    completed = subprocess.run(
                        [self.exe, work], cwd=exe_dir, timeout=timeout,
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                        creationflags=_NO_WINDOW)
                except subprocess.TimeoutExpired:
                    raise DecryptError(
                        f"LDDec 解密超时（超过 {timeout:.0f} 秒）")
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
            _cleanup_new_files(before, dst)
            # dec.exe 的语义是就地覆盖，通常会把副本改名吃掉；但它失败/跳过时
            # 副本还在 —— 显式删一次，避免临时目录里堆密文副本
            release(work)


def _split_command(template: str) -> List[str]:
    """把命令模板切成 argv。

    用非 posix 模式切：posix 模式会把 Windows 路径里的反斜杠当转义符吃掉
    （``C:\\Tools\\dec.exe`` → ``C:Toolsdec.exe``）。代价是它**保留**引号，
    所以这里再手工剥掉成对的外层双引号 —— 否则带空格的程序路径会连引号一起被
    当成文件名。
    """
    try:
        tokens = shlex.split(template, posix=False)
    except ValueError as exc:
        # 引号未闭合时 shlex 抛 "No closing quotation"。这个异常过去会一路逃到
        # Tk 回调里，把**整个添加批次**打断（已成功解析的文件也不进列表）。
        # 切不出来就返回空表：available() 变 False、problem() 给出可行动的原因。
        _warn("解密命令模板无法解析（%s）：%s" % (exc, template))
        return []
    return [t[1:-1] if len(t) > 1 and t.startswith('"') and t.endswith('"') else t
            for t in tokens]


def _snapshot_dir() -> Set[str]:
    """明文目录的当前文件清单（解密器产出前后各拍一张，用于兜底清理）。"""
    try:
        return set(os.listdir(plaintext_dir()))
    except OSError:
        return set()


def _cleanup_new_files(before: Set[str], keep: str) -> None:
    """删掉解密器顺手留下的**一切**中间产物，只留 ``keep``。

    早先只清理 ``work`` / ``work.dec_`` / ``work_dec`` 三个硬编码名字 —— 换个
    解密器（或它改个后缀）就会把 ``.dec``、``.tmp`` 之类的残骸永久留在明文
    目录里。改成「目录快照差集」后与解密器实现无关，任何产物都跑不掉。
    """
    directory = plaintext_dir()
    keep_name = os.path.basename(keep)
    try:
        after = set(os.listdir(directory))
    except OSError:
        return
    for name in (after - before):
        if name == keep_name or name == _PID_FILE:
            continue
        release(os.path.join(directory, name))


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
    timeout = _env_float("WM_DLP_TIMEOUT")
    template = (os.environ.get("WM_DLP_DECRYPT_CMD") or "").strip()
    if template:
        return CommandProvider(template, timeout or DEFAULT_TIMEOUT)
    # ``WM_DLP_TIMEOUT`` 对内置链同样生效：文档写的是「单文件解密超时」，管理员
    # 设成 10 秒就是希望快速失败，不该因为没配命令模板而失效。
    if timeout and timeout > 0:
        chain: List[DecryptProvider] = []
        exe = find_lddec()
        if exe:
            chain.append(LddecProvider(exe, timeout))
        chain.append(CmdTypeProvider(timeout))
        chain.append(PowershellProvider(timeout))
        return ChainProvider(chain)
    chain = []
    exe = find_lddec()
    if exe:
        chain.append(LddecProvider(exe))
    chain.append(CmdTypeProvider())
    chain.append(PowershellProvider())
    return ChainProvider(chain)


def _env_float(name: str) -> Optional[float]:
    """读一个浮点环境变量；缺失或非法返回 ``None``（**并留痕**）。"""
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        _warn("%s 不是数字（%r），已忽略该设置" % (name, raw))
        return None


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


#: 已落盘的临时明文累计字节数（用于 M7 的总量上限；受 _state_lock 保护）
_plain_bytes = 0

#: 明文落盘时的字节数（``abspath -> size``），读取前拿来比对。
#:
#: 为什么需要：明文躺在临时目录里，可能被磁盘清理工具、杀软或用户自己删掉；
#: 若被**换成别的文件**，消费方照读不误，会静默产出错误内容。大小对不上拦不住
#: 所有替换，但能挡住最常见的一类，且成本只有一次 ``getsize``。
_plain_sizes: "Dict[str, int]" = {}


def plain_bytes() -> int:
    """当前临时明文占用的总字节数。"""
    with _state_lock:
        return _plain_bytes


def plaintext_problem(path: Optional[str]) -> Optional[str]:
    """读取**之前**校验一条明文路径是否还可用；``None`` 表示可用，否则是可直接
    展示给用户的原因。

    为什么必须由消费方（批处理 / 预览）来做（审计 M8）：这两处都是「取到路径就
    直接读」，失败时一个走裸 ``except Exception``、一个干脆返回 ``None``，于是
    「明文被删了」和「文件本身打不开」混成同一句没信息量的提示，日志里也没有
    任何线索。这里把两者分开，原因直接可行动（重新添加该文件）。
    """
    if not path:
        return "没有可读的文件内容"
    with _state_lock:
        expected = _plain_sizes.get(os.path.abspath(path))
    known = expected is not None          # 是否本模块产出的明文
    if not os.path.isfile(path):
        return ("临时明文已失效（文件被删除或清理），请重新添加该文件" if known
                else "文件不存在或已被移走，请重新添加")
    try:
        size = os.path.getsize(path)
    except OSError as exc:
        return "文件无法访问：%s" % exc
    if size <= 0:
        return ("临时明文为空（0 字节），解密可能未完成，请重新添加该文件" if known
                else "文件为空（0 字节），无法读取")
    if known and size != expected:
        return ("临时明文大小已变化（%d → %d 字节），可能被其它程序改写，"
                "请重新添加该文件" % (expected, size))
    return None


def max_plain_bytes() -> float:
    """明文总量上限（``WM_DLP_MAX_BYTES``，单位字节；默认 4GB）。"""
    configured = _env_float("WM_DLP_MAX_BYTES")
    if configured is None or configured <= 0:
        return float(DEFAULT_MAX_PLAIN_BYTES)
    return configured


def resolve(path: str, budget: Optional[float] = None) -> Resolution:
    """把一条路径解析成"可读路径"，必要时先解密。

    判定顺序（刻意保守，宁可多读一次也不误杀）：

    1. 头 3 字节不是加密魔数 → **原样返回**，零额外开销；
    2. 命中魔数 → 先**试着按原路径打开一次**：能打开说明当前进程读到的就是明文
       （白名单已生效，或只是魔数误判），同样原样返回；
    3. 打不开且**没有解密器** → 失败，原因写明"未配置解密器"；
    4. 打不开且有解密器 → 解密到临时目录 → 校验产物 → 成功返回临时明文路径，
       失败则清理产物并返回原因。

    ``budget`` 是**本文件的解密总时限**（秒）。三条通道各自 60/60/90 秒时最坏会
    卡 330 秒，用户既看不到进度也关不掉；总预算把时间切成「剩余可用量」分给
    每条通道，超了就放弃该文件转下一个。    ``None`` 用 :data:`DEFAULT_BUDGET`
    （可用 ``WM_DLP_BUDGET`` 覆盖，设为 0 表示不限）。
    """
    global _plain_bytes
    _started = time.monotonic()
    _trace("resolve 开始：%s（%.1f KB）" % (path, _safe_size(path) / 1024.0))
    if not is_encrypted(path):
        _trace("  未命中加密魔数 -> 原样使用（%.2fs）" % (time.monotonic() - _started))
        return Resolution(path, path, STATE_PLAIN)
    if probe_readable(path) is None:
        # 疑似加密但读得开：可能是透明解密已生效，也可能只是魔数撞上了
        _trace("  命中魔数但原路径可读 -> 直接读明文（%.2fs）"
               % (time.monotonic() - _started))
        return Resolution(path, path, STATE_DIRECT)
    provider = get_provider()
    if provider is None:
        return Resolution(
            None, None, STATE_FAILED,
            "文件受加密软件保护，当前环境无法解密（本机没有可用的解密组件）；"
            "请联系信息安全部门为水印工具授权")
    if not provider.available():
        return Resolution(
            None, None, STATE_FAILED,
            "文件受加密软件保护，且解密组件当前不可用 —— %s"
            % (provider.problem() or "请联系信息安全部门为水印工具授权"))
    # 明文必须常驻到文件移出列表，所以不能 LRU 淘汰；改为「到顶即停」并说清原因，
    # 免得一次拖几百个大文件把磁盘撑爆还不知道发生了什么。
    try:
        incoming = os.path.getsize(path)
    except OSError:
        incoming = 0
    if _plain_bytes + incoming > max_plain_bytes():
        return Resolution(
            None, None, STATE_FAILED,
            "已解密的临时内容超过上限（%.1f GB），为避免占满磁盘已停止解密该文件；"
            "请分批处理，或调大 WM_DLP_MAX_BYTES" % (max_plain_bytes() / 1024 ** 3))
    if budget is None:
        configured = _env_float("WM_DLP_BUDGET")
        budget = DEFAULT_BUDGET if configured is None else configured
    deadline = None if budget and budget <= 0 else time.monotonic() + budget
    dst = new_plaintext_path(path)
    try:
        _call_decrypt(provider, path, dst, deadline)
    except DecryptError as exc:
        _trace("  全部通道失败（%.2fs）：%s" % (time.monotonic() - _started, exc))
        release(dst)
        return Resolution(None, None, STATE_FAILED, str(exc))
    except Exception as exc:  # 解密器实现可能有未预料的行为，不能让它带崩添加流程
        _trace("  解密异常（%.2fs）：%s: %s"
               % (time.monotonic() - _started, type(exc).__name__, exc))
        release(dst)
        return Resolution(None, None, STATE_FAILED, f"解密异常：{type(exc).__name__}: {exc}")
    error = verify_plaintext(dst)
    if error:
        _trace("  产物校验不通过（%.2fs）：%s" % (time.monotonic() - _started, error))
        release(dst)
        return Resolution(None, None, STATE_FAILED, error)
    with _state_lock:
        try:
            got = os.path.getsize(dst)
        except OSError:
            got = 0
        _plain_bytes += got
        _plain_sizes[os.path.abspath(dst)] = got  # 供 plaintext_problem 比对
    _trace("  resolve 完成：decrypted（%.2fs，明文 %d 字节）"
           % (time.monotonic() - _started, got))
    return Resolution(path, dst, STATE_DECRYPTED)


def resolve_all(
    paths: List[str],
    budget: Optional[float] = None,
    is_cancelled: "Optional[object]" = None,
) -> Tuple[List[Resolution], List[Resolution]]:
    """批量解析：返回 ``(可用结果, 失败结果)`` —— 失败项**不影响**其它文件。

    ``is_cancelled`` 可选，是个 ``() -> bool``：返回 True 就**立刻停止**后续文件
    （已解析的结果照常返回）。关窗、用户点取消都要能中断这条同步链路，否则拖
    20 个加密文件就是几十分钟无响应。
    """
    usable: List[Resolution] = []
    failed: List[Resolution] = []
    cancelled = is_cancelled if callable(is_cancelled) else None
    for path in paths:
        if cancelled is not None and cancelled():
            break
        try:
            result = resolve(path, budget=budget)
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
