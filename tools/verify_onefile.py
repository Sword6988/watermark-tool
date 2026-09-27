# -*- coding: utf-8 -*-
"""单文件 EXE 独立验收：把 exe 拷到**空目录**里跑，证明它没有隐形旁挂依赖。

放在 ``tools/`` 而不是 ``_smoke/``：后者被 .gitignore 排除，而 README 第十节引用了本
脚本做交付验收 —— 留在被排除的目录里，别人 clone 下来这条命令根本跑不了。

三项硬断言（任一失败即退出码 1）：
1. alone-selftest —— 单独拷走后自检 4/4 PASS（tkdnd / 图片 / PDF / 中文路径全过）；
2. cold-start     —— 冷启动到主窗口出现的时间被量化（秒），并确认确实解压出了
                     %TEMP%\\_MEI* 自解压目录；
3. no-side-files  —— 运行期间目标目录**没有**生成 _internal 之类的旁挂文件夹
                     （真单文件的判据：程序目录保持干净）。
"""
import ctypes
import os
import shutil
import subprocess
import sys
import time
from ctypes import wintypes

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# 默认验单文件版；传参可验目录版以做冷启动对比：
#   python tools/verify_onefile.py dist/WatermarkTool/WatermarkTool.exe
EXE = os.path.abspath(sys.argv[1]) if len(sys.argv) > 1 else os.path.join(ROOT, "dist", "WatermarkTool.exe")
# 隔离目录放在**本脚本同级**而不是 ROOT/_smoke：_smoke 被 .gitignore 排除，
# 放在那里等于每次 clone 后第一次运行才临时造目录，且路径随仓库忽略规则漂移。
ALONE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_one_alone")
# 精确主窗口标题（app.TITLE），不用模糊关键词 —— 模糊匹配会撞上残留窗口
TITLE_KEY = "图片 / PDF 文字水印工具"

EnumWindows = ctypes.windll.user32.EnumWindows
EnumWindowsProc = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
GetWindowTextLength = ctypes.windll.user32.GetWindowTextLengthW
GetWindowText = ctypes.windll.user32.GetWindowTextW
IsWindowVisible = ctypes.windll.user32.IsWindowVisible


def find_window(key: str) -> bool:
    """是否已有标题含 key 的可见窗口（冷启动计时用）。"""
    found = []

    def cb(hwnd, _lparam):
        if not IsWindowVisible(hwnd):
            return True
        n = GetWindowTextLength(hwnd)
        if n <= 0:
            return True
        buf = ctypes.create_unicode_buffer(n + 1)
        GetWindowText(hwnd, buf, n + 1)
        if key in buf.value:
            found.append(buf.value)
            return False
        return True

    EnumWindows(EnumWindowsProc(cb), 0)
    return bool(found)


def kill_leftovers() -> int:
    """杀掉所有残留的 WatermarkTool.exe —— 残留窗口会让冷启动计时读到 0.01 秒
    （实测踩过：上一轮冒烟的进程没死透，脚本一启动就「找到」了它的窗口）。"""
    proc = subprocess.run(["taskkill", "/F", "/IM", "WatermarkTool.exe"],
                          capture_output=True, text=True, errors="replace")
    time.sleep(1.0)
    n = proc.stdout.count("PID")
    if n:
        print("[OK] 清理残留进程 %d 个" % n)
    return n


def main() -> int:
    fails = []
    if not os.path.isfile(EXE):
        print("[FAIL] 源 exe 不存在: %s" % EXE)
        return 1

    kill_leftovers()
    if find_window(TITLE_KEY):
        print("[FAIL] 仍存在标题含「%s」的窗口，冷启动计时会失真" % TITLE_KEY)
        return 1

    os.makedirs(ALONE, exist_ok=True)
    alone_exe = os.path.join(ALONE, "WatermarkTool.exe")
    shutil.copy2(EXE, alone_exe)
    # 目录版（同级有 _internal）必须连文件夹一起搬，否则根本起不来 —— 这本身就是
    # 两种形态最直观的区别：单文件版只有 1 个文件，目录版必须带着 _internal 走。
    side = os.path.join(os.path.dirname(EXE), "_internal")
    is_onefile = not os.path.isdir(side)
    if not is_onefile:  # 验单文件时先清掉上一轮可能留下的 _internal，保证环境干净
        shutil.rmtree(os.path.join(ALONE, "_internal"), ignore_errors=True)
    if os.path.isdir(side):
        dst = os.path.join(ALONE, "_internal")
        if os.path.isdir(dst):
            shutil.rmtree(dst, ignore_errors=True)
        shutil.copytree(side, dst)
        print("[OK] 目录版：一并复制 _internal (%.1f MB)"
              % (sum(os.path.getsize(os.path.join(b, f)) for b, _d, fs in os.walk(dst) for f in fs) / 1e6))
    print("[OK] 已单独复制到空目录: %s" % alone_exe)

    # 1) 单独拷走后自检
    out = os.path.join(ALONE, "selftest")
    t0 = time.time()
    proc = subprocess.run([alone_exe, "--selftest", out], timeout=900,
                          capture_output=True, text=True, encoding="utf-8", errors="replace")
    report = os.path.join(out, "selftest_report.txt")
    text = open(report, encoding="utf-8").read() if os.path.exists(report) else ""
    print(text.strip())
    ok1 = proc.returncode == 0 and "[FAIL]" not in text
    print("[%s] alone-selftest (%d 项全过, %.1fs)"
          % ("OK" if ok1 else "FAIL", text.count("[OK]"), time.time() - t0))
    if not ok1:
        fails.append("alone-selftest")

    # 2) 冷启动计时（单独目录 + 独立 cwd）
    before = set(os.listdir(ALONE))
    temp = os.environ.get("TEMP", "")
    mei_before = {d for d in os.listdir(temp) if d.startswith("_MEI")} if temp else set()
    t0 = time.time()
    gui = subprocess.Popen([alone_exe], cwd=ALONE)
    window_at = None
    deadline = t0 + 90
    while time.time() < deadline:
        if find_window(TITLE_KEY):
            window_at = time.time() - t0
            break
        if gui.poll() is not None:
            break
        time.sleep(0.1)
    if window_at is None:
        print("[FAIL] cold-start 窗口始终未出现（进程退出码 %s）" % gui.poll())
        fails.append("cold-start")
    else:
        print("[OK] cold-start 冷启动到主窗口出现 %.2f 秒" % window_at)
    mei_after = {d for d in os.listdir(temp) if d.startswith("_MEI")} if temp else set()
    new_mei = mei_after - mei_before
    # 单文件版：必须解压出 _MEI*；目录版：反过来，绝不该解压（它直接读 _internal）
    mei_ok = bool(new_mei) if is_onefile else not new_mei
    print("[%s] %s _MEI 自解压目录: %s"
          % ("OK" if mei_ok else "FAIL",
             "单文件版解压出" if is_onefile else "目录版未产生", sorted(new_mei)))
    if not mei_ok:
        fails.append("mei-dir")
    try:
        gui.terminate()
        gui.wait(timeout=20)
    except Exception:
        gui.kill()
    time.sleep(1.5)

    # 3) 目录保持干净（真单文件：不在程序目录旁生成任何东西）
    after = set(os.listdir(ALONE))
    added = after - before
    print("[%s] 程序目录无旁挂: 新增 %s" % ("OK" if not added else "FAIL", sorted(added)))
    if added:
        fails.append("no-side-files")

    print()
    print("[RESULT] %s" % ("PASS（单文件版可独立运行）" if not fails else "FAIL %s" % fails))
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(main())
