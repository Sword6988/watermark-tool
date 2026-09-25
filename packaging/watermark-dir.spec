# -*- mode: python ; coding: utf-8 -*-
"""目录版 spec：dist/WatermarkTool/WatermarkTool.exe + _internal。

构建：python -m PyInstaller packaging/watermark-dir.spec --noconfirm
注意：不要加 --clean（build/_obsolete 里存放着被移走的旧产物）。
"""
import os
import sys

sys.path.insert(0, SPECPATH)
import spec_common as SC  # noqa: E402

SC.check_environment()
datas, binaries = SC.collect_assets()

a = Analysis(
    [os.path.join(SC.PROJECT_ROOT, "main.py")],
    pathex=[SC.PROJECT_ROOT],
    binaries=binaries,
    datas=datas,
    hiddenimports=SC.HIDDEN_IMPORTS,
    excludes=SC.EXCLUDES,
    noarchive=False,
    optimize=1,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,   # 目录版：运行时由 _internal 提供
    name=SC.APP_NAME,
    console=False,           # 窗口程序；stdout 为 None 已在 main.install_stdio 处理
    icon=os.path.join(SC.PROJECT_ROOT, "assets", "app.ico"),
)
coll = COLLECT(exe, a.binaries, a.datas, name=SC.APP_NAME)
