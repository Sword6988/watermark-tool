"""主窗口装配：左（文件列表 + 参数面板）/ 右（预览）/ 下（进度与操作）。

线程模型（关键）：
    * 预览与批处理都在后台线程跑；
    * **后台线程绝不直接调用 Tk**，结果放进 ``queue.Queue``，主线程用
      ``after()`` 轮询后贴图 —— 跨线程调用 Tk 是不安全的。
    * 预览渲染用 generation 计数丢弃过期结果（快速拖参数时只贴最新一帧）。

关闭语义（关键）：
    * 用户点「×」走 ``WM_DELETE_WINDOW`` -> ``_on_close``；
    * 代码 / 测试直接 ``root.destroy()`` 走 ``_guard_root_destroy`` 装的守卫；
    * 两条路径最终都汇到 ``_release_resources``（幂等）：停轮询、停预览、关文档、
      通知批处理线程。文档**必须**关 —— 实测 PDF 的 PyMuPDF 句柄不关就一直占住
      源文件（WinError 32，删除 / 改名都会被拒）。
"""

from __future__ import annotations

import os
import queue
import sys
import threading
import traceback
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from typing import Dict, List, Optional, Set, Tuple

from PIL import Image

from .. import media, render
from ..spec import WatermarkSpec
from . import batch, output, preview_job
from . import theme as T
from .panel import ParamPanel
from .preview import PreviewCanvas
from .theme import FluentButton

TITLE = "图片 / PDF 文字水印工具"
PREVIEW_DEBOUNCE_MS = 150
#: 画布尺寸变化时的更短防抖（拖动窗口边缘连续触发，用更短延时保证跟手）
PREVIEW_DEBOUNCE_RESIZE_MS = 90
_POLL_MS = 50
#: 关闭时对批处理线程的有界等待（秒）。线程是 daemon，等不等都不会拖住进程退出；
#: 等一下只是为了让它在关窗前把当前这一页写完，避免留下半截输出文件。
_CLOSE_JOIN_SEC = 1.0


class App:
    """应用主控制器。"""

    #: 输出编码器支持接收 EXIF 的格式（单处真源见 ``output.EXIF_SAVE_EXTS``）。
    _EXIF_SAVE_EXTS = output.EXIF_SAVE_EXTS

    def __init__(self, root: tk.Misc) -> None:
        self.root = root
        self.files: List[str] = []
        self.doc: Optional[media.Document] = None
        self.page_index = 0
        self.out_dir: Optional[str] = None
        self._current_index = -1

        self._queue: "queue.Queue[dict]" = queue.Queue()
        self._preview_job: Optional[str] = None
        self._preview_gen = 0
        self._need_loading = True
        # 预览线程并发闸门：见 _render_preview_async（一次只跑一帧，避免内存叠加）
        self._preview_busy = False
        self._preview_pending = False

        self._batch_thread: Optional[threading.Thread] = None
        self._cancel = False
        self._busy = False
        # 关闭流程标志：置位后不再续接 after、不再接受新的文件列表改动
        self._closing = False
        # 挂起的 after 回调 id（轮询 / 预览防抖），关闭时统一取消
        self._poll_job: Optional[str] = None

        # 拖拽可用性（P1-8：失败要让用户看见，不再静默）
        self.dnd_ok: bool = False
        self.dnd_note: str = ""
        # Tk 回调异常去重键，避免同一个错误反复刷屏 / 反复弹框
        self._tk_error_keys: Set[Tuple[str, str]] = set()

        T.install_ttk_styles(root)
        self._build_ui()
        self._install_lifecycle_hooks()
        self.panel.load_families()
        self._setup_dnd()
        self.preview.show_placeholder("empty", "把文件拖到这里，或点击「选择文件」")
        self._poll_job = self.root.after(_POLL_MS, self._poll_results)

    # ------------------------------------------------------------------
    # 界面装配
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        root = self.root
        root.title(TITLE)
        root.geometry("1120x740")
        root.minsize(T.px(1040), T.px(700))
        root.configure(bg=T.BG)

        pane = tk.PanedWindow(root, orient="horizontal", bd=0, sashwidth=T.px(6),
                              bg=T.BG, sashrelief="flat", opaqueresize=True)
        pane.pack(fill="both", expand=True)
        self.pane = pane

        left = tk.Frame(pane, bg=T.PANEL)
        pane.add(left, minsize=T.px(390), width=T.px(420), stretch="never")
        right = tk.Frame(pane, bg=T.BG)
        pane.add(right, minsize=T.px(420), stretch="always")
        self.left_frame = left

        # -- 文件（准备区 1/4：处理什么文件 + 输出到哪） ---------------------
        files_card = tk.Frame(left, bg=T.PANEL)
        files_card.pack(fill="x", padx=T.px(T.PANEL_PAD), pady=(T.px(T.SP_LG), 0))
        head = tk.Frame(files_card, bg=T.PANEL)
        head.pack(fill="x")
        tk.Label(head, text="文件", bg=T.PANEL, fg=T.TEXT,
                 font=(T.UI_FAMILY, T.sf(11), "bold"), anchor="w").pack(side="left")
        self.count_label = tk.Label(head, text="0 个文件", bg=T.PANEL, fg=T.TEXT_FAINT,
                                    font=(T.UI_FAMILY, T.sf(9)))
        self.count_label.pack(side="right")
        tk.Frame(files_card, bg=T.SECT_BAR, height=T.px(1)).pack(fill="x",
                                                                 pady=(T.px(T.SP_XS), 0))

        list_wrap = tk.Frame(files_card, bg=T.PANEL)
        list_wrap.pack(fill="x", pady=(T.px(T.SP_SM), 0))
        self.files_list = tk.Listbox(
            list_wrap, height=6, activestyle="none", bg=T.PANEL, fg=T.TEXT,
            selectbackground=T.SELECT, selectforeground=T.TEXT,
            highlightthickness=T.px(1), highlightbackground=T.BORDER_SOFT,
            highlightcolor=T.BORDER, bd=0, font=(T.UI_FAMILY, T.sf(9)),
            exportselection=False, relief="flat")
        listbar = ttk.Scrollbar(list_wrap, orient="vertical", command=self.files_list.yview,
                                style="App.Vertical.TScrollbar")
        self.files_list.configure(yscrollcommand=listbar.set)
        self.files_list.pack(side="left", fill="both", expand=True)
        listbar.pack(side="right", fill="y")
        self.files_list.bind("<<ListboxSelect>>", self._on_list_select)

        btn_row = tk.Frame(files_card, bg=T.PANEL)
        btn_row.pack(fill="x", pady=(T.px(T.SP_SM), T.px(T.SP_SM)))
        self.add_btn = FluentButton(btn_row, "选择文件", self._choose_files,
                                    kind="secondary", width=T.px(90))
        self.add_btn.pack(side="left")
        self.del_btn = FluentButton(btn_row, "删除选中", self._remove_selected,
                                    kind="secondary", width=T.px(90))
        self.del_btn.pack(side="left", padx=(T.px(T.SP_SM), 0))
        self.clr_btn = FluentButton(btn_row, "清空", self._clear_files,
                                    kind="secondary", width=T.px(64))
        self.clr_btn.pack(side="left", padx=(T.px(T.SP_SM), 0))

        # 输出目录：与「选择文件」同属准备阶段，迁入文件区形成输入/输出闭环
        out_row = tk.Frame(files_card, bg=T.PANEL)
        out_row.pack(fill="x", pady=(0, T.px(T.SP_MD)))
        self.out_label = tk.Label(out_row, text="输出：与原文件同目录", bg=T.PANEL,
                                  fg=T.TEXT_FAINT, font=(T.UI_FAMILY, T.sf(9)), anchor="w")
        self.out_label.pack(side="left", fill="x", expand=True)
        self.out_btn = FluentButton(out_row, "更改目录", self._choose_out_dir,
                                    kind="secondary", width=T.px(78))
        self.out_btn.pack(side="right")
        self.out_reset_btn = FluentButton(out_row, "恢复同目录", self._reset_out_dir,
                                          kind="ghost", width=T.px(84))
        self.out_reset_btn.pack(side="right", padx=(0, T.px(T.SP_XS)))

        # -- 参数面板 ------------------------------------------------------
        self.panel = ParamPanel(left, on_change=self._on_spec_change)
        self.panel.pack(fill="both", expand=True)

        # -- 预览 ----------------------------------------------------------
        self.preview = PreviewCanvas(
            right,
            on_page_change=self._on_page_change,
            on_canvas_resize=self._on_canvas_resize,
            on_pick_files=self._choose_files,
        )
        self.preview.pack(fill="both", expand=True)

        self._build_action_bar(root)

    def _build_action_bar(self, root: tk.Misc) -> None:
        """底部执行栏：只保留「进展反馈 + 执行」两个职责（输出配置已迁入文件区）。"""
        bar = tk.Frame(root, bg=T.BG)
        bar.pack(fill="x", side="bottom")
        tk.Frame(bar, bg=T.SECT_BAR, height=T.px(1)).pack(fill="x")

        inner = tk.Frame(bar, bg=T.BG)
        inner.pack(fill="x", padx=T.px(T.PANEL_PAD), pady=T.px(T.SP_MD))

        # 阅读顺序从左到右 = 发生了什么 → 到哪了 → 要不要执行
        self.status = tk.Label(inner, text="就绪", bg=T.BG, fg=T.TEXT_DIM,
                               font=(T.UI_FAMILY, T.sf(9)), anchor="w")
        self.status.pack(side="left")
        self.progress = ttk.Progressbar(inner, style="App.Horizontal.TProgressbar",
                                        mode="determinate", maximum=100.0)
        self.progress.pack(side="left", fill="x", expand=True,
                           padx=(T.px(T.SP_LG), T.px(T.SP_LG)))

        # 主按钮（右）
        self.start_btn = FluentButton(inner, "开始添加水印", self._start_batch,
                                      kind="primary", bg=T.BG, width=T.px(124))
        self.start_btn.pack(side="right")
        self.cancel_btn = FluentButton(inner, "取消", self._cancel_batch,
                                       kind="secondary", bg=T.BG, width=T.px(74))
        self.cancel_btn.pack(side="right", padx=(0, T.px(T.SP_MD)))
        self.cancel_btn.set_enabled(False)

    def _install_lifecycle_hooks(self) -> None:
        """装上三件事：关闭协议、destroy 守卫、Tk 回调异常上报。"""
        root = self.root
        # (1) 用户点「×」：走一次清理再销毁（见 _on_close）
        try:
            root.protocol("WM_DELETE_WINDOW", self._on_close)
        except Exception:
            # 没有窗口管理器 / 被嵌入的 root 不支持 WM 协议。此时协议注册是空操作，
            # 清理改由 (2) 的 destroy 守卫兜底，所以这里吞掉是安全的。
            pass
        # (2) 代码或测试直接 root.destroy()（本项目 UI 用例就这么收尾）不会触发协议，
        #     守卫保证任何销毁路径都先清理。
        self._guard_root_destroy()
        # (3) Tk 回调异常：默认实现只打 stderr，冻结版用户完全看不到（见 _on_tk_error）
        root.report_callback_exception = self._on_tk_error

    def _guard_root_destroy(self) -> None:
        """把根窗口的 ``destroy`` 包一层，先清理再销毁。

        为什么必须包：``Misc.after`` 注册的 Tcl 命令会在 ``destroy`` 时被删掉，但
        已经排队的定时器不会跟着消失 —— 销毁后 Tcl 再去执行它就报
        ``invalid command name "..._poll_results"``（本项目实测的 stderr 噪声）。
        只在 ``WM_DELETE_WINDOW`` 上做清理挡不住直接调 ``destroy()`` 的调用方。
        """
        root = self.root
        if getattr(root, "_wm_destroy_guarded", False):
            return  # 同一个 root 上已有守卫，别再包一层（否则会嵌套清理）
        original_destroy = root.destroy
        app = self

        def _destroy_and_release() -> None:
            try:
                app._release_resources()
            except Exception as exc:  # 清理失败绝不能挡住销毁（窗口关不掉更严重）
                stderr = sys.stderr or sys.__stderr__
                if stderr is not None:
                    print(f"[WARN] 关闭清理异常：{exc!r}", file=stderr)
            original_destroy()

        try:
            root.destroy = _destroy_and_release  # type: ignore[method-assign]
            root._wm_destroy_guarded = True
        except Exception as exc:
            # 极少数宿主对象禁止写属性：此时退化为「只靠协议清理」，至少不影响启动。
            # GUI / pythonw 入口可能同时没有 sys.stderr 与 sys.__stderr__，此时静默降级，
            # 绝不能让一条诊断信息反过来阻断 App 实例化。
            stderr = sys.stderr or sys.__stderr__
            if stderr is not None:
                print(f"[WARN] 未能安装 destroy 守卫：{exc!r}", file=stderr)

    def _setup_dnd(self) -> None:
        """注册拖拽投放目标；失败**不再静默**，记进 ``dnd_ok`` 并在状态栏说明。

        历史行为是 ``except: return``：拖拽扩展缺失时用户只看到「拖文件没反应」，
        无从判断是软件坏了还是自己拖错了地方。现在把结果记下来，由
        ``_report_dnd_state`` 给出一次性提示。
        """
        try:
            from tkinterdnd2 import DND_FILES
        except Exception as exc:
            # 未安装 / 冻结包里缺 tkdnd：拖拽功能整体不可用，但不是致命错误，
            # 「选择文件」按钮始终可用，所以只降级提示、不中断启动。
            self.dnd_ok = False
            self.dnd_note = f"缺少拖拽扩展（{exc}）"
            self._report_dnd_state()
            return
        failed: List[str] = []
        for name, widget in (("文件列表", self.files_list),
                             ("预览区", self.preview.canvas),
                             ("左侧面板", self.left_frame)):
            try:
                widget.drop_target_register(DND_FILES)
                widget.dnd_bind("<<Drop>>", self._on_drop)
            except Exception as exc:
                # 单个控件注册失败不影响其它控件继续注册，攒起来最后统一提示
                failed.append(f"{name}（{type(exc).__name__}）")
                continue
        self.dnd_ok = not failed
        self.dnd_note = "、".join(failed)
        self._report_dnd_state()

    def _report_dnd_state(self) -> None:
        """把拖拽不可用这件事写到状态栏（**不**弹模态框）。

        启动阶段弹框会打断无头自检与打包流程，而状态栏提示既可见又不阻塞。
        """
        if self.dnd_ok:
            return
        if self.dnd_note.startswith("缺少"):
            text = "拖拽不可用（缺少 tkinterdnd2），请点「选择文件」"
        else:
            text = f"拖拽部分区域不可用（{self.dnd_note}），也可点「选择文件」"
        self._set_status(text, T.WARN)

    def _set_status(self, text: str, color: str = T.TEXT_DIM) -> None:
        """写状态栏；控件已随 root 销毁（关窗竞态）时忽略。"""
        try:
            self.status.configure(text=text, fg=color)
        except Exception:
            # 关窗后仍可能有回调想写状态栏；此时控件已消失，没有界面可更新，
            # 吞掉不影响任何后续流程（调用方都只是顺手提示）。
            pass

    # ------------------------------------------------------------------
    # 文件管理
    # ------------------------------------------------------------------

    def _on_drop(self, event) -> None:
        if self._list_locked():
            return "break"
        try:
            paths = list(self.root.tk.splitlist(event.data))
        except Exception:
            # splitlist 遇到非法 Tcl 列表串会抛（例如含未配对花括号的罕见路径）；
            # 退化成「整串当一条路径」仍然比直接丢弃更接近用户意图。
            paths = [event.data]
        self._add_paths(paths)
        return "break"

    def add_files(self, paths: List[str]) -> None:
        """公开入口：把一批路径加入列表（供 main.py / CLI 使用）。"""
        self._add_paths(list(paths))

    def _choose_files(self) -> None:
        paths = filedialog.askopenfilenames(
            title="选择图片或 PDF",
            filetypes=[("图片 / PDF", "*.png *.jpg *.jpeg *.bmp *.webp *.tif *.tiff *.gif *.pdf"),
                       ("所有文件", "*.*")])
        if paths:
            self._add_paths(list(paths))

    def _add_paths(self, paths: List[str]) -> None:
        if self._list_locked():
            return
        added: List[str] = []
        skipped: List[str] = []
        for raw in paths:
            path = os.path.abspath(str(raw))
            if os.path.isdir(path):
                try:
                    entries = sorted(os.listdir(path))
                except OSError:
                    # 无权限 / 目录已被删：这一条列不出来就跳过，不计入待处理集合；
                    # 用户会看到「不支持/已跳过」提示，不会误以为加进去了。
                    skipped.append(path)
                    continue
                for name in entries:
                    candidate = os.path.join(path, name)
                    if os.path.isfile(candidate) and media.is_supported(candidate):
                        added.append(candidate)
            elif os.path.isfile(path) and media.is_supported(path):
                added.append(path)
            else:
                skipped.append(path)
        # 去重
        seen = set(self.files)
        fresh = [p for p in added if not (p in seen or seen.add(p))]
        if fresh:
            self.files.extend(fresh)
            self._refresh_list()
            if self._current_index < 0:
                self._select_file(0)
        if skipped and not fresh:
            messagebox.showwarning(TITLE, "以下文件类型不支持，已跳过：\n" + "\n".join(skipped[:8]))

    def _list_locked(self) -> bool:
        """批处理进行中：文件列表只读（命中时顺带说明原因）。

        分母（``len(self.files)``）在启动批处理时就拷进工作线程了，运行中再增删会
        让「已完成 N/总数」的显示错乱。这里**不**静默拒绝：状态栏直接写原因，
        否则用户只会看到「拖进去了但列表没变」。
        """
        if self._closing:
            return True
        thread = self._batch_thread
        if self._busy or (thread is not None and thread.is_alive()):
            self._set_status("处理中，暂不能修改文件列表", T.WARN)
            return True
        return False

    def _refresh_list(self) -> None:
        self.files_list.delete(0, "end")
        for path in self.files:
            kind = media.kind_of(path)
            badge = "PDF" if kind == media.KIND_PDF else "图片"
            self.files_list.insert("end", f"[{badge}]  {os.path.basename(path)}")
        self.count_label.configure(text=f"{len(self.files)} 个文件")
        if 0 <= self._current_index < len(self.files):
            self.files_list.selection_clear(0, "end")
            self.files_list.selection_set(self._current_index)

    def _on_list_select(self, _event=None) -> None:
        selection = self.files_list.curselection()
        if not selection:
            return
        index = int(selection[0])
        if index != self._current_index:
            self._select_file(index)

    def _select_file(self, index: int) -> None:
        if not (0 <= index < len(self.files)):
            return
        self._current_index = index
        self.files_list.selection_clear(0, "end")
        self.files_list.selection_set(index)
        self.files_list.see(index)
        self._close_doc()
        try:
            self.doc = media.Document(self.files[index])
            if self.doc.page_count <= 0:
                raise ValueError("文件不含任何页面（0 页）")
            self.doc.page_size(0)  # 探活：取不到页面尺寸（0 页 / 加密）会在此抛错
        except Exception as exc:
            self._abort_open(index, exc)
            return
        self.page_index = 0
        self.preview.set_nav(0, self.doc.page_count)
        self._need_loading = True
        self._schedule_preview(0)

    def _abort_open(self, index: int, exc: Exception) -> None:
        """打开文件失败时的收尾：关掉半开文档（防句柄泄漏）+ 显示错误占位。

        0 页 PDF 的 ``page_size(0)`` 抛 ``IndexError``、加密 PDF 抛 ``ValueError``；
        这些异常必须在**这里**被接住 —— 否则会直达 Tk 回调，用户看不到任何提示，
        预览停在原态，且失败路径会把 ``Document`` 留在打开状态（Windows 下
        tempdir 清理报 WinError 32）。同时取消在途预览并作废其代数，避免过期
        worker 把错误占位覆盖成「空态」。
        """
        self._cancel_after(self._preview_job)
        self._preview_job = None
        self._preview_gen += 1  # 作废在途预览结果
        self._close_doc()
        name = os.path.basename(self.files[index]) if 0 <= index < len(self.files) else "?"
        self.page_index = 0
        self.preview.set_nav(0, 1)
        self.preview.show_placeholder("error", f"无法打开该文件：\n{name}\n{exc}")

    def _remove_selected(self) -> None:
        if self._list_locked():
            return
        selection = self.files_list.curselection()
        if not selection:
            return
        index = int(selection[0])
        del self.files[index]
        self._close_doc()
        self._current_index = -1
        self._refresh_list()
        if self.files:
            self._select_file(min(index, len(self.files) - 1))
        else:
            # 清空要连元信息一起复位：页码与画布
            self.preview.set_nav(0, 1)
            self.preview.show_placeholder("empty", "把文件拖到这里，或点击「选择文件」")

    def _clear_files(self) -> None:
        if self._list_locked():
            return
        self._close_doc()
        self.files = []
        self._current_index = -1
        self._refresh_list()
        # 清空要连元信息一起复位：页码与画布
        self.preview.set_nav(0, 1)
        self.preview.show_placeholder("empty", "把文件拖到这里，或点击「选择文件」")

    # ------------------------------------------------------------------
    # 参数变化 -> 预览
    # ------------------------------------------------------------------

    def _on_spec_change(self, _spec: WatermarkSpec) -> None:
        self._schedule_preview()

    def _on_canvas_resize(self) -> None:
        self._schedule_preview(PREVIEW_DEBOUNCE_RESIZE_MS)

    def _schedule_preview(self, delay: Optional[int] = None) -> None:
        if self._closing:
            return
        self._cancel_after(self._preview_job)
        self._preview_job = None
        wait = PREVIEW_DEBOUNCE_MS if delay is None else int(delay)
        self._preview_job = self.root.after(wait, self._render_preview_async)

    def _on_page_change(self, delta: int) -> None:
        if self.doc is None:
            return
        target = self.page_index + delta
        if not (0 <= target < self.doc.page_count):
            return
        self.page_index = target
        self.preview.set_nav(target, self.doc.page_count)
        self._need_loading = True
        self._schedule_preview(0)

    # ------------------------------------------------------------------
    # 预览渲染（后台线程 + 主线程贴图）
    # ------------------------------------------------------------------

    def _render_preview_async(self) -> None:
        self._preview_job = None
        if self._closing:
            return
        if self.doc is None:
            self.preview.show_placeholder("empty", "把文件拖到这里，或点击「选择文件」")
            return
        canvas_w, canvas_h = self.preview.canvas_size()
        if canvas_w <= 1 or canvas_h <= 1:
            self._preview_job = self.root.after(80, self._render_preview_async)
            return
        if self._need_loading:
            self.preview.show_placeholder("loading", "正在生成预览…")
            self._need_loading = False
        self._preview_gen += 1
        gen = self._preview_gen
        # **一次只跑一个预览线程**。每个预览线程都会另起一份全分辨率图像
        # （4000×3000 单帧 +156MB，8000×8000 +777MB），并发叠加是**线性**的：
        # 8 并发 × 8000×8000 ≈ 6.2GB，拖 3 秒滑杆（150ms 防抖 ≈ 20 次触发）≈ 15GB。
        # 所以运行期的新请求只记一个 pending，等当前这帧跑完由主线程（_poll_results）
        # 用**最新参数**再起一轮 —— 中间那些过时参数直接跳过。
        if self._preview_busy:
            self._preview_pending = True
            return
        self._preview_busy = True
        self._preview_pending = False
        threading.Thread(
            target=self._preview_worker,
            args=(gen, self.doc.path, self.page_index, self.panel.spec(), canvas_w, canvas_h),
            daemon=True,
        ).start()

    def _preview_worker(self, gen: int, path: str, page: int, spec: WatermarkSpec,
                        canvas_w: int, canvas_h: int) -> None:
        try:
            out, page_w, page_h = preview_job.render_preview(
                path, page, spec, canvas_w, canvas_h)
            if out is None:
                # 打开 / 渲染失败：preview_job 已吞掉异常并返回 None，这里按原始
                # _preview_worker 的 error 载荷上报（不含 page_w / page_h）。
                self._queue.put({"kind": "preview", "gen": gen,
                                 "error": "预览渲染失败：文件无法打开或页面不存在"})
            else:
                self._queue.put({"kind": "preview", "gen": gen, "image": out,
                                 "page_w": page_w, "page_h": page_h})
        except Exception as exc:  # 预览失败不能静默（防御性：render_preview 不应抛）
            self._queue.put({"kind": "preview", "gen": gen, "error": str(exc)})
        finally:
            # 闸门必须在任何出口都放开，否则后续预览全部被 pending 卡死
            self._preview_busy = False

    # ------------------------------------------------------------------
    # 输出目录
    # ------------------------------------------------------------------

    def _choose_out_dir(self) -> None:
        directory = filedialog.askdirectory(title="选择输出目录")
        if directory:
            self.out_dir = directory
            self.out_label.configure(text=f"输出：{self._shorten_path(directory)}")

    @staticmethod
    def _shorten_path(path: str, max_len: int = 26) -> str:
        """长路径中段省略（文件区标签较窄，取更短上限），避免把按钮挤出可视区。"""
        if len(path) <= max_len:
            return path
        return path[:3] + "…" + path[-(max_len - 4):]

    def _reset_out_dir(self) -> None:
        self.out_dir = None
        self.out_label.configure(text="输出：与原文件同目录")

    # ------------------------------------------------------------------
    # 批处理
    # ------------------------------------------------------------------

    def _start_batch(self) -> None:
        if not self.files:
            messagebox.showinfo(TITLE, "请先添加要处理的文件。")
            return
        if self._batch_thread is not None and self._batch_thread.is_alive():
            return
        self._cancel = False
        spec = self.panel.spec()
        files = list(self.files)
        out_dir = self.out_dir
        self._set_busy(True)
        self.progress.configure(value=0.0)
        self.status.configure(text=f"正在准备… 0/{len(files)} 个文件")
        self._batch_thread = threading.Thread(target=self._batch_worker,
                                              args=(files, spec, out_dir), daemon=True)
        self._batch_thread.start()

    def _cancel_batch(self) -> None:
        self._cancel = True
        self.status.configure(text="正在取消…")

    def _set_busy(self, busy: bool) -> None:
        self._busy = bool(busy)
        self.start_btn.set_enabled(not busy)
        self.add_btn.set_enabled(not busy)
        # 批处理进行中不允许改动文件列表与输出目录，避免与后台任务状态混淆
        self.del_btn.set_enabled(not busy)
        self.clr_btn.set_enabled(not busy)
        self.out_btn.set_enabled(not busy)
        self.out_reset_btn.set_enabled(not busy)
        self.cancel_btn.set_enabled(busy)

    def _batch_worker(self, files: List[str], spec: WatermarkSpec,
                      out_dir: Optional[str]) -> None:
        total = len(files)

        def _on_progress(done: int, count: int, label: str, index: int) -> None:
            value = (index + done / max(1, count)) / total * 100.0
            text = f"{index + 1}/{total} 个文件" + (f" · {label}" if label else "")
            self._queue.put({"kind": "progress", "value": value, "text": text})

        def _on_log(text: str) -> None:
            self._queue.put({"kind": "log", "text": text})

        def _on_done(succeeded: int, failed, cancelled: bool) -> None:
            self._queue.put({"kind": "done", "succeeded": succeeded,
                             "failed": failed, "cancelled": cancelled})

        # 循环体已抽到 batch.run_batch（纯逻辑，可无头单测）。这里只负责把 App 的
        # 取消标志 / 队列接到回调上，保持与原来完全一致的载荷形状。
        batch.run_batch(
            files, spec, out_dir,
            is_cancelled=lambda: self._cancel,
            on_progress=_on_progress,
            on_log=_on_log,
            on_done=_on_done)

    @staticmethod
    def _save_image(out: Image.Image, dst: str, exif: Optional[bytes] = None) -> None:
        """按输出扩展名选择合适的保存方式（有损格式压平 alpha）。

        业务逻辑已抽到 :func:`wm.ui.output.save_image`，这里仅作薄封装以保留对外
        口径（``App._save_image(...)`` / ``ui_app.App._save_image(...)`` 仍可用）。
        """
        return output.save_image(out, dst, exif)

    @staticmethod
    def _write_image(image: Image.Image, dst: str, params: Dict[str, object]) -> None:
        """落盘；EXIF 写失败时降级成「不带 EXIF 再存一次」。

        业务逻辑已抽到 :func:`wm.ui.output.write_image`，这里仅作薄封装以保留对外
        口径。
        """
        return output.write_image(image, dst, params)

    # ------------------------------------------------------------------
    # 结果轮询（主线程）
    # ------------------------------------------------------------------

    def _poll_results(self) -> None:
        self._poll_job = None
        try:
            while True:
                item = self._queue.get_nowait()
                self._handle_result(item)
        except queue.Empty:
            # 队列空是**正常轮空**（每 50ms 都会发生），不是异常流程；
            # 正是这个 Empty 让 drain 循环结束，所以必须吞。
            pass
        if self._closing:
            return  # 关窗后不再续接（否则销毁后 Tcl 仍会执行 -> invalid command name）
        # 预览闸门已放开、且期间又有人提了新请求 -> 用**最新参数**补跑一帧。
        # 放在主线程里做，避免从工作线程调 Tk（Tk 不是线程安全的）。
        if self._preview_pending and not self._preview_busy:
            self._render_preview_async()
        if self._closing:
            return
        self._poll_job = self.root.after(_POLL_MS, self._poll_results)

    def _handle_result(self, item: dict) -> None:
        kind = item.get("kind")
        if kind == "preview":
            if self._closing:
                return  # 关窗后不再贴图（画布已随 root 消失）
            if item.get("gen") != self._preview_gen:
                return  # 过期帧，丢弃
            if item.get("error"):
                self.preview.show_placeholder("error", f"预览失败：\n{item['error']}")
                return
            self.preview.show_image(item["image"], item["page_w"], item["page_h"])
        elif kind == "progress":
            self.progress.configure(value=float(item.get("value", 0.0)))
            self.status.configure(text=str(item.get("text", "")), fg=T.TEXT_DIM)
        elif kind == "log":
            print(str(item.get("text", "")))  # Windows 中文控制台用 [OK]/[FAIL]，不用 ✓
        elif kind == "done":
            self._set_busy(False)
            succeeded = int(item.get("succeeded", 0))
            failed = item.get("failed") or []
            cancelled = bool(item.get("cancelled"))
            if cancelled:
                self.status.configure(
                    text=f"已取消 · 已完成 {succeeded}/{len(self.files)} 个文件", fg=T.WARN)
            else:
                self.progress.configure(value=100.0)
                # 完成态颜色语义：全成绿 / 全败红 / 部分失败橙
                self.status.configure(
                    text=f"完成 · 成功 {succeeded} · 失败 {len(failed)}",
                    fg=T.SUCCESS if not failed else (T.DANGER if succeeded == 0 else T.WARN))
            if failed:
                detail = "\n".join(f"{os.path.basename(p)}：{msg}" for p, msg in failed[:8])
                messagebox.showwarning(TITLE, f"以下文件处理失败：\n{detail}")

    # ------------------------------------------------------------------
    # 关闭清理 / Tk 回调异常
    # ------------------------------------------------------------------

    def _on_close(self) -> None:
        """窗口关闭入口（``WM_DELETE_WINDOW``）：先清理，再销毁。"""
        self._release_resources()
        try:
            self.root.destroy()
        except Exception as exc:
            # 走到这里说明 root 已经没了（协议回调与 destroy 竞态 / 重复关闭）：
            # 窗口本来就不存在，没有可回滚的状态，记一笔即可。
            print(f"[WARN] 销毁窗口失败（可能已销毁）：{exc!r}", file=sys.stderr)

    def _release_resources(self) -> None:
        """关闭前清理，幂等（协议路径与 destroy 守卫各调一次也不会重复执行）。

        依次：置关闭标志与取消标志 → 取消挂起的 after（轮询 / 预览防抖）→ 作废在途
        预览 → 关闭当前文档（GIF / 多帧 TIFF 不关会锁住源文件 WinError 32）→
        有界等待批处理线程自行收尾。
        """
        if self._closing:
            return
        self._closing = True
        self._cancel = True  # 让批处理线程尽快自行了断
        self._cancel_after(self._poll_job)
        self._poll_job = None
        self._cancel_after(self._preview_job)
        self._preview_job = None
        self._preview_gen += 1  # 作废在途预览结果，避免关窗后往死画布贴图
        self._close_doc()
        self._join_batch(_CLOSE_JOIN_SEC)

    def _cancel_after(self, job: Optional[str]) -> None:
        """取消一个 ``after`` 回调。"""
        if job is None:
            return
        try:
            self.root.after_cancel(job)
        except Exception:
            # after_cancel 对三种情况都会抛：回调已执行、id 已被取消、root 已销毁。
            # 前两种说明这次取消是空操作，第三种说明已经没有要取消的东西了 ——
            # 三种都不需要任何补偿动作，所以吞掉是安全的。
            pass

    def _close_doc(self) -> None:
        """关闭当前文档并置空引用（``self.doc`` 绝不留半开对象）。"""
        doc = self.doc
        self.doc = None  # 先摘引用：即使 close 抛也不会留下可误用的半死对象
        if doc is None:
            return
        close = getattr(doc, "close", None)
        if not callable(close):
            return  # 鸭子类型的替身文档（测试桩）没有 close，跳过即可
        try:
            close()
        except Exception as exc:
            # 关闭失败（文件已失效 / 句柄已被系统回收）时没有更多可做的清理，
            # 但仍要记一笔：重复出现说明资源泄漏路径还在。
            print(f"[WARN] 关闭文档失败：{exc!r}", file=sys.stderr)

    def _join_batch(self, timeout: float) -> None:
        """有界等待批处理线程收尾（超时就放弃，绝不拖住关窗）。"""
        thread = self._batch_thread
        if thread is None or not thread.is_alive():
            return
        try:
            thread.join(timeout=timeout)
        except Exception as exc:
            # 只有对当前线程 join 才会抛 RuntimeError；这里 target 恒为子线程，
            # 抛了也不影响销毁流程（线程是 daemon，进程退出不会等它）。
            print(f"[WARN] 等待批处理线程失败：{exc!r}", file=sys.stderr)

    def _on_tk_error(self, exc, val, tb) -> None:
        """Tk 回调里的未捕获异常：写 stderr（冻结后进 runtime.log）+ 状态栏显形。

        冻结版没有控制台，``sys.stderr`` 被重定向到 ``runtime.log``，默认实现只打
        traceback，界面上毫无痕迹 —— 用户看到的只是「点了没反应」。这里补两件事：
        状态栏变红给出摘要；同一类错误只打一次完整 traceback（回调可能每 50ms
        触发一次，全量重复会把 runtime.log 刷爆）。
        """
        key = (str(getattr(exc, "__name__", exc)), str(val))
        first_time = key not in self._tk_error_keys
        self._tk_error_keys.add(key)
        if first_time:
            traceback.print_exception(exc, val, tb)
        else:
            print(f"[ERROR] Tk 回调异常（重复，已省略堆栈）：{key[0]}: {val}", file=sys.stderr)
        try:
            sys.stderr.flush()  # 冻结版是文件流，不 flush 可能丢在缓冲区里
        except Exception:
            pass  # 某些重定向流不支持 flush；日志少一行不影响界面提示
        self._set_status(f"内部错误：{key[0]}: {val}", T.DANGER)


def run() -> None:
    """创建根窗口并进入主循环（供 main.py 与冒烟测试复用）。"""
    from tkinterdnd2 import TkinterDnD
    T.enable_dpi_awareness()
    root = TkinterDnD.Tk()
    App(root)
    root.mainloop()


__all__ = ["App", "run", "TITLE"]
