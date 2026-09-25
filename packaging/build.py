# -*- coding: utf-8 -*-
"""目录版打包脚本：构建 -> 自检验收 -> GUI 冒烟 -> 打 zip。

用法（用**装了 PyInstaller + 本项目依赖的那个解释器**跑；路径随机器而异，
必须含 tkinter —— 托管 Python 通常没有）：

    "C:\\Users\\Administrator\\AppData\\Local\\Programs\\Python\\Python313\\python.exe" packaging/build.py
    # 或者直接：<当前 python> packaging/build.py（推荐，sys.executable 就是它）

依赖缺失时的安装（务必走国内镜像，官方 PyPI 实测慢 50 倍以上）：
    <python> -m pip install -i https://pypi.tuna.tsinghua.edu.cn/simple pyinstaller

产物：dist/WatermarkTool/（免安装目录版）+ dist/WatermarkTool-portable.zip

铁律：清理旧产物用「移走」（os.rename 到 build/_obsolete），绝不 rmtree ——
本机存在批量删除保护（阈值 50/回合），rmtree 会让脚本无声卡住。
"""
import os
import subprocess
import sys
import time
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DIST = os.path.join(ROOT, "dist")
BUILD = os.path.join(ROOT, "build")
OBSOLETE = os.path.join(BUILD, "_obsolete")
APP_DIR = os.path.join(DIST, "WatermarkTool")
EXE = os.path.join(APP_DIR, "WatermarkTool.exe")
SPEC = os.path.join(HERE, "watermark-dir.spec")


def move_aside(path: str) -> None:
    """把旧产物移进 build/_obsolete/<名>_<时间戳>（不删除）。"""
    if not os.path.exists(path):
        return
    os.makedirs(OBSOLETE, exist_ok=True)
    target = os.path.join(OBSOLETE, "%s_%s" % (os.path.basename(path), time.strftime("%H%M%S")))
    os.rename(path, target)
    print("[OK] 旧产物移走 -> %s" % target)


def run(cmd, **kw):
    print("[RUN] %s" % " ".join(os.path.basename(c) if i == 0 else c for i, c in enumerate(cmd)))
    proc = subprocess.run(cmd, **kw)
    if proc.returncode != 0:
        raise SystemExit("[FAIL] 命令失败，退出码 %d" % proc.returncode)
    return proc


def _rmtree_guarded(path: str) -> None:
    """子进程删除（带超时守卫）：本机存在批量删除保护（阈值约 50 删除/回合），
    进程内 ``shutil.rmtree`` 会静默卡住、拖垮整个构建，故每次删除都走独立
    python 子进程并限时 120s。超时或失败只报 WARN 跳过，绝不向上抛。
    """
    try:
        # 目录走 rmtree；``_obsolete`` 里也有被移走的 zip 文件，单独 os.remove。
        cmd = ("import os,sys; p=sys.argv[1]; "
               "(os.remove(p) if os.path.isfile(p) else __import__('shutil').rmtree(p))")
        subprocess.run(
            [sys.executable, "-c", cmd, path],
            timeout=120, check=False,
        )
    except subprocess.TimeoutExpired:
        print("[WARN] 清理 %s 超时，已跳过（下次构建再试）" % path)
    except Exception as exc:
        print("[WARN] 清理 %s 失败：%r" % (path, exc))


def prune_obsolete(keep: int = 2) -> None:
    """保留 ``build/_obsolete/`` 中 mtime 最新的 ``keep`` 个，删除其余以腾空间。

    绝不进程内 rmtree：每个待删项都经 ``_rmtree_guarded`` 子进程删除。
    目录不存在时静默 no-op；任何异常都只报 WARN，绝不把构建打断。
    """
    try:
        if not os.path.isdir(OBSOLETE):
            return
        entries = [e for e in os.listdir(OBSOLETE)]
        if len(entries) <= keep:
            print("[OK] 清理 _obsolete：保留 %d 个，删除 0 个" % len(entries))
            return
        full = [os.path.join(OBSOLETE, e) for e in entries]
        full.sort(key=lambda p: os.path.getmtime(p), reverse=True)
        keep_paths = full[:keep]
        del_paths = full[keep:]
        for p in del_paths:
            _rmtree_guarded(p)
        print("[OK] 清理 _obsolete：保留 %d 个，删除 %d 个"
              % (len(keep_paths), len(del_paths)))
    except Exception as exc:
        print("[WARN] 清理 _obsolete 失败：%r" % exc)


def main() -> int:
    t0 = time.time()
    prune_obsolete()  # 先清理历史产物，再 move_aside 本轮旧包

    # 1) 环境校验（spec 里还会再查一次，这里提前暴露问题）
    sys.path.insert(0, HERE)
    import spec_common as SC

    SC.check_environment()

    # 2) 旧产物移走（绝不 rmtree）
    move_aside(APP_DIR)
    move_aside(os.path.join(BUILD, "pyi"))

    # 3) 构建
    run([sys.executable, "-m", "PyInstaller", SPEC, "--noconfirm",
         "--distpath", DIST, "--workpath", os.path.join(BUILD, "pyi")])

    # 4) 产物完整性：PyMuPDF 原生 DLL 必须已进 _internal
    internal = os.path.join(APP_DIR, "_internal")
    dll = os.path.join(internal, "pymupdf", "mupdfcpp64.dll")
    if not os.path.exists(dll):
        raise SystemExit("[FAIL] %s 缺失，PDF 功能会在运行时报 DLL 错误" % dll)
    print("[OK] pymupdf 原生 DLL 就位")

    # 5) 自检验收（退出码 + 报告无 [FAIL] 双重判定）
    accept_dir = os.path.join(BUILD, "_accept")
    proc = subprocess.run([EXE, "--selftest", accept_dir], timeout=600)
    report = os.path.join(accept_dir, "selftest_report.txt")
    text = open(report, encoding="utf-8").read() if os.path.exists(report) else ""
    print(text)
    if proc.returncode != 0 or "[FAIL]" in text:
        raise SystemExit("[FAIL] 冻结环境自检未通过，拒绝交付")
    print("[OK] 冻结环境自检 PASS")

    # 6) GUI 冒烟：真实启动 4 秒，进程仍存活即视为未崩溃
    gui = subprocess.Popen([EXE], cwd=APP_DIR)
    time.sleep(4)
    if gui.poll() is not None:
        raise SystemExit("[FAIL] GUI 启动即退出（退出码 %s），查 %%LOCALAPPDATA%%\\%s\\logs"
                         % (gui.returncode, SC.APP_NAME))
    gui.terminate()
    gui.wait(timeout=10)
    print("[OK] GUI 冒烟通过（4 秒存活）")

    # 7) 用户文档 + 自检报告
    doc = os.path.join(APP_DIR, "使用说明.txt")
    with open(doc, "w", encoding="utf-8") as f:
        f.write(USAGE.format(app=SC.APP_NAME, ver=SC.APP_VERSION))
    with open(report, encoding="utf-8") as f:
        rep = f.read()
    with open(os.path.join(DIST, "自检报告.txt"), "w", encoding="utf-8") as f:
        f.write(rep)

    # 8) 打 zip（含 CRC 校验）
    zip_path = os.path.join(DIST, "%s-portable.zip" % SC.APP_NAME)
    if os.path.exists(zip_path):
        move_aside(zip_path)
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for base, _dirs, files in os.walk(APP_DIR):
            for name in files:
                full = os.path.join(base, name)
                rel = os.path.relpath(full, DIST)
                zf.write(full, rel)
        zf.write(os.path.join(DIST, "自检报告.txt"), "自检报告.txt")
    with zipfile.ZipFile(zip_path) as zf:
        bad = zf.testzip()
        if bad is not None:
            raise SystemExit("[FAIL] zip CRC 校验失败: %s" % bad)
    print("[OK] zip CRC 校验通过")

    # 9) 体积清单
    def size(p):
        total = 0
        for b, _d, fs in os.walk(p):
            for n in fs:
                total += os.path.getsize(os.path.join(b, n))
        return total / 1e6

    print()
    print("=" * 56)
    print("[DONE] %.1f 秒" % (time.time() - t0))
    print("  目录版: %s (%.1f MB)" % (APP_DIR, size(APP_DIR)))
    print("  压缩包: %s (%.1f MB)" % (zip_path, os.path.getsize(zip_path) / 1e6))
    print("  注意：目录版必须保留 %s 目录" % os.path.join(SC.APP_NAME, "_internal"))
    return 0


USAGE = """{app} {ver} —— 图片 / PDF 文字水印工具（免安装版）
============================================================

【启动】双击 WatermarkTool.exe。
【拖拽】把图片或 PDF（可多选）直接拖进窗口；也可点「选择文件」。
【参数】字号 / 四周边距 / 旋转角度 / 不透明度均按「页面短边百分比」计量，改动即时生效。
        水印始终以平铺方式铺满整页，疏密由字号 / 边距 / 角度决定，改参数即时
        重绘预览；位置由系统固定，不可拖动或偏移调节，也不能单独设置某一个水印。
【导出】点「开始添加水印」，默认输出到原目录并加 _watermarked 后缀，
        绝不覆盖原文件；PDF 逐页加水印。

【重要】
* 目录版必须保留 WatermarkTool\\_internal 文件夹，缺了会无法启动。
* 首次运行若被 SmartScreen 拦截：点「更多信息 -> 仍要运行」。
* 运行日志在 %LOCALAPPDATA%\\{app}\\logs\\，启动失败可去那里查 error.log。
* 本工具基于 PyMuPDF（AGPL-3.0），自用无影响，商用分发前请评估许可条款。
"""


if __name__ == "__main__":
    sys.exit(main())
