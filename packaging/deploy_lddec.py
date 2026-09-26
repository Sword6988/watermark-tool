"""部署 LDDec 解密通道到 ``tools/LDDec/``。

水印工具**内置**了 LDDec 的调用适配器（``wm.dlp.LddecProvider``）：只要程序目录下
能找到 ``dec.exe``，添加加密文件时就会自动调用它，无需任何配置。本脚本负责把
LDDec 的**运行文件**放到工具认得的位置。

为什么不随仓库自带 dec.exe：
    * 仓库里的 ``dec.exe`` 是第三方**预编译二进制**，未经审计，不适合盲发到内网；
    * 它动态链接 Qt，缺 Qt 运行库直接跑不起来，放进去也是坏包；
    * Faker 进程（默认名 notepad.exe）是否部署、叫什么名字，属于**贵司信息安全
      策略**范畴，应由管理员决定，不该由本工具替你决定。

所以正确姿势是：从贵司已审计/已部署的 LDDec 拷贝过来，或用源码自行编译后再拷贝。

用法：
    python packaging/deploy_lddec.py --from D:\\Tools\\LDDec     # 从已有目录部署
    python packaging/deploy_lddec.py --from D:\\Tools\\dec.exe    # 只给一个 exe 也行
    python packaging/deploy_lddec.py --check                      # 只检查当前状态
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEST = os.path.join(PROJECT_ROOT, "tools", "LDDec")

# 从 packaging/ 目录直接跑时项目根不在 sys.path 上，--check 要 import wm.dlp
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

#: dec.exe 运行必需 / 常见的伴生文件（源码 ``Common/Setting.cpp`` 与
#: ``Dec/main_dec.cpp`` 决定：同目录必须有 config.json，且要能启动 Faker 进程）
DEFAULT_CONFIG = {"port": 34500, "faker": "notepad.exe"}


def _is_runtime_file(name: str) -> bool:
    """是否属于 LDDec 运行期需要的文件。

    整目录拷贝、只排除明显的杂项：dec.exe 靠 ``applicationDirPath()`` 找 config.json
    与 Faker，而 Faker 常被改名成 notepad.exe（源码默认值），Qt 的 DLL 也在同目录。
    宁可多拷几个文件，也不要漏掉导致"dec.exe 跑了但没反应"。
    """
    lower = name.lower()
    if lower.startswith("."):
        return False
    if lower.endswith((".md", ".txt", ".gitignore", ".pro", ".sln", ".vcxproj",
                       ".filters", ".cpp", ".h", ".user")):
        return False
    return True


def deploy(source: str, force: bool = False) -> int:
    if os.path.isfile(source):
        source_dir, files = os.path.dirname(source) or ".", [os.path.basename(source)]
    elif os.path.isdir(source):
        source_dir = source
        files = [f for f in os.listdir(source) if _is_runtime_file(f)]
    else:
        print(f"[FAIL] 源路径不存在：{source}")
        return 1

    if not files:
        print(f"[FAIL] 源目录里没有可部署的文件：{source}")
        return 1

    if os.path.isdir(DEST) and not force:
        print(f"[FAIL] 目标已存在：{DEST}（加 --force 覆盖）")
        return 1
    if os.path.isdir(DEST) and force:
        shutil.rmtree(DEST, ignore_errors=True)
    os.makedirs(DEST, exist_ok=True)

    copied = []
    for name in sorted(files):
        src = os.path.join(source_dir, name)
        if not os.path.isfile(src):
            continue
        shutil.copy2(src, os.path.join(DEST, name))
        copied.append(name)
    print(f"[OK] 已复制 {len(copied)} 个文件 -> {DEST}")
    for name in copied:
        print(f"       {name}")

    config_path = os.path.join(DEST, "config.json")
    if not os.path.isfile(config_path):
        with open(config_path, "w", encoding="utf-8") as handle:
            json.dump(DEFAULT_CONFIG, handle, ensure_ascii=False, indent=2)
        print("[WARN] 源里没有 config.json，已按 LDDec 源码默认值生成：")
        print(f"       {json.dumps(DEFAULT_CONFIG, ensure_ascii=False)}")
        print("       **请按贵司实际的读取进程名修改 faker 字段**")

    exe = os.path.join(DEST, "dec.exe")
    if not os.path.isfile(exe):
        print("[WARN] 目标目录里没有 dec.exe —— 工具不会启用解密通道")
        return 1
    print("[OK] dec.exe 就位")

    if not any(n.lower().endswith(".exe") and n.lower() != "dec.exe" for n in copied):
        print("[WARN] 目录里只有 dec.exe，没有 Faker 进程（config.json 的 faker 字段"
              "指向的那个可执行文件）。缺了它 dec.exe 会报「Faker 进程没能连上」。")

    dlls = [n for n in copied if n.lower().endswith(".dll")]
    if not dlls:
        print("[WARN] 目录里没有 Qt 运行库（Qt5Core.dll 等）。若 dec.exe 是动态链接"
              " Qt 编译的，缺库会直接启动失败。")
    return 0


def check() -> int:
    from wm import dlp

    exe = dlp.find_lddec()
    print("程序目录:", dlp.app_dir())
    print("搜索位置:", [os.path.join(dlp.app_dir(), d or ".") for d in dlp.LDDEC_SEARCH_DIRS])
    if not exe:
        print("[--] 未找到 dec.exe -> 解密通道不会启用（不会影响普通文件）")
        return 1
    print("[OK] 找到 dec.exe:", exe)
    config = os.path.join(os.path.dirname(exe), "config.json")
    if os.path.isfile(config):
        try:
            with open(config, encoding="utf-8") as handle:
                print("[OK] config.json:", json.load(handle))
        except Exception as exc:
            print(f"[WARN] config.json 读不出来：{exc}")
    else:
        print("[FAIL] 缺少 config.json -> dec.exe 无法启动读取进程")
        return 1
    provider = dlp.LddecProvider(exe)
    print("[OK] 解密通道可用:", provider.describe(),
          f"（超时 {provider.timeout:.0f}s，TCP {dlp.LDDEC_PORT}）")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="部署 LDDec 解密通道")
    parser.add_argument("--from", dest="source", help="已有的 dec.exe 或 LDDec 目录")
    parser.add_argument("--check", action="store_true", help="只检查当前部署状态")
    parser.add_argument("--force", action="store_true", help="覆盖已存在的目标目录")
    args = parser.parse_args()

    if args.check:
        return check()
    if not args.source:
        parser.print_help()
        return 1
    return deploy(args.source, force=args.force)


if __name__ == "__main__":
    sys.exit(main())
