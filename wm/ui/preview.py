"""预览画布：自适应 fit、页码翻页、三态占位。

三态占位（empty / loading / error）必须分清楚：文件已交进来但画面还未渲染的
窗口期若仍画着「拖放区」，用户会以为没添加成功。

**预览只用于「看」**：水印恒为平铺铺满整页、位置由系统固定，因此这里没有任何
拖拽定位 / 命中判定 / 命中框绘制 —— 空态下点击画布等价于「选择文件」，
除此之外不接受交互。

坐标映射：画布展示的是「按 ``factor`` 等比缩放后的页面」，
    display = origin + page * factor
    page    = (display - origin) / factor
"""

from __future__ import annotations

from typing import Callable, Optional, Tuple

import tkinter as tk
import tkinter.font as tkfont
from PIL import Image, ImageTk

from . import theme as T
from .theme import FluentButton


class PreviewCanvas(tk.Frame):
    """右侧预览区。"""

    def __init__(
        self,
        parent,
        on_page_change: Optional[Callable[[int], None]] = None,
        on_canvas_resize: Optional[Callable[[], None]] = None,
        on_pick_files: Optional[Callable[[], None]] = None,
    ) -> None:
        super().__init__(parent, bg=T.BG)
        self._on_page_change = on_page_change
        self._on_canvas_resize = on_canvas_resize
        self._on_pick_files = on_pick_files

        self._pil: Optional[Image.Image] = None
        self._photo: Optional[ImageTk.PhotoImage] = None
        self._factor: float = 1.0
        self._origin: Tuple[int, int] = (0, 0)
        self._page_size: Tuple[float, float] = (0.0, 0.0)
        self._cursor = ""  # 缓存上次 cursor，Motion 事件只在变化时才 configure
        self._placeholder = ("empty", "把文件拖到这里，或点击「选择文件」")
        self._dots = 0
        self._load_job: Optional[str] = None
        self._info = ""
        self._page_index = 0
        self._page_total = 1

        head = tk.Frame(self, bg=T.BG)
        head.pack(fill="x", padx=T.px(T.PANEL_PAD), pady=(T.px(T.SP_LG), T.px(T.SP_SM)))
        tk.Label(head, text="预览", bg=T.BG, fg=T.TEXT,
                 font=(T.UI_FAMILY, T.sf(11)), anchor="w").pack(side="left")
        self.info_label = tk.Label(head, text="", bg=T.BG, fg=T.TEXT_FAINT,
                                   font=(T.UI_FAMILY, T.sf(9)), anchor="e")
        self.info_label.pack(side="right")

        self.canvas = tk.Canvas(self, bg=T.CANVAS_BG, highlightthickness=T.px(1),
                                highlightbackground=T.BORDER_STRONG, bd=0)
        self.canvas.pack(fill="both", expand=True, padx=T.px(T.PANEL_PAD))

        nav = tk.Frame(self, bg=T.BG)
        nav.pack(fill="x", padx=T.px(T.PANEL_PAD), pady=(T.px(T.SP_SM), T.px(T.SP_LG)))
        self._nav = nav
        self.prev_btn = FluentButton(nav, "上一页", lambda: self._page_step(-1),
                                     kind="secondary", bg=T.BG, width=T.px(74))
        self.prev_btn.pack(side="left")
        self.page_label = tk.Label(nav, text="1 / 1", bg=T.BG, fg=T.TEXT_DIM,
                                   font=(T.UI_FAMILY, T.sf(9)))
        self.page_label.pack(side="left", padx=T.px(T.SP_MD))
        self.next_btn = FluentButton(nav, "下一页", lambda: self._page_step(1),
                                     kind="secondary", bg=T.BG, width=T.px(74))
        self.next_btn.pack(side="left")

        self.canvas.bind("<Configure>", self._on_configure)
        self.canvas.bind("<ButtonPress-1>", self._on_press)
        self.canvas.bind("<Motion>", self._on_hover)
        self._update_nav_visibility()
        self._redraw()

    # ------------------------------------------------------------------
    # 显示
    # ------------------------------------------------------------------

    def canvas_size(self) -> Tuple[int, int]:
        """画布可用尺寸（未就绪时返回 0,0）。"""
        width = self.canvas.winfo_width()
        height = self.canvas.winfo_height()
        if width <= 1 or height <= 1:
            return 0, 0
        return width, height

    def show_image(self, pil: Image.Image, page_w: float, page_h: float) -> None:
        """展示一张已渲染好的预览图（display 像素尺寸）。"""
        self._cancel_load_anim()
        self._pil = pil
        self._page_size = (float(page_w), float(page_h))
        self._factor = (pil.size[0] / page_w) if page_w > 0 else 1.0
        self._redraw()
        if page_w > 0:
            self.info_label.configure(
                text=f"原图 {int(round(page_w))}×{int(round(page_h))} · 预览 {self._factor * 100:.0f}%")
        else:
            self.info_label.configure(text="")

    def show_placeholder(self, kind: str, message: str) -> None:
        """展示三态占位之一：empty / loading / error。"""
        self._cancel_load_anim()
        self._pil = None
        self._photo = None
        self._placeholder = (kind, message)
        self.info_label.configure(text="")
        if kind == "loading":
            self._dots = 0
            self._schedule_load_anim()
        self._redraw()

    def set_nav(self, index: int, total: int) -> None:
        """更新页码。"""
        self._page_index = int(index)
        self._page_total = max(1, int(total))
        self.page_label.configure(text=f"{self._page_index + 1} / {self._page_total}")
        # 分页到边界时禁用对应按钮，给「不能再翻」一个明确的状态反馈
        has_pages = self._page_total > 1
        self.prev_btn.set_enabled(has_pages and self._page_index > 0)
        self.next_btn.set_enabled(has_pages and self._page_index < self._page_total - 1)
        self._update_nav_visibility()

    def _update_nav_visibility(self) -> None:
        # 只在多页时显示分页器，避免首屏挂一个无意义的分页器
        if self._page_total > 1:
            if not self._nav.winfo_ismapped():
                self._nav.pack(fill="x", padx=T.px(T.PANEL_PAD),
                               pady=(T.px(T.SP_SM), T.px(T.SP_LG)))
        else:
            if self._nav.winfo_ismapped():
                self._nav.pack_forget()

    def _page_step(self, delta: int) -> None:
        if self._on_page_change is not None:
            self._on_page_change(delta)

    # ------------------------------------------------------------------
    # 绘制
    # ------------------------------------------------------------------

    def _redraw(self) -> None:
        self.canvas.delete("all")
        width = self.canvas.winfo_width()
        height = self.canvas.winfo_height()
        if width <= 1 or height <= 1:
            return
        if self._pil is not None:
            self._photo = ImageTk.PhotoImage(self._pil)
            img_w, img_h = self._pil.size
            ox = max(0, (width - img_w) // 2)
            oy = max(0, (height - img_h) // 2)
            self._origin = (ox, oy)
            self.canvas.create_image(ox, oy, anchor="nw", image=self._photo)
        else:
            kind, message = self._placeholder
            color = {
                "empty": T.TEXT_DIM,
                "loading": T.TEXT_DIM,
                "error": T.DANGER,
            }.get(kind, T.TEXT_FAINT)
            if kind == "empty":
                # 虚线圆角框构成标准「拖放区」；边框对比度按画布底色取 BORDER_STRONG
                inset = T.px(28)
                T.round_rect(self.canvas, inset, inset, width - inset, height - inset,
                             r=T.px(10), fill="", outline=T.BORDER_STRONG,
                             dash=(4, 3), width=T.px(1))
            font = tkfont.Font(family=T.UI_FAMILY, size=T.sf(12 if kind == "empty" else 11))
            if kind == "loading":
                # 动态省略号：画面静止太久会被当成卡死
                text = message + "." * self._dots
            else:
                text = message
            self.canvas.create_text(width // 2, height // 2, text=text,
                                    fill=color, font=font, width=width - T.px(40),
                                    justify="center")
            if kind == "empty":
                self.canvas.create_text(width // 2, height // 2 + T.px(28),
                                        text="支持：png / jpg / jpeg / bmp / webp / tif / tiff / gif / pdf",
                                        fill=T.TEXT_FAINT, font=tkfont.Font(family=T.UI_FAMILY,
                                                                            size=T.sf(9)))

    # loading 省略号动画 ----------------------------------------------------

    def _schedule_load_anim(self) -> None:
        if self._load_job is not None:
            try:
                self.after_cancel(self._load_job)
            except Exception:
                pass
        self._load_job = self.after(400, self._tick_loading)

    def _tick_loading(self) -> None:
        self._load_job = None
        self._dots = (self._dots + 1) % 4
        self._redraw()
        # 只要还停在 loading 占位就续接下一帧（_redraw 本身不负责调度）
        if self._pil is None and self._placeholder[0] == "loading":
            self._schedule_load_anim()

    def _cancel_load_anim(self) -> None:
        if self._load_job is not None:
            try:
                self.after_cancel(self._load_job)
            except Exception:
                pass
            self._load_job = None

    def _on_configure(self, _event=None) -> None:
        self._redraw()
        if self._on_canvas_resize is not None:
            self._on_canvas_resize()

    # ------------------------------------------------------------------
    # 指针交互（空态点击 = 选择文件；无拖拽定位）
    # ------------------------------------------------------------------

    def _set_cursor(self, cursor: str) -> None:
        """仅当光标状态变化时才 configure（Motion 高频触发，避免重复配置开销）。"""
        if cursor != self._cursor:
            self._cursor = cursor
            try:
                self.canvas.configure(cursor=cursor)
            except Exception:
                pass

    def _on_hover(self, _event=None) -> None:
        # 空态占位可点击选文件，给一个手型光标提示（预览本身不接受拖拽定位）
        if self._pil is None:
            if self._placeholder[0] == "empty" and self._on_pick_files is not None:
                self._set_cursor("hand2")
            return
        self._set_cursor("")

    def _on_press(self, _event=None) -> None:
        # 空态：点击画布任意位置等价于「选择文件」
        if self._pil is None and self._placeholder[0] == "empty" \
                and self._on_pick_files is not None:
            self._on_pick_files()


__all__ = ["PreviewCanvas"]
