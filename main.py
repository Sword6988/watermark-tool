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
import tempfile
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
    """可写日志目录：``%LOCALAPPDATA%\\WatermarkTool\\logs``（绝不写程序目录）。

    **创建失败必须吞掉**：受限账户、组策略重定向、漫游配置文件未就绪都会让
    ``%LOCALAPPDATA%`` 不可写。而这个函数同时被「崩溃兜底」自己调用
    （:func:`main` 的 except 分支第一行）—— 它一抛，兜底就跟着崩，表现为
    **双击 exe 完全无反应**，连错误框都没有。所以失败时降级到系统临时目录。
    """
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    d = os.path.join(base, APP_NAME, "logs")
    try:
        os.makedirs(d, exist_ok=True)
        return d
    except OSError:
        pass
    fallback = os.path.join(tempfile.gettempdir(), APP_NAME, "logs")
    try:
        os.makedirs(fallback, exist_ok=True)
    except OSError:
        pass  # 连临时目录都建不了：返回路径即可，后续写入各自吞错
    return fallback


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
            try:
                with open(self._path + ".%d" % os.getpid(), "a",
                          encoding="utf-8", errors="replace") as f:
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


def _prune_runtime_logs(directory: str, keep: int = 5) -> None:
    """只保留最近 ``keep`` 份 runtime 日志（改成按 pid 分文件后会累积）。"""
    try:
        names = sorted((n for n in os.listdir(directory)
                        if n.startswith("runtime-") and n.endswith(".log")),
                       key=lambda n: os.path.getmtime(os.path.join(directory, n)),
                       reverse=True)
    except OSError:
        return
    for name in names[keep:]:
        try:
            os.remove(os.path.join(directory, name))
        except OSError:
            pass


def install_stdio() -> None:
    if sys.stdout is not None and sys.stderr is not None:
        return
    # 日志名带 pid：多实例并行时共用 runtime.log 会互相截断，排查时看到的是
    # 两个进程交织的半句话。按 pid 分开写，谁写的谁的一目了然。
    log = os.path.join(user_log_dir(), "runtime-%d.log" % os.getpid())
    _prune_runtime_logs(os.path.dirname(log))
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

    # argv 已经是「待处理路径」列表（选项由 _split_argv 剥离）。
    # 不存在的路径**必须提示**：静默滤掉会让人以为工具坏了（双击关联打开时
    # 路径带引号/空格尤其容易踩）。
    missing = [p for p in argv if not os.path.exists(p)]
    files = [os.path.abspath(p) for p in argv if os.path.exists(p)]
    if missing:
        print("[WARN] 以下路径不存在，已忽略：" + "；".join(missing[:5]), file=sys.stderr)
    if files:
        application.add_files(files)

    root.mainloop()
    return 0


def install_excepthooks() -> None:
    """把**后台线程**的异常也写进日志。

    默认只有主线程未捕获异常会走 ``sys.excepthook``；预览线程 / 批处理线程里
    的异常既不打日志也不弹框 —— 用户看到的只是「预览一直不出来」，排查时
    连一行线索都没有。这里统一接到 error.log（与主流程同一份）。
    """
    def _thread_hook(args) -> None:
        try:
            err = os.path.join(user_log_dir(), "error.log")
            rotate_log(err)
            with open(err, "a", encoding="utf-8") as f:
                f.write("==== %s (thread %s) ====\n"
                        % (time.strftime("%Y-%m-%d %H:%M:%S"),
                           getattr(args.thread, "name", "?")))
                f.write("".join(traceback.format_exception(
                    args.exc_type, args.exc_value, args.exc_traceback)) + "\n")
        except Exception:
            pass
        try:
            print("[ERROR] 后台线程异常：%s: %s"
                  % (getattr(args.exc_type, "__name__", "?"), args.exc_value),
                  file=sys.stderr)
        except Exception:
            pass

    try:
        import threading
        threading.excepthook = _thread_hook
    except Exception:
        pass


def _split_argv(argv: list) -> Tuple[list, Optional[str]]:
    """把 argv 拆成 ``(待处理文件, selftest 输出目录|None)``。

    早先 ``"--selftest" in argv`` 是成员判断：``a.png --selftest`` 会因为
    ``i+1`` 超界而把 a.png 也丢掉（且无任何提示）。这里按“第一个 -- 开头的
    选项”切分，选项之后的参数才能被当成选项值。
    """
    files: list = []
    selftest_out: Optional[str] = None
    index = 0
    while index < len(argv):
        item = argv[index]
        if item == "--selftest":
            selftest_out = argv[index + 1] if len(argv) > index + 1 \
                else os.path.join(os.getcwd(), "selftest_out")
            index += 2
            continue
        if item.startswith("--"):
            print("[WARN] 忽略未知选项：%s" % item, file=sys.stderr)
            index += 1
            continue
        files.append(item)
        index += 1
    return files, selftest_out


def _warn_non_ascii_paths() -> None:
    """启动期留痕：解包目录含非 ASCII 时记一行（**不阻断**）。

    为什么留痕而不是直接处理：本机模拟验证过（中文 / emoji 路径）三条解密通道都
    照常产出字节，现代 Windows 的 ``type`` 走 Unicode API，不走 ANSI。但**真
    exe + 中文 ``%TEMP%``** 这一组合没法在没有中文账号的机器上真实复现 —— 与其
    猜测，不如让它出问题时在日志里自己说话。
    """
    if not FROZEN:
        return
    base = getattr(sys, "_MEIPASS", "") or ""
    if base and not base.isascii():
        print("[WARN] 解包目录含非 ASCII 字符：%s —— 若解密异常，请把程序放到"
              "纯 ASCII 路径下（此路径由 %%TEMP%% 决定）" % base, file=sys.stderr)


def main(argv: list) -> int:
    try:
        # install_stdio 必须在 try 里：它依赖 user_log_dir()（要建目录），而
        # 兜底逻辑在下面的 except —— 若在 try 之外抛出，就没人接得住了。
        install_stdio()
        install_excepthooks()
        _warn_non_ascii_paths()
        files, selftest_out = _split_argv(argv[1:])
        if selftest_out is not None:
            # 自检**不走**下面的 GUI 报错路径：那里会弹模态框，在无头环境（打包脚本 /
            # CI）里会永久挂起。这里自己兜住异常，只写 stderr + 返回退出码。
            try:
                return selftest(selftest_out)
            except Exception:
                traceback.print_exc()
                print("[SELFTEST] FAIL")
                return 1
        return gui_main(files)
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
