"""入口程序：``python main.py [文件...]``（打包版为 ``WatermarkTool.exe``）。

用法：
    python main.py                      # 打开空窗口
    python main.py a.png b.pdf          # 启动时预载文件
    python main.py --selftest <目录>    # 冻结环境自检：不建主窗口，跑通核心链路，
                                        # 结果写 <目录>/selftest_report.txt，退出码表达成败

冻结（PyInstaller）环境适配：
    * console=False 时 ``sys.stdout/stderr`` 为 None —— 库内部任何 print 都会抛
      AttributeError 并让程序**无声退出**，因此启动时装一个最小文件 writer；
    * 未捕获异常写 error.log 并弹错误框，避免「窗口闪一下就没了」无法排查；
    * 模块搜索根在冻结后是 ``sys._MEIPASS``，不再用 ``__file__`` 推导。
"""

from __future__ import annotations

import os
import sys
import time
import traceback

APP_NAME = "WatermarkTool"
FROZEN = bool(getattr(sys, "frozen", False))
#: 单个错误日志的字节上限，超过就轮转（runtime.log 每次启动重写，不受此限）
ERROR_LOG_MAX_BYTES = 512 * 1024
#: 保留的历史错误日志份数（``error.log.1`` … ``error.log.N``）
ERROR_LOG_BACKUPS = 3


# ---------------------------------------------------------------------------
# 冻结环境基础设施
# ---------------------------------------------------------------------------

def rotate_log(path: str, limit: int = ERROR_LOG_MAX_BYTES,
               backups: int = ERROR_LOG_BACKUPS) -> None:
    """``error.log`` 超限即轮转：``.log -> .log.1 -> .log.2 …``，最多留 ``backups`` 份。

    为什么必须轮转：每异常写数百到数千字节，而卡在同一个启动错误上时用户会反复
    双击 —— 日志**只增不减**，几个月后单个文件就能长到几十 MB，且「最新的原因」
    被埋在文件尾部，排查时反而更难找。轮转后 error.log 恒为最新一次。

    任何一步失败都吞掉：日志轮转不是主流程，绝不能为了整理日志把程序搞崩。
    """
    try:
        if not os.path.exists(path) or os.path.getsize(path) <= limit:
            return
        oldest = "%s.%d" % (path, backups)
        if os.path.exists(oldest):
            os.remove(oldest)
        for index in range(backups - 1, 0, -1):
            source = "%s.%d" % (path, index)
            if os.path.exists(source):
                os.replace(source, "%s.%d" % (path, index + 1))
        os.replace(path, "%s.1" % path)
    except OSError:
        pass

def user_log_dir() -> str:
    """可写日志目录：``%LOCALAPPDATA%\\WatermarkTool\\logs``（绝不写程序目录）。"""
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    d = os.path.join(base, APP_NAME, "logs")
    os.makedirs(d, exist_ok=True)
    return d


def module_root() -> str:
    """模块搜索根：冻结后是解包目录 ``sys._MEIPASS``，源码运行是本文件所在目录。"""
    if FROZEN:
        return getattr(sys, "_MEIPASS", None) or os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


class _FileWriter:
    """console=False 时顶替 None 的 stdout/stderr，写入日志文件。"""

    encoding = "utf-8"

    def __init__(self, path: str) -> None:
        self._path = path

    def write(self, s: str) -> int:
        try:
            with open(self._path, "a", encoding="utf-8", errors="replace") as f:
                f.write(s)
        except OSError:
            pass
        return len(s)

    def writelines(self, lines) -> None:
        for line in lines:
            self.write(line)

    def flush(self) -> None:
        pass

    def isatty(self) -> bool:
        return False

    def fileno(self) -> int:
        raise OSError("no fileno")


def install_stdio() -> None:
    if sys.stdout is not None and sys.stderr is not None:
        return
    log = os.path.join(user_log_dir(), "runtime.log")
    try:
        open(log, "w", encoding="utf-8").close()  # 每会话清空，避免无限增长
    except OSError:
        pass
    if sys.stdout is None:
        sys.stdout = _FileWriter(log)
    if sys.stderr is None:
        sys.stderr = _FileWriter(log)


# ---------------------------------------------------------------------------
# 冻结环境自检（构建后自动验收的依据）
# ---------------------------------------------------------------------------

def selftest(out_dir: str) -> int:
    """不创建主窗口，跑通核心链路；结果写 ``selftest_report.txt``。

    断言落到实处：原生库可导入且版本正确、拖拽扩展真实加载（TkinterDnD + TkdndVersion）、
    图片加水印**逐像素 diff** 生效、PDF 加水印后逐页有墨迹、中文目录/文件名全链路可用。
    """
    import io

    # ⚠️ 自检是给打包脚本 / CI 用的**无人值守**入口：任何失败都必须直接返回退出码，
    # 绝不能弹模态框 —— 那会让无头环境永久挂起（实测传已存在的**文件**路径作输出
    # 目录时抛 FileExistsError，弹出框后 180 秒都不退出）。
    try:
        os.makedirs(out_dir, exist_ok=True)
    except OSError as exc:
        print("[FAIL] 自检输出目录不可用：%r" % (exc,))
        print("[SELFTEST] FAIL")
        return 1
    report = os.path.join(out_dir, "selftest_report.txt")
    lines: list = []
    failures: list = []

    def check(name: str, cond: bool, detail: str = "") -> None:
        tag = "[OK]" if cond else "[FAIL]"
        lines.append("%s %s %s" % (tag, name, detail))
        if not cond:
            failures.append(name)

    root = module_root()
    if root not in sys.path:
        sys.path.insert(0, root)

    # 1) 原生库
    try:
        import PIL
        import pymupdf as fitz

        check("libs", True, "Pillow=%s PyMuPDF=%s" % (PIL.__version__, fitz.__version__))
    except Exception as exc:  # pragma: no cover
        check("libs", False, repr(exc))
        lines.append("[FAIL] 后续用例依赖原生库，终止")
        with open(report, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        return 1

    # 2) 拖拽扩展（最容易被静默吞掉的功能，必须显式断言）
    try:
        from tkinterdnd2 import TkinterDnD

        r = TkinterDnD.Tk()
        r.withdraw()
        ver = r.tk.call("package", "present", "tkdnd")
        r.destroy()
        check("tkdnd", bool(ver), "TkdndVersion=%s" % (ver,))
    except Exception as exc:
        check("tkdnd", False, "拖拽扩展加载失败: %r" % (exc,))

    from PIL import Image, ImageDraw

    from wm import render
    from wm.spec import WatermarkSpec

    # 3) 图片链路（中文目录 + 中文文件名）
    try:
        cn_dir = os.path.join(out_dir, "中文 目录")
        os.makedirs(cn_dir, exist_ok=True)
        src_path = os.path.join(cn_dir, "测试图.png")
        Image.new("RGB", (600, 400), (250, 250, 252)).save(src_path)
        src = Image.open(src_path)
        out_img = render.render_image(src, WatermarkSpec())
        out_path = os.path.join(cn_dir, "测试图_水印.png")
        out_img.save(out_path)
        reloaded = Image.open(out_path)
        a = src.convert("RGB")
        b = reloaded.convert("RGB").resize(a.size)
        ca, cb = a.tobytes(), b.tobytes()
        changed = sum(1 for x, y in zip(ca, cb) if x != y)
        ratio = changed / max(1, len(ca))
        check("image", reloaded.size == src.size and ratio > 0.005,
              "尺寸=%s 改动像素占比=%.2f%%" % (reloaded.size, ratio * 100))
    except Exception as exc:
        check("image", False, repr(exc))

    # 4) PDF 链路（逐页有墨迹）
    try:
        cn_dir = os.path.join(out_dir, "中文 目录")
        pdf_in = os.path.join(cn_dir, "测试文档.pdf")
        doc = fitz.open()
        for i in range(3):
            page = doc.new_page(width=595, height=842)
            page.insert_text((72, 90), "selftest page %d" % (i + 1), fontsize=14)
        doc.save(pdf_in)
        doc.close()
        pdf_out = os.path.join(cn_dir, "测试文档_水印.pdf")
        n = render.render_pdf(pdf_in, pdf_out, WatermarkSpec())
        rd = fitz.open(pdf_out)
        ink_pages = 0
        for page in rd:
            pix = page.get_pixmap(dpi=72)
            img = Image.open(io.BytesIO(pix.tobytes("png"))).convert("RGB")
            white = sum(1 for v in img.tobytes() if v < 250)
            if white > 50:
                ink_pages += 1
        rd.close()
        check("pdf", n == 3 and ink_pages == 3, "页数=%d 有墨迹页数=%d" % (n, ink_pages))
    except Exception as exc:
        check("pdf", False, repr(exc))

    lines.append("")
    lines.append("总计 %d 项 | 失败 %d 项" % (len(failures) + len([l for l in lines if l.startswith('[OK]')]),
                                            len(failures)))
    ok = not failures
    lines.append("[SELFTEST] %s" % ("PASS" if ok else "FAIL"))
    text = "\n".join(lines) + "\n"
    with open(report, "w", encoding="utf-8") as f:
        f.write(text)
    # 同时打到 stdout：**只写文件**会让靠 grep stdout 判断的脚本误以为自检没跑
    print(text, end="")
    return 0 if ok else 1


# ---------------------------------------------------------------------------
# GUI 主流程
# ---------------------------------------------------------------------------

def set_window_icon(root) -> None:
    """用 ``assets/app.ico`` 顶掉 Tk 默认的「羽毛」图标。

    Tk 在 Windows 上有两个不同的图标位，必须**都设**才稳妥：

    * ``wm iconbitmap -default <ico>`` —— 标题栏 / 任务栏位（不带 ``-default``
      的 ``iconbitmap`` 只作用于「最小化后的图标」，换不掉标题栏的羽毛）；
    * ``wm iconphoto -default <img>``  —— 跨平台兜底，同样落在标题栏。

    任一环节失败都不影响启动：图标不该拖垮主流程。
    """
    base = module_root()
    ico = os.path.join(base, "assets", "app.ico")
    png = os.path.join(base, "assets", "app_256.png")

    if os.path.isfile(ico):
        try:
            root.tk.call("wm", "iconbitmap", root._w, "-default", ico)
        except Exception:
            try:
                root.iconbitmap(ico)  # 退化：至少设上最小化图标
            except Exception:
                pass

    if os.path.isfile(png):
        try:
            from PIL import Image, ImageTk

            photo = ImageTk.PhotoImage(Image.open(png).resize((64, 64), Image.LANCZOS))
            root.iconphoto(True, photo)
            root._app_icon = photo  # 持有引用：否则被 GC 后图标会消失
        except Exception:
            pass


def gui_main(argv: list) -> int:
    if module_root() not in sys.path:
        sys.path.insert(0, module_root())

    from wm.ui import app as ui_app
    from wm.ui import theme as theme

    theme.enable_dpi_awareness()  # 必须在创建 Tk() 之前声明 DPI 感知

    try:
        from tkinterdnd2 import TkinterDnD
        root = TkinterDnD.Tk()
    except Exception:
        import tkinter as tk
        root = tk.Tk()

    set_window_icon(root)  # 在建窗口后、进入主循环前换掉 Tk 默认羽毛图标

    application = ui_app.App(root)

    files = [os.path.abspath(p) for p in argv[1:] if os.path.exists(p)]
    if files:
        application.add_files(files)

    root.mainloop()
    return 0


def main(argv: list) -> int:
    install_stdio()
    try:
        if "--selftest" in argv:
            i = argv.index("--selftest")
            out = argv[i + 1] if len(argv) > i + 1 else os.path.join(os.getcwd(), "selftest_out")
            # 自检**不走**下面的 GUI 报错路径：那里会弹模态框，在无头环境（打包脚本 /
            # CI）里会永久挂起。这里自己兜住异常，只写 stderr + 返回退出码。
            try:
                return selftest(out)
            except Exception:
                traceback.print_exc()
                print("[SELFTEST] FAIL")
                return 1
        return gui_main(argv)
    except Exception:
        err = os.path.join(user_log_dir(), "error.log")
        tb = traceback.format_exc()
        try:
            rotate_log(err)  # 先轮转：否则反复失败会让 error.log 无限增长
            with open(err, "a", encoding="utf-8") as f:
                f.write("==== %s ====\n" % time.strftime("%Y-%m-%d %H:%M:%S"))
                f.write(tb + "\n")
        except OSError:
            pass
        try:
            from tkinter import Tk, messagebox
            r = Tk()
            r.withdraw()
            messagebox.showerror(APP_NAME, "启动失败，详见日志：\n%s\n\n%s" % (err, tb[-800:]))
            r.destroy()
        except Exception:
            pass
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
