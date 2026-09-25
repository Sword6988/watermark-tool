# -*- coding: utf-8 -*-
"""打包公共配置：环境校验 + 资产收集（供 .spec 与 build.py 共用）。

关键点（踩坑记录）：
* tkdnd 库要按 **构建机的 Tcl 版本**二选一：``TclVersion >= 9`` 收 ``win-x64-tcl9``，
  ``8.6`` 收 ``win-x64``（``NEED_TCL9`` 自动判定，已验证 8.6 / 9.0 两条路都能出可拖拽的包）。
  PyInstaller 的 contrib hook 只收**非 tcl9** 目录，Tcl9 机器漏了这步会让拖拽**静默失效**。
* PyMuPDF 的原生 DLL（``mupdfcpp64.dll`` 等）靠依赖分析自动进 ``_internal/pymupdf/``，
  构建后必须存在，否则到运行时才报 DLL 缺失。
"""
import os
import sys

SPECDIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(SPECDIR)

APP_NAME = "WatermarkTool"
APP_VERSION = "1.0.0"

# Tcl/Tk >= 9 时 tkdnd 走 *-tcl9 目录
import tkinter  # noqa: E402  (延迟到使用处也行，这里直接用)

NEED_TCL9 = float(tkinter.TclVersion) >= 9.0


def check_environment() -> None:
    """构建前校验：任何一项缺失都应该立即失败，而不是打出坏包。"""
    import PyInstaller

    print("[OK] PyInstaller %s" % PyInstaller.__version__)

    try:
        import tkinterdnd2
    except ImportError as exc:
        raise SystemExit("[FAIL] 构建 venv 缺少 tkinterdnd2: %r" % exc)
    tkdnd_pkg = os.path.join(os.path.dirname(tkinterdnd2.__file__), "tkdnd")
    base = {
        "AMD64": "win-x64",
        "x86": "win-x86",
        "ARM64": "win-arm64",
    }[os.environ.get("PROCESSOR_ARCHITECTURE", "AMD64")]
    sub = ("win-x64-tcl9" if NEED_TCL9 else base)
    src = os.path.join(tkdnd_pkg, sub)
    if not os.path.isdir(src):
        raise SystemExit("[FAIL] tkdnd 目录缺失: %s" % src)
    print("[OK] tkdnd %s (%d 个文件)" % (sub, len(os.listdir(src))))

    import pymupdf

    pkg = os.path.dirname(pymupdf.__file__)
    dlls = [f for f in os.listdir(pkg) if f.endswith((".pyd", ".dll"))]
    if not any("mupdf" in f for f in dlls):
        raise SystemExit("[FAIL] pymupdf 原生 DLL 未找到: %s" % dlls)
    print("[OK] pymupdf 原生库: %s" % dlls)


def collect_assets():
    """返回 (datas, binaries)：tkdnd 的 tcl9 目录（DLL 走 binaries、.tcl 走 datas）。"""
    import tkinterdnd2

    tkdnd_pkg = os.path.join(os.path.dirname(tkinterdnd2.__file__), "tkdnd")
    base = {
        "AMD64": "win-x64",
        "x86": "win-x86",
        "ARM64": "win-arm64",
    }[os.environ.get("PROCESSOR_ARCHITECTURE", "AMD64")]
    subs = ["%s-tcl9" % base] if NEED_TCL9 else [base]

    datas, binaries = [], []
    for sub in subs:
        src_dir = os.path.join(tkdnd_pkg, sub)
        dest = os.path.join("tkinterdnd2", "tkdnd", sub)
        for name in sorted(os.listdir(src_dir)):
            full = os.path.join(src_dir, name)
            if name.endswith(".dll"):
                binaries.append((full, dest))
            else:
                datas.append((full, dest))
    print("[OK] 收集 tkdnd: %s -> %s" % (subs, dest))

    # 应用图标：标题栏 / 任务栏都要用，冻结后从 sys._MEIPASS/assets 读。
    # 缺失直接构建失败 —— 否则会打出「标题栏还是 Tk 羽毛图标」的坏包。
    assets_dir = os.path.join(PROJECT_ROOT, "assets")
    for name in ("app.ico", "app_256.png"):
        full = os.path.join(assets_dir, name)
        if not os.path.isfile(full):
            raise SystemExit("[FAIL] 缺少图标资产: %s（先跑 packaging/make_icon.py）" % full)
        datas.append((full, "assets"))
    print("[OK] 收集图标: assets/app.ico + assets/app_256.png")
    return datas, binaries


# 本工具只用 tkinter / tkinterdnd2 / PIL / pymupdf + 标准库。
# numpy 只被 tests 用到；lxml 无任何依赖引用；ssl/hashlib 是纯本地工具用不到的
# 大件（连同 libcrypto-3.dll 一起省 ~7MB）。fontTools 是 pymupdf subset_fonts()
# 的懒加载依赖，本工具不调用该功能（缺失时有优雅降级提示），不收。
#
# 注：这里**故意不再收** ``fitz``。自 PyMuPDF 1.24.3 起 ``fitz`` 只是个已弃用的
# 兼容 shim，全仓库已统一为 ``import pymupdf as fitz``（见 AUDIT.md P1-7）。
# 留着它的唯一后果是 PyInstaller 分析阶段会真的 import 一次 shim，在构建日志里
# 打出 ``warning: The 'fitz' API is deprecated...``，并把 shim 本身打进产物 ——
# 运行时没有任何代码路径会用到它。
HIDDEN_IMPORTS = [
    "tkinterdnd2",
    "pymupdf",
]

EXCLUDES = [
    "numpy",
    "lxml",
    "ssl",
    "_ssl",
    "hashlib",
    "_hashlib",
    "curses",
    "_curses",
    "fontTools",
    "PIL._avif",
]
