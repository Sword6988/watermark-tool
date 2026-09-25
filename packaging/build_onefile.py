# -*- coding: utf-8 -*-
"""单文件版打包脚本：构建 -> 自检验收 -> GUI 冒烟 -> md5 清单。

用法（必须用**装了 PyInstaller + 本项目依赖**的解释器，路径随机器而异，
必须含 tkinter —— 托管 Python 通常没有）：

    "C:\\Users\\Administrator\\AppData\\Local\\Programs\\Python\\Python313\\python.exe" packaging/build_onefile.py

产物：dist/WatermarkTool.exe（单文件，双击即用，无 _internal）

铁律：清理旧产物用「移走」（os.rename 到 build/_obsolete），绝不 rmtree ——
本机存在批量删除保护（阈值 50/回合），rmtree 会让脚本无声卡住。
"""
import hashlib
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

import build as B  # noqa: E402  复用目录版脚本的 move_aside / run / prune_obsolete
import spec_common as SC  # noqa: E402

DIST = B.DIST
BUILD = B.BUILD
EXE = os.path.join(DIST, "%s.exe" % SC.APP_NAME)
SPEC = os.path.join(HERE, "watermark-onefile.spec")


def md5(path: str) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> int:
    t0 = time.time()
    B.prune_obsolete()

    # 1) 环境校验
    SC.check_environment()

    # 2) 旧产物移走（绝不 rmtree）
    B.move_aside(EXE)
    B.move_aside(os.path.join(BUILD, "pyi-one"))

    # 3) 构建
    B.run([sys.executable, "-m", "PyInstaller", SPEC, "--noconfirm",
           "--distpath", DIST, "--workpath", os.path.join(BUILD, "pyi-one")])

    # 4) 产物形态断言：必须是**单文件**，且旁边没有 _internal
    if not os.path.isfile(EXE):
        raise SystemExit("[FAIL] 未生成单文件 %s" % EXE)
    if os.path.exists(os.path.join(DIST, "_internal")):
        raise SystemExit("[FAIL] dist/_internal 存在，说明打成了目录版")
    print("[OK] 单文件产物: %s (%.1f MB)" % (EXE, os.path.getsize(EXE) / 1e6))

    # 5) 自检验收（同目录版：退出码 + 报告无 [FAIL] 双重判定）
    accept_dir = os.path.join(BUILD, "_accept_one")
    proc = subprocess.run([EXE, "--selftest", accept_dir], timeout=900)
    report = os.path.join(accept_dir, "selftest_report.txt")
    text = open(report, encoding="utf-8").read() if os.path.exists(report) else ""
    print(text)
    if proc.returncode != 0 or "[FAIL]" in text:
        raise SystemExit("[FAIL] 单文件版自检未通过，拒绝交付")
    print("[OK] 单文件版自检 PASS")

    # 6) GUI 冒烟：单文件每次启动要先自解压 ~70MB，给 15 秒仍存活即视为未崩溃
    gui = subprocess.Popen([EXE], cwd=DIST)
    time.sleep(15)
    if gui.poll() is not None:
        raise SystemExit("[FAIL] GUI 启动即退出（退出码 %s），查 %%LOCALAPPDATA%%\\%s\\logs"
                         % (gui.returncode, SC.APP_NAME))
    gui.terminate()
    gui.wait(timeout=20)
    print("[OK] GUI 冒烟通过（15 秒存活，含自解压时间）")

    # 7) 交付说明 + md5 清单
    doc = os.path.join(DIST, "单文件版说明.txt")
    with open(doc, "w", encoding="utf-8") as f:
        f.write(USAGE.format(app=SC.APP_NAME, ver=SC.APP_VERSION))
    dig = md5(EXE)
    with open(os.path.join(DIST, "单文件版-md5.txt"), "w", encoding="utf-8") as f:
        f.write("%s  %s\n" % (dig, os.path.basename(EXE)))
    print("[OK] md5 = %s" % dig)

    print()
    print("=" * 56)
    print("[DONE] %.1f 秒" % (time.time() - t0))
    print("  单文件: %s (%.1f MB)" % (EXE, os.path.getsize(EXE) / 1e6))
    print("  说明:   %s" % doc)
    return 0


USAGE = """{app} {ver} —— 图片 / PDF 文字水印工具（单文件版）
============================================================

【启动】双击 WatermarkTool.exe。整个程序就这一个文件，不用装、不用解压。
【拖拽】把图片或 PDF（可多选）直接拖进窗口；也可点「选择文件」。
【参数】字号 / 四周边距 / 旋转角度 / 不透明度均按「页面短边百分比」计量，改动即时生效。
        水印始终以平铺方式铺满整页，疏密由字号 / 边距 / 角度决定，改参数即时
        重绘预览；位置由系统固定，不可拖动或偏移调节，也不能单独设置某一个水印。
【导出】点「开始添加水印」，默认输出到原目录并加 _watermarked 后缀，
        绝不覆盖原文件；PDF 逐页加水印。

【启动为什么会慢几秒】
* 单文件版每次运行会把整包解压到系统临时目录（%%TEMP%%\\_MEIxxxxxx，约 70MB），
  退出时自动清理。冷启动因此比目录版慢，属正常现象，不是卡死。
* 介意这点延迟就用目录版（WatermarkTool 文件夹 + _internal，双击文件夹里的 exe）。

【其它】
* 首次运行若被 SmartScreen 拦截：点「更多信息 -> 仍要运行」（单文件版更容易触发）。
* 运行日志在 %LOCALAPPDATA%\\{app}\\logs\\，启动失败可去那里查 error.log。
* 本工具基于 PyMuPDF（AGPL-3.0），自用无影响，商用分发前请评估许可条款。
"""


if __name__ == "__main__":
    sys.exit(main())
