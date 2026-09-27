"""在内网真机上逐通道探测解密链路：看**哪条通道真的解出明文**、**各花多久**。

它回答审计里那两个"端到端通过了但仍未知"的问题：

1. 三���通道（LDDec / ``cmd /c type`` / PowerShell）里到底是哪条解的；
2. 单文件解密耗时多少（因此拖 20 个文件要等多久）。

用法 —— 只要标准库，不需要装 Pillow / PyMuPDF：

    python tools/probe_dlp_channels.py "D:\\Desktop\\PLM验收单.pdf"
    python tools/probe_dlp_channels.py a.pdf b.png        # 多个文件
    set WM_DLP_TRACE=1 && python tools/probe_dlp_channels.py a.pdf
                                                          # 连链内部每一次尝试都打印

**拷到内网机器最少只要两个文件**：把 ``wm/dlp.py`` 和本脚本放在同一个目录，
直接跑即可（脚本会自动按路径加载 dlp.py，不必带整个仓库）。

输出里最该关注的三行：
    [通道] LDDec …           每条通道被**强制**单独试一次的结果与耗时
    界面实际会走：…          按回退顺序第一个成功的通道
    原文件哈希：… 未被改写 = True   硬契约复核（永远不改写原文件）

脚本只把明文写进 dlp 自己的临时目录并在结束时清理，不碰任何原文件。
"""

from __future__ import annotations

import hashlib
import importlib.util
import os
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
# 让「仓库根目录在上一级」时也能 `from wm import dlp`
sys.path.insert(0, os.path.dirname(_HERE))


def _load_dlp():
    """拿到 dlp 模块：优先走包，找不到就按文件路径直接加载（便于单文件带走）。"""
    try:
        from wm import dlp as module
        return module, "wm.dlp"
    except Exception:
        pass
    for candidate in (
        os.path.join(_HERE, "dlp.py"),
        os.path.join(_HERE, "wm", "dlp.py"),
        os.path.join(os.path.dirname(_HERE), "wm", "dlp.py"),
    ):
        if os.path.isfile(candidate):
            spec = importlib.util.spec_from_file_location("_wm_dlp_probe", candidate)
            module = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = module
            spec.loader.exec_module(module)
            return module, os.path.normpath(candidate)
    raise SystemExit(
        "[错误] 找不到 wm/dlp.py。请把本脚本与 wm/dlp.py 放在同一目录，"
        "或从仓库根目录运行。")


dlp, DLP_FROM = _load_dlp()


def sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def head(path: str, n: int = 16) -> bytes:
    try:
        with open(path, "rb") as handle:
            return handle.read(n)
    except OSError as exc:
        return ("<读取失败: %s>" % exc).encode("utf-8", "replace")


def try_channel(provider, src: str) -> str:
    """单独试一条通道，返回一行结论（含耗时）。"""
    dst = dlp.new_plaintext_path(src)
    started = time.time()
    try:
        provider.decrypt(src, dst)
    except dlp.DecryptError as exc:
        dlp.release(dst)
        return "失败（%.2fs）：%s" % (time.time() - started, exc)
    except Exception as exc:  # 通道自身的实现问题也要暴露，不能静默
        dlp.release(dst)
        return "异常（%.2fs）：%s: %s" % (time.time() - started, type(exc).__name__, exc)
    if not os.path.isfile(dst):
        return "失败（%.2fs）：没有产出文件" % (time.time() - started)
    size = os.path.getsize(dst)
    error = dlp.verify_plaintext(dst)
    elapsed = time.time() - started
    if error:
        dlp.release(dst)
        return "产物不可用（%.2fs，%d 字节）：%s" % (elapsed, size, error)
    with open(dst, "rb") as handle:
        preview = handle.read(16)
    dlp.release(dst)
    return "成功（%.2fs，%d 字节，头=%s）" % (elapsed, size, preview)


def probe(path: str, repeat: int) -> None:
    print("=" * 74)
    print("文件：" + path)
    if not os.path.isfile(path):
        print("  [错误] 文件不存在")
        return
    print("  大小       ：%.1f KB" % (os.path.getsize(path) / 1024.0))
    print("  文件头     ：%s" % head(path))
    print("  判为加密   ：%s" % dlp.is_encrypted(path))
    readable = dlp.probe_readable(path)
    if readable is None:
        print("  按原路径打开：可以（读到的就是明文，说明已在白名单里）")
    elif "文件解析模块不可用" in readable:
        # 只带 dlp.py 时 wm.media 导入不了（它依赖 PyMuPDF/Pillow），这条判据
        # 必然失败 —— 是**探针的运行方式**造成的，不是本机的真实行为。
        print("  按原路径打开：%s" % readable)
        print("                ^ 只带 dlp.py 时的正常现象（缺 wm.media），"
              "**不代表 exe 里的行为**：真实 exe 会真的试着打开一次")
    else:
        print("  按原路径打开：失败 —— %s" % readable)

    before = sha256(path)

    provider = dlp.get_provider()
    print("  解密器     ：%s" % (provider.describe() if provider else "无（未启用）"))
    if provider is None:
        print("  >> 没有可用解密通道：确认 tools/LDDec/dec.exe 是否存在，"
              "或是否设了 WM_DLP_ENABLE=0")
        return

    exe = dlp.find_lddec()
    print("  dec.exe    ：%s" % (exe or "未找到（LDDec 通道不参与）"))
    if not exe:
        print("                ^ 想单独验证 LDDec 通道，二选一：把 dec.exe 放进")
        print("                  <本目录>/wm/ 的上一级（即与本脚本同级），")
        print("                  或 set WM_LDDEC_EXE=<dec.exe 完整路径> 后重跑")

    channels = list(getattr(provider, "providers", [provider]))
    print("  ---- 逐通道强制单独尝试（顺序 = 回退优先级）----")
    winner = None
    for index, channel in enumerate(channels, 1):
        label = dlp._channel_label(channel)
        if not channel.available():
            print("    %d. [%s] 当前系统不可用 —— %s"
                  % (index, label, channel.problem() or "原因未知"))
            continue
        line = try_channel(channel, path)
        print("    %d. [%s] %s" % (index, label, line))
        if winner is None and line.startswith("成功"):
            winner = label

    print("  ---- 整体 resolve（界面实际走的路径）----")
    timings = []
    for run in range(repeat):
        started = time.time()
        result = dlp.resolve(path)
        timings.append(time.time() - started)
        if result.state == dlp.STATE_DECRYPTED and result.read_path:
            # 明文要等文件移出列表才释放；探针这里**每次**都用完即还，
            # 免得"残留"数字随 --repeat 虚高，被误读成清理不干净
            dlp.release(result.read_path)
        if run == 0:
            used = getattr(provider, "last_used", None)
            print("    状态     ：%s" % result.state)
            print("    读取路径 ：%s" % result.read_path)
            if result.message:
                print("    说明     ：%s" % result.message)
            if used is not None:
                print("    **本文件由 [%s] 解出**" % dlp._channel_label(used))
    print("    耗时     ：%s（第 1 次通常含冷启动，后面是稳态）"
          % "、".join("%.2fs" % t for t in timings))
    if winner and getattr(provider, "last_used", None) is not None:
        actual = dlp._channel_label(provider.last_used)
        if actual != winner:
            print("    [注意] 单独试的首个成功通道是 %s，实际走的是 %s —— "
                  "顺序或超时设置可能有差异" % (winner, actual))

    after = sha256(path)
    print("  原文件哈希：%s -> %s | 未被改写 = %s" % (
        before[:16], after[:16], before == after))

    plain_dir = dlp.plaintext_dir()
    names = os.listdir(plain_dir) if os.path.isdir(plain_dir) else []
    left = [n for n in names if n != dlp._PID_FILE]
    print("  明文目录  ：%s" % plain_dir)
    print("  残留      ：%d（%s）" % (
        len(left), "、".join(sorted(left)) if left
        else "无 —— 中间产物清理干净了"))


def main(argv) -> int:
    args = [a for a in argv[1:] if a]
    repeat = 2
    if "--repeat" in args:
        at = args.index("--repeat")
        try:
            repeat = max(1, int(args[at + 1]))
        except (IndexError, ValueError):
            print("[错误] --repeat 后面要跟一个正整数")
            return 2
        del args[at:at + 2]
    if not args:
        print(__doc__)
        return 2
    print("解密链路探针 —— dlp 来源：%s" % DLP_FROM)
    provider = dlp.get_provider()
    print("通道清单：%s" % (provider.describe() if provider else "无"))
    print("魔数：%s" % dlp.magic().hex(" "))
    print("总预算：%s" % (os.environ.get("WM_DLP_BUDGET") or dlp.DEFAULT_BUDGET))
    for path in args:
        probe(path, repeat)
    dlp.release_all()
    print("=" * 74)
    print("完成。把上面这段输出整段发回来即可定位。")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
