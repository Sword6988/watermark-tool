"""在本机**模拟中文机器**，验证非 ASCII 路径会不会打断解密链路。

没有中文用户名的机器也能验：把"中文原文件 / 中文临时目录 / 中文 dec.exe 路径"
三种情形各造一遍，看每条通道是照常产出还是静默读不到。

    python tools/probe_cn_path.py

（2026-09-27 从 ``_smoke/`` 移到 ``tools/``：``_smoke/`` 被 .gitignore 排除，
拷不完整目录就到不了内网；要验的正是中文机器，必须能单独拷走。）

判定口径（本机没有 DLP，所以**不会出现"解出明文"**）：看通道有没有**正常产出
字节**。产出 0 字节 / 无产物 = 路径把它绊倒了；产出等长密文 = 机械上跑通了，
只差授权。
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from wm import dlp  # noqa: E402

CN_ROOT = os.path.join(tempfile.gettempdir(), "中文环境_测试", "加解密 目录")
CN_EXE_DIR = os.path.join(CN_ROOT, "解密器 目录")


def make_sample(path: str, size: int = 4096) -> str:
    """写一个「假加密文件」：魔数 + PDF 头 + 填充。"""
    blob = dlp.magic() + b"%PDF-1.6\n%\xe2\xe3\xcf\xd3\n" + b"x" * size
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as handle:
        handle.write(blob)
    return path


def run_one(provider, src: str, label: str) -> None:
    dst = dlp.new_plaintext_path(src)
    try:
        provider.decrypt(src, dst)
    except dlp.DecryptError as exc:
        print("    %-22s 失败：%s" % (label, exc))
        return
    except Exception as exc:
        print("    %-22s 异常：%s: %s" % (label, type(exc).__name__, exc))
        return
    if not os.path.isfile(dst):
        print("    %-22s 无产物（路径问题？）" % label)
        return
    size = os.path.getsize(dst)
    dlp.release(dst)
    print("    %-22s 产出 %d 字节（源文件 %d 字节）%s" % (
        label, size, os.path.getsize(src),
        "" if size else "  <== 0 字节，被路径绊倒了"))


def main() -> int:
    os.makedirs(CN_ROOT, exist_ok=True)
    src_cn = make_sample(os.path.join(CN_ROOT, "PLM验收单 副本.pdf"))

    print("=" * 74)
    print("模拟中文环境：%s" % CN_ROOT)
    print("  TEMP 是否 ASCII：%s" % tempfile.gettempdir().isascii())

    print("\n[1] 中文**原文件**路径（临时目录仍是 ASCII）—— 最最常见的情形")
    run_one(dlp.CmdTypeProvider(), src_cn, "cmd 读取")
    run_one(dlp.PowershellProvider(), src_cn, "PowerShell 读取")

    print("\n[2] 中文**临时目录**（模拟中文用户名，且找不到任何 ASCII 根）")
    saved = dlp._ascii_temp_root
    dlp._ascii_temp_root = lambda: CN_ROOT  # 强制落在中文目录里
    dlp._plain_dir = None
    try:
        work_dir = dlp.plaintext_dir()
        print("    明文目录：%s（ASCII=%s）" % (work_dir, work_dir.isascii()))
        run_one(dlp.CmdTypeProvider(), src_cn, "cmd 读取")
        run_one(dlp.PowershellProvider(), src_cn, "PowerShell 读取")
    finally:
        dlp._ascii_temp_root = saved
        dlp._plain_dir = None

    print("\n[3] 中文 **dec.exe 路径 + 中文 cwd**")
    os.makedirs(CN_EXE_DIR, exist_ok=True)
    src_exe = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "tools", "LDDec", "dec.exe")
    cn_exe = os.path.join(CN_EXE_DIR, "dec.exe")
    if os.path.isfile(src_exe):
        shutil.copy2(src_exe, cn_exe)
        run_one(dlp.LddecProvider(cn_exe), src_cn, "LDDec（中文路径）")
    else:
        print("    跳过：仓库里没有 tools/LDDec/dec.exe")

    print("\n[4] **非 GBK 字符**的临时目录（emoji / 土耳其文）—— cmd 的真正破坏点")
    odd_root = os.path.join(tempfile.gettempdir(), "\U0001f512非GBK_İş\u0020目录")
    os.makedirs(odd_root, exist_ok=True)
    odd_src = make_sample(os.path.join(odd_root, "验收单\U0001f512.pdf"), 2000)
    dlp._ascii_temp_root = lambda: odd_root
    dlp._plain_dir = None
    try:
        print("    明文目录：%s" % dlp.plaintext_dir())
        run_one(dlp.CmdTypeProvider(), odd_src, "cmd 读取")
        run_one(dlp.PowershellProvider(), odd_src, "PowerShell 读取")
    finally:
        dlp._ascii_temp_root = saved
        dlp._plain_dir = None

    print("\n[5] **最坏情况**：明文目录也是非 ASCII（dec.exe 内部 type 要读中文路径）")
    dlp._ascii_temp_root = lambda: CN_ROOT
    dlp._plain_dir = None
    try:
        print("    明文目录：%s" % dlp.plaintext_dir())
        if os.path.isfile(cn_exe):
            run_one(dlp.LddecProvider(cn_exe), src_cn, "LDDec（中文明文）")
        run_one(dlp.CmdTypeProvider(), src_cn, "cmd 读取（中文明文）")
        run_one(dlp.PowershellProvider(), src_cn, "PowerShell 读取")
    finally:
        dlp._ascii_temp_root = saved
        dlp._plain_dir = None

    print("\n[6] 整体 resolve（中文原文件 + ASCII 临时目录）")
    result = dlp.resolve(src_cn)
    print("    状态：%s | 说明：%s" % (result.state, result.message or "-"))

    dlp.release_all()
    print("=" * 74)
    return 0


if __name__ == "__main__":
    sys.exit(main())
