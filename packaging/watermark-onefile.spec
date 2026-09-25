# -*- mode: python ; coding: utf-8 -*-
"""单文件版 spec：dist/WatermarkTool.exe（一个文件，无 _internal 旁挂目录）。

构建：python -m PyInstaller packaging/watermark-onefile.spec --noconfirm

与目录版（watermark-dir.spec）的差异只有最后一步：
* 目录版：EXE(exclude_binaries=True) + COLLECT -> exe 与 _internal 分开放；
* 单文件版：binaries/datas **全部塞进 EXE**，不生成 COLLECT。

运行机制（必须写进交付说明，否则用户会以为是 bug）：
* 每次启动把整包解压到 ``%TEMP%\\_MEIxxxxxx``（本包约 70MB 内容），
  首次/冷启动因此比目录版慢几秒，退出时自动清理；
* 解压目录即 ``sys._MEIPASS``，main.module_root() 走的正是它，
  图标 / tkdnd / pymupdf 的 DLL 都从那里加载，**源码无需任何改动**。

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
    a.binaries,
    a.datas,
    [],
    name=SC.APP_NAME,
    console=False,           # 窗口程序；stdout 为 None 已在 main.install_stdio 处理
    icon=os.path.join(SC.PROJECT_ROOT, "assets", "app.ico"),
)
