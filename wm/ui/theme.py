"""Windows 11 Fluent 浅色主题 + 自绘控件套件。

铁律（来自主题技能文档）：
    * 浅底上悬停 / 按下必须**向深色**变化，不能"调亮"；
    * 所有颜色集中在本模块，控件不得出现字面色值；
    * 次要按钮靠描边区分层级；
    * 边界的对比度是对「它自己的背景」算的 —— 画布 / 壳底上要用 ``BORDER_STRONG``；
    * 「类型」与「状态」两套色，绝不共用；
    * Tk 做不到阴影，层级模型主动声明为「无投影」（最多三层色差）。

字号一律经 ``sf()`` 返回**负的整数像素**（正数是"点"，会落在非整数像素上发虚）；
尺寸一律经 ``px()``（DPI 缩放）。
"""

from __future__ import annotations

from typing import Callable, List, Optional, Tuple

import tkinter as tk
import tkinter.font as tkfont
from tkinter import ttk

from .. import fonts

# ---------------------------------------------------------------------------
# DPI
# ---------------------------------------------------------------------------

SCALE: float = 1.0


def enable_dpi_awareness() -> float:
    """声明进程 DPI 感知并返回缩放系数。**必须在 ``tk.Tk()`` 之前调用。**"""
    global SCALE
    try:
        import ctypes

        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            ctypes.windll.user32.SetProcessDPIAware()
        dc = ctypes.windll.user32.GetDC(0)
        dpi = ctypes.windll.gdi32.GetDeviceCaps(dc, 88)
        ctypes.windll.user32.ReleaseDC(0, dc)
        SCALE = (dpi / 96.0) if dpi else 1.0
    except Exception:
        SCALE = 1.0
    return SCALE


def px(value: float) -> int:
    """逻辑像素 -> 物理像素（下限 1）。"""
    return max(1, int(round(float(value) * SCALE)))


def sf(pt: float) -> int:
    """字号令牌：点数换算为整数像素后取负，明确告诉 Tk「这是像素字号」。"""
    return -max(1, int(round(float(pt) * 96.0 / 72.0 * SCALE)))


# ---------------------------------------------------------------------------
# 配色令牌
# ---------------------------------------------------------------------------

BG = "#f4f5f8"
PANEL = "#ffffff"
PANEL_ALT = "#fafbfd"
CONTROL = "#ffffff"
CONTROL_HI = "#f1f2f6"
BORDER = "#8f92a0"
BORDER_STRONG = "#7f8391"
BORDER_SOFT = "#e3e5ea"
BORDER_HAIR = "#eceef2"
TEXT = "#1a1a1f"
TEXT_DIM = "#545663"
TEXT_FAINT = "#6b6e7d"
TEXT_MUTE = "#8d90a0"
ACCENT = "#0f6cbd"
ACCENT_HOVER = "#1a7fd4"
ACCENT_DOWN = "#0b5394"
ACCENT_SOFT = "#e7f1fb"
ACCENT_DIM = "#cfe4f7"
DANGER = "#c02a1a"
SUCCESS = "#0f7b3f"
WARN = "#8a5200"
CANVAS_BG = "#eaecef"
ROW_ALT = "#fafbfd"
ROW_HOVER = "#f1f3f7"
SELECT = "#dbeafc"
SELECT_BAR = "#0f6cbd"

#: 类型标：中性色，绝不复用成功 / 危险
BADGE_BG = "#eef0f5"
BADGE_FG = "#4b5566"

#: 滑块（Canvas 自绘）：轨道分「已填充 / 未填充」两段，滑块带描边 + 投影
SLIDER_TRACK = "#dcdfe6"        # 未填充轨道
SLIDER_TRACK_EDGE = "#c2c6d0"   # 未填充轨道描边（让轨道在白底上更可辨）
SLIDER_TRACK_OFF = "#e8eaef"    # 禁用态轨道
SLIDER_FILL = ACCENT            # 已填充轨道（主题色，与未填充段强对比）
SLIDER_FILL_OFF = "#c9ccd4"     # 禁用态已填充
KNOB = "#ffffff"                # 滑块本体
KNOB_OFF = "#c6c6cf"            # 禁用态滑块
KNOB_SHADOW = "#c8ccd6"         # 滑块投影（比本体暗，形成「浮起」感）
TRACK_BAR = "#d5d7de"           # 进度条轨道
BTN_BG = "#ffffff"
BTN_BG_HOVER = "#f2f3f7"
BTN_BG_DOWN = "#e6e7ee"
SWITCH_OFF = "#8f92a0"
SWITCH_OFF_HOVER = "#787b8a"
FOCUS = "#0f6cbd"
BADGE_OK = "#0f7b3f"
BADGE_FAIL = "#c02a1a"

#: uTools 式极简面板零件的补充令牌
SECT_TITLE = "#2f2f36"
SECT_BAR = "#ebebf1"
FIELD_LINE = "#d9d9de"
FIELD_LINE_HI = "#b6b6c0"

#: 间距语义刻度（所有 pady/padx 只允许从这几档取）
SP_XS, SP_SM, SP_MD, SP_LG, SP_XL, SP_XXL = 4, 6, 8, 12, 16, 20
PANEL_PAD = 16

#: 界面字体家族（优先 HarmonyOS Sans SC）
UI_FAMILY = fonts.default_family()


# ---------------------------------------------------------------------------
# 混色工具
# ---------------------------------------------------------------------------

def _hex2rgb(color: str) -> tuple:
    color = color.lstrip("#")
    return tuple(int(color[i:i + 2], 16) for i in (0, 2, 4))


def mix(color1: str, color2: str, t: float) -> str:
    """在 ``color1`` 与 ``color2`` 之间线性插值，t=0 返回 color1。"""
    a, b = _hex2rgb(color1), _hex2rgb(color2)
    return "#%02x%02x%02x" % tuple(round(a[i] + (b[i] - a[i]) * t) for i in range(3))


def round_rect(canvas: tk.Canvas, x0, y0, x1, y1, r=0, **kw):
    """Canvas 上画圆角矩形；r<=0 时退化为普通矩形（保留同一套调用）。"""
    if r <= 0:
        return canvas.create_rectangle(x0, y0, x1, y1, **kw)
    points = [x0 + r, y0, x1 - r, y0, x1, y0, x1, y0 + r, x1, y1 - r, x1, y1,
              x1 - r, y1, x0 + r, y1, x0, y1, x0, y1 - r, x0, y0 + r, x0, y0]
    return canvas.create_polygon(points, smooth=True, **kw)


# ---------------------------------------------------------------------------
# ttk 样式
# ---------------------------------------------------------------------------

def install_ttk_styles(root: tk.Misc) -> ttk.Style:
    """套用 App.* 系列 ttk 样式（原生 ttk 不吃 bg/fg，必须走 style）。"""
    style = ttk.Style(root)
    try:
        style.theme_use("clam")
    except tk.TclError:
        pass

    style.configure("App.TCombobox", fieldbackground=CONTROL, background=CONTROL,
                    foreground=TEXT, arrowcolor=TEXT_DIM, bordercolor=BORDER,
                    lightcolor=CONTROL, darkcolor=CONTROL,
                    selectbackground=SELECT, selectforeground=TEXT, padding=4)
    style.map("App.TCombobox",
              fieldbackground=[("readonly", CONTROL), ("disabled", CONTROL_HI)],
              foreground=[("disabled", TEXT_MUTE)],
              bordercolor=[("focus", ACCENT)])

    # 注：滑块已改为 Canvas 自绘（``FlatScale``），不再有 App.Horizontal.TScale ——
    # ttk::scale 的 trough 是单一元素，没有「已填充部分」这个样式位。
    style.configure("App.Horizontal.TProgressbar", troughcolor=TRACK_BAR,
                    background=ACCENT, bordercolor=TRACK_BAR,
                    lightcolor=ACCENT, darkcolor=ACCENT, thickness=px(6))

    # 细体扁平滚动条：与 Fluent 极简面板协调（默认 ttk 滚动条灰块 + 3D 边缘太重）
    style.configure("App.Vertical.TScrollbar", troughcolor=PANEL,
                    background=BORDER_SOFT, bordercolor=PANEL,
                    lightcolor=BORDER_SOFT, darkcolor=BORDER_SOFT,
                    arrowcolor=TEXT_MUTE, gripcount=0, arrowsize=10)
    style.map("App.Vertical.TScrollbar",
              background=[("active", BORDER), ("pressed", ACCENT),
                          ("disabled", BORDER_SOFT)])
    return style


def style_combobox_popdown(combobox, style_name: str = "App.Vertical.TScrollbar",
                           list_font: Optional[Tuple] = None) -> bool:
    """把下拉框**弹出列表**统一到项目样式（宽度 / 圆角 / 轨道 / 悬浮与按下色）。

    ``ttk::combobox`` 的弹出窗口是它自己内部建的：``<cb>.popdown``（Toplevel），
    里面是 ``<cb>.popdown.f``（Frame）+ ``<cb>.popdown.f.l``（Listbox）+
    ``<cb>.popdown.f.sb``（ttk::scrollbar）—— 列表框与滚动条都挂在那个内容
    Frame 下，不是直接挂在 popdown 上。那个滚动条**不带任何 style**，于是走
    默认主题的宽滚条，和面板里 ``App.Vertical.TScrollbar`` 的细条明显不一致。

    这里先用 ``ttk::combobox::PopdownWindow`` 把弹出窗口**预先建出来**（否则要等
    用户第一次点开才存在），再给它的滚动条挂上同一个 style —— 宽度、滑块圆角、
    轨道底色、hover / pressed 颜色全部由该 style 决定，因此自然与程序其余位置一致。

    ``list_font`` 顺手把列表文字设成界面字体（默认 Tk 字体与输入框不一致）。

    **只接受 ``ttk::combobox``**：``ttk::combobox::PopdownWindow`` 对任意 Tk 窗口
    （Label / Entry / Frame…）都会「成功」返回并**凭空预建出** ``<widget>.popdown``
    这个残留窗口，属于无意义的副作用。这里先判控件类型，非下拉框直接返回 False。
    """
    try:
        # 判类型必须在动手之前：Tcl 层不校验，只能由调用方自己挡。
        if str(combobox.winfo_class()) != "TCombobox":
            return False
        popdown = combobox.tk.call("ttk::combobox::PopdownWindow", str(combobox))
        if popdown and combobox.tk.call("winfo", "exists", popdown + ".f.sb"):
            combobox.tk.call(popdown + ".f.sb", "configure", "-style", style_name)
        # 列表框与滚动条同在 popdown 的内容 frame 里（...popdown.f.l / ...popdown.f.sb）
        if list_font is not None and combobox.tk.call("winfo", "exists", popdown + ".f.l"):
            combobox.tk.call(popdown + ".f.l", "configure", "-font", list_font)
        return True
    except (tk.TclError, AttributeError):
        return False


# ---------------------------------------------------------------------------
# 自绘控件
# ---------------------------------------------------------------------------

def section(parent: tk.Misc, title: str, bg: str = PANEL,
            pady=(SP_XXL, SP_MD)) -> tk.Frame:
    """区块标题：accent 竖条 + 加粗标题一行 + 其下一整条细分隔线。

    层级表达（不依赖编号）：竖条标记区块起点，加粗标题与正文字段拉开字重差，
    分隔线收束标题区；区块之间的空隙（pady[0]=SP_XXL）大于区块内部字段间距，
    让「区块 > 字段」的层级靠留白即可读出。
    """
    wrap = tk.Frame(parent, bg=bg)
    wrap.pack(fill="x", pady=(px(pady[0]), px(pady[1])))
    head = tk.Frame(wrap, bg=bg)
    head.pack(fill="x")
    # accent 竖条：区块的视觉锚点（替代旧的「② ③ ④」数字编号）
    bar = tk.Frame(head, bg=ACCENT, width=px(3), height=px(12))
    bar.pack(side="left")
    bar.pack_propagate(False)
    tk.Label(head, text=title, bg=bg, fg=SECT_TITLE,
             font=(UI_FAMILY, sf(10), "bold"), anchor="w"
             ).pack(side="left", padx=(px(SP_SM), 0))
    tk.Frame(wrap, bg=SECT_BAR, height=px(1)).pack(fill="x", pady=(px(SP_SM), 0))
    return wrap


def field_label(parent: tk.Misc, text: str, bg: str = PANEL) -> tk.Label:
    """字段标签。"""
    return tk.Label(parent, text=text, bg=bg, fg=TEXT_DIM, font=(UI_FAMILY, sf(9)), anchor="w")


class FlatScale(tk.Canvas):
    """自绘横向滑块：轨道分「已填充 / 未填充」两段 + 带描边与投影的圆形滑块。

    **为什么不用 ``ttk.Scale``**：ttk::scale 的 trough 是**单一元素**，根本没有
    「已填充部分」这个样式位（那是 Progressbar 才有的概念），滑块的直径 / 描边 /
    投影在 clam 主题下也都改不动。这里沿用项目既有做法（开关 ``ToggleSwitch``、
    按钮 ``FluentButton`` 均为 Canvas 自绘）自己画，才能同时满足「对比更强」与
    「已填充 / 未填充可辨」两条要求。

    交互：轨道任意处点击即跳位，按住可拖动；悬停 / 按下 / 禁用各有独立视觉。
    """

    H = 20          # 控件总高（逻辑 px）
    TRACK = 6       # 轨道粗细
    KNOB = 16       # 滑块直径
    RING = 2        # 滑块描边宽

    def __init__(self, parent, from_: float = 0.0, to: float = 1.0,
                 orient: str = "horizontal", command: Optional[Callable[[float], None]] = None,
                 bg: str = PANEL, **_ignored) -> None:
        super().__init__(parent, height=px(self.H), bg=bg,
                         highlightthickness=0, bd=0, cursor="hand2")
        self._lo, self._hi = float(from_), float(to)
        self._cmd = command
        self._bg_color = bg
        self._value = self._lo
        self._hover = False
        self._press = False
        self._disabled = False
        # 注意：不能叫 _w / _h —— 那是 tk.Misc 的保留属性（Tcl 路径名）
        self._cw, self._ch = px(1), px(self.H)

        self.bind("<Configure>", self._on_configure)
        self.bind("<Button-1>", self._on_press)
        self.bind("<B1-Motion>", self._on_drag)
        self.bind("<ButtonRelease-1>", self._on_release)
        self.bind("<Enter>", lambda _e: self._set_hover(True))
        self.bind("<Leave>", lambda _e: self._set_hover(False))

    # -- 几何 -------------------------------------------------------------

    def _pad(self) -> float:
        """两端留半个滑块的余量，保证滑块在最值处也完整可见。"""
        return px(self.KNOB) / 2.0

    def _span(self) -> float:
        return max(1.0, self._cw - 2.0 * self._pad())

    def _x_of(self, value: float) -> float:
        ratio = 0.0 if self._hi <= self._lo else (value - self._lo) / (self._hi - self._lo)
        ratio = min(1.0, max(0.0, ratio))
        return self._pad() + ratio * self._span()

    def _value_of(self, x: float) -> float:
        ratio = min(1.0, max(0.0, (x - self._pad()) / self._span()))
        return self._lo + ratio * (self._hi - self._lo)

    # -- 交互 -------------------------------------------------------------

    def _on_configure(self, _event=None) -> None:
        self._cw = max(px(1), self.winfo_width())
        self._ch = max(px(1), self.winfo_height())
        self._redraw()

    def _on_press(self, event) -> None:
        if self._disabled:
            return
        self._press = True
        self._apply_x(event.x)

    def _on_drag(self, event) -> None:
        if self._disabled:
            return
        self._apply_x(event.x)

    def _on_release(self, _event=None) -> None:
        self._press = False
        self._redraw()

    def _set_hover(self, on: bool) -> None:
        self._hover = bool(on)
        self._redraw()

    def _apply_x(self, x: float) -> None:
        self._value = self._value_of(x)
        self._redraw()
        if self._cmd is not None:
            self._cmd(self._value)

    # -- 绘制 -------------------------------------------------------------

    def _redraw(self) -> None:
        self.delete("all")
        cy = self._ch / 2.0
        th = px(self.TRACK)
        pad = self._pad()
        right = max(pad, self._cw - pad)
        kx = self._x_of(self._value)

        if self._disabled:
            track_c, edge_c = SLIDER_TRACK_OFF, SLIDER_TRACK_OFF
            fill_c = SLIDER_FILL_OFF
            knob_fill = knob_line = shadow_c = KNOB_OFF
        else:
            track_c, edge_c = SLIDER_TRACK, SLIDER_TRACK_EDGE
            fill_c = SLIDER_FILL
            knob_fill = ACCENT_SOFT if self._press else KNOB
            knob_line = ACCENT_DOWN if self._press else (ACCENT_HOVER if self._hover else ACCENT)
            shadow_c = KNOB_SHADOW

        # 未填充轨道（整条，带描边以便在白底上可辨）
        top, bottom = cy - th / 2.0, cy + th / 2.0
        round_rect(self, pad, top, right, bottom, r=th / 2.0,
                   fill=track_c, outline=edge_c, width=px(1))
        # 已填充轨道：从起点到滑块中心 —— 与未填充段形成明确区分
        if kx > pad + 0.5:
            # 半径按实际长度钳制：填充段极短时 r 若超过半宽，圆角会画歪
            round_rect(self, pad, top, kx, bottom, r=min(th / 2.0, (kx - pad) / 2.0),
                       fill=fill_c, outline=fill_c)

        r = px(self.KNOB) / 2.0
        # 悬停 / 按下时加一圈柔光，强化「可拖动」的可供性
        if not self._disabled and (self._hover or self._press):
            self.create_oval(kx - r - px(3), cy - r - px(3), kx + r + px(3), cy + r + px(3),
                             outline=ACCENT_DIM, width=px(2))
        # 投影：先画一个下移 1px 的暗色圆，本体覆盖后底部留一道月牙 —— 浮起感
        self.create_oval(kx - r, cy - r + px(1), kx + r, cy + r + px(1),
                         fill=shadow_c, outline=shadow_c)
        # 本体：白底 + 主题色描边，与灰色轨道强对比
        self.create_oval(kx - r, cy - r, kx + r, cy + r,
                         fill=knob_fill, outline=knob_line, width=px(self.RING))

    # -- 公共 API（与 ttk.Scale 保持兼容的最小集） ------------------------

    def get(self) -> float:
        return self._value

    def set(self, value: float) -> None:
        """设值并刷新外观；**不**回调 command（与 ttk.Scale 行为一致）。"""
        self._value = min(max(float(value), self._lo), self._hi)
        self._redraw()

    def state(self, states=None):
        if states is None:
            return ("disabled",) if self._disabled else ()
        self._disabled = "disabled" in states
        self._redraw()
        return states


class SliderField(tk.Frame):
    """「标签 + 数字框 + 滑块（+ 提示）」的组合字段。"""

    def __init__(self, parent, label: str, lo: float, hi: float, value: float,
                 step: float, command: Callable[[float], None], unit: str = "",
                 decimals: int = 0, show_entry: bool = True, hint: Optional[str] = None,
                 bg: str = PANEL) -> None:
        super().__init__(parent, bg=bg)
        self._lo, self._hi, self._step = float(lo), float(hi), float(step)
        self._decimals = int(decimals)
        self._command = command
        self._bg = bg
        self._enabled = True
        self._value = min(max(float(value), self._lo), self._hi)
        self._hint_text = hint or ""
        self._invalid_job: Optional[str] = None

        head = tk.Frame(self, bg=bg)
        head.pack(fill="x")
        self.label_widget = tk.Label(head, text=label, bg=bg, fg=TEXT_DIM,
                                     font=(UI_FAMILY, sf(9)), anchor="w")
        self.label_widget.pack(side="left")

        self.unit_label: Optional[tk.Label] = None
        self.entry: Optional[tk.Entry] = None
        self.var: Optional[tk.StringVar] = None
        if show_entry:
            if unit:
                self.unit_label = tk.Label(head, text=unit, bg=bg, fg=TEXT_FAINT,
                                           font=(UI_FAMILY, sf(9)), anchor="e")
                self.unit_label.pack(side="right", padx=(px(SP_XS), 0))
            self.var = tk.StringVar(value=self._format(self._value))
            # 常驻 1px 边框：既是输入框可视边界，也承担焦点环（聚焦变 accent）
            self.entry = tk.Entry(head, textvariable=self.var, width=5, justify="right",
                                  bg=bg, fg=TEXT, relief="flat", bd=0,
                                  highlightthickness=px(1), highlightbackground=FIELD_LINE,
                                  highlightcolor=ACCENT, font=(UI_FAMILY, sf(11)))
            self.entry.pack(side="right")
            self.entry.bind("<Return>", self._commit_entry)
            self.entry.bind("<FocusOut>", self._commit_entry)
            self.entry.bind("<Up>", lambda _e: self._nudge(1))
            self.entry.bind("<Down>", lambda _e: self._nudge(-1))
            self.entry.bind("<MouseWheel>", self._on_wheel)

        self.scale = FlatScale(self, from_=self._lo, to=self._hi,
                               command=self._on_scale)
        self.scale.set(self._value)
        self.scale.pack(fill="x", pady=(px(SP_SM), 0))

        # 提示行按需显示：没有文案时**不占位**（否则空 Label 会白留一行高度）；
        # 有文案时保留控件本身，便于随模式动态切换（如「四周边距」两种含义）。
        self.hint_label = tk.Label(self, text=self._hint_text, bg=bg, fg=TEXT_FAINT,
                                   font=(UI_FAMILY, sf(9)), anchor="w", justify="left",
                                   wraplength=px(300))
        self._sync_hint_visibility()

    def _sync_hint_visibility(self, force_show: bool = False) -> None:
        """提示行显隐：有文案（或需强制显示报错）时占位，否则收起不占位。"""
        if force_show or self._hint_text:
            if not self.hint_label.winfo_manager():
                self.hint_label.pack(fill="x", pady=(px(SP_SM), 0))
        else:
            self.hint_label.pack_forget()

    def set_hint(self, text: str) -> None:
        """动态替换提示文案；传空串则隐藏提示行（不占位）。"""
        self._hint_text = text or ""
        self.hint_label.configure(text=self._hint_text, fg=TEXT_FAINT)
        self._sync_hint_visibility()

    # -- 校验提示 -----------------------------------------------------------

    def _flag_invalid(self) -> None:
        """输入非法：输入框红框 + 提示行红字，短暂显示后自动复原。"""
        if self._invalid_job is not None:
            try:
                self.after_cancel(self._invalid_job)
            except Exception:
                pass
        if self.entry is not None:
            self.entry.configure(highlightbackground=DANGER, highlightcolor=DANGER)
        self.hint_label.configure(
            text=f"请输入 {self._format(self._lo)} – {self._format(self._hi)} 之间的数值",
            fg=DANGER)
        self._sync_hint_visibility(force_show=True)
        self._invalid_job = self.after(1200, self._clear_invalid)

    def _clear_invalid(self) -> None:
        self._invalid_job = None
        if self.entry is not None:
            self.entry.configure(highlightbackground=FIELD_LINE, highlightcolor=ACCENT)
        self.hint_label.configure(text=self._hint_text, fg=TEXT_FAINT)
        self._sync_hint_visibility()

    # -- 内部 -------------------------------------------------------------

    def _format(self, value: float) -> str:
        """按 decimals 格式化；**decimals 为 0 而值又非整数时自动多留 1 位**。

        字号下界是 0.5、步长却是 1 —— 0.5 在整数网格里表示不出来，硬按 0 位小数
        格式化会显示成 "0"（Python 的 format 走「四舍六入五成双」），与真实值不符。
        这里对非整数值回退到 1 位小数，守住「显示什么就是什么」。
        """
        if self._decimals <= 0 and abs(value - round(value)) > 1e-9:
            return f"{value:.1f}"
        return f"{value:.{self._decimals}f}"

    def _snap(self, value: float) -> float:
        step = self._step if self._step > 0 else 1.0
        return min(max(round(value / step) * step, self._lo), self._hi)

    def _on_scale(self, raw: str) -> None:
        try:
            value = self._snap(float(raw))
        except (TypeError, ValueError, OverflowError):
            return
        self._value = value
        if self.var is not None:
            self.var.set(self._format(value))
        self._emit()

    def _nudge(self, direction: int) -> str:
        self.set(self._value + direction * self._step)
        return "break"

    def _on_wheel(self, event):
        # 仅当输入框持有焦点时才接管滚轮（滚一格改一个步长），否则**放行**事件
        # 让滚动容器正常滚动 —— 旧版无条件 "break" 会吞掉事件，导致鼠标悬停
        # 在数字框上时整个面板滚不动。
        if self.entry is not None and self.focus_get() is self.entry:
            self.set(self._value + (1 if event.delta > 0 else -1) * self._step)
            return "break"
        return None

    def _commit_entry(self, _event=None) -> None:
        if self.entry is None or self.var is None:
            return
        raw = self.var.get().strip()
        try:
            value = self._snap(float(raw))
        except (TypeError, ValueError, OverflowError):
            # 解析失败回退上次有效值，并给出明确的校验提示（不再静默）
            self._flag_invalid()
            value = self._value
        self.var.set(self._format(value))
        self.scale.set(value)
        self._value = value
        self._emit()

    def _emit(self) -> None:
        if self._enabled and self._command is not None:
            self._command(self._value)

    # -- 公共 API ---------------------------------------------------------

    def get(self) -> float:
        return self._value

    def set(self, value: float, notify: bool = True) -> None:
        value = self._snap(float(value))
        self._value = value
        if self.var is not None:
            self.var.set(self._format(value))
        self.scale.set(value)
        if notify:
            self._emit()

    def set_enabled(self, enabled: bool) -> None:
        """禁用态三通道同时变淡：数值 + 标签 + 单位，滑块一并禁用。"""
        self._enabled = bool(enabled)
        try:
            self.scale.state(["!disabled"] if enabled else ["disabled"])
        except tk.TclError:
            pass
        if self.entry is not None:
            self.entry.configure(state="normal" if enabled else "readonly",
                                 fg=TEXT if enabled else TEXT_FAINT)
        self.label_widget.configure(fg=TEXT_DIM if enabled else TEXT_MUTE)
        if self.unit_label is not None:
            self.unit_label.configure(fg=TEXT_FAINT if enabled else TEXT_MUTE)


class ToggleSwitch(tk.Frame):
    """胶囊开关（开关在左、标签在右）。"""

    W, H = 42, 22

    def __init__(self, parent, text: str, command: Callable[[bool], None],
                 value: bool = False, bg: str = PANEL) -> None:
        super().__init__(parent, bg=bg)
        self._command = command
        self._value = bool(value)
        self._hover = False
        self._bg = bg
        # 注意：属性名不能叫 _w / _h —— _w 是 tk.Misc 的**保留属性**（控件 Tcl 路径名），
        # 覆盖它会让后续 self.delete(...) 之类调用报 "invalid command name"。
        self._cw, self._ch = px(self.W), px(self.H)
        self.canvas = tk.Canvas(self, width=self._cw, height=self._ch, bg=bg,
                                highlightthickness=0, bd=0, cursor="hand2")
        self.canvas.pack(side="left")
        self.label = tk.Label(self, text=text, bg=bg, fg=TEXT_DIM,
                              font=(UI_FAMILY, sf(9)), anchor="w")
        self.label.pack(side="left", padx=(px(SP_SM), 0))
        for widget in (self.canvas, self.label):
            widget.bind("<Button-1>", self._toggle)
            widget.bind("<Enter>", self._on_enter)
            widget.bind("<Leave>", self._on_leave)
        self._redraw()

    def _toggle(self, _event=None) -> None:
        self._value = not self._value
        self._redraw()
        if self._command is not None:
            self._command(self._value)

    def _on_enter(self, _event=None) -> None:
        self._hover = True
        self.canvas.configure(cursor="hand2")
        self._redraw()

    def _on_leave(self, _event=None) -> None:
        self._hover = False
        self._redraw()

    def _redraw(self) -> None:
        self.canvas.delete("all")
        h = self._ch
        if self._value:
            track = ACCENT_HOVER if self._hover else ACCENT
        else:
            track = SWITCH_OFF_HOVER if self._hover else SWITCH_OFF
        round_rect(self.canvas, 0, 0, self._cw, h, r=h / 2, fill=track, outline=track)
        pad = px(2)
        knob_d = h - pad * 2
        knob_x = (self._cw - pad - knob_d) if self._value else pad
        self.canvas.create_oval(knob_x, pad, knob_x + knob_d, pad + knob_d,
                                fill=KNOB, outline=KNOB)

    def get(self) -> bool:
        return self._value

    def set(self, value: bool, notify: bool = False) -> None:
        self._value = bool(value)
        self._redraw()
        if notify and self._command is not None:
            self._command(self._value)


class FluentButton(tk.Canvas):
    """圆角按钮：primary / secondary / ghost / danger。"""

    def __init__(self, parent, text: str, command: Callable[[], None],
                 kind: str = "secondary", bg: str = PANEL, width: Optional[int] = None) -> None:
        self._text = text
        self._command = command
        self._kind = kind
        self._bg = bg
        self._hover = False
        self._pressed = False
        self._enabled = True
        self._font = tkfont.Font(family=UI_FAMILY, size=sf(9))
        w = width if width is not None else self._font.measure(text) + px(30)
        h = px(30)
        super().__init__(parent, width=w, height=h, bg=bg, highlightthickness=0,
                         bd=0, cursor="hand2")
        self._cw, self._ch = w, h  # 不用 _w / _h：_w 是 tk.Misc 保留属性
        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)
        self.bind("<ButtonPress-1>", self._on_press)
        self.bind("<ButtonRelease-1>", self._on_release)
        self._redraw()

    def _palette(self):
        if not self._enabled:
            if self._kind == "primary":
                return mix(ACCENT, PANEL, 0.55), PANEL, "#ffffff"
            return mix(BTN_BG_HOVER, PANEL, 0.5), BORDER_SOFT, TEXT_MUTE
        if self._kind == "primary":
            fill = ACCENT_DOWN if self._pressed else (ACCENT_HOVER if self._hover else ACCENT)
            return fill, fill, "#ffffff"
        if self._kind == "ghost":
            fill = ACCENT_SOFT if (self._hover or self._pressed) else self._bg
            return fill, fill, ACCENT
        if self._kind == "danger":
            fill = mix(DANGER, "#ffffff", 0.88) if self._hover else self._bg
            return fill, DANGER, DANGER
        # secondary：白底 + 描边（浅色主题靠描边区分层级）
        fill = BTN_BG_DOWN if self._pressed else (BTN_BG_HOVER if self._hover else BTN_BG)
        return fill, BORDER, TEXT

    def _redraw(self) -> None:
        self.delete("all")
        fill, outline, fg = self._palette()
        round_rect(self, px(0.5), px(0.5), self._cw - px(0.5), self._ch - px(0.5),
                   r=px(6), fill=fill, outline=outline, width=px(1))
        self.create_text(self._cw // 2, self._ch // 2, text=self._text, fill=fg,
                         font=self._font)

    def _on_enter(self, _event=None) -> None:
        self._hover = True
        self._redraw()

    def _on_leave(self, _event=None) -> None:
        # 离开时同时清掉 pressed，否则按下后拖出会卡在按下外观
        self._hover = False
        self._pressed = False
        self._redraw()

    def _on_press(self, _event=None) -> None:
        self._pressed = True
        self._redraw()

    def _on_release(self, _event=None) -> None:
        was_pressed = self._pressed
        self._pressed = False
        self._redraw()
        if was_pressed and self._enabled and self._command is not None:
            self._command()

    def set_enabled(self, enabled: bool) -> None:
        self._enabled = bool(enabled)
        self.configure(cursor="hand2" if enabled else "arrow")
        self._redraw()

    def set_text(self, text: str) -> None:
        self._text = text
        self._redraw()


class ColorField(tk.Frame):
    """预设色快捷键：颜色只能从预设中选择。

    不再显示单独的「当前色块 + 十六进制值」——预设色块自身即当前色反馈：
    选中的那一块描一圈 accent 边，未选中只有浅边。
    """

    def __init__(self, parent, command: Callable[[str], None], value: str = "#d32f2f",
                 presets=(), bg: str = PANEL) -> None:
        super().__init__(parent, bg=bg)
        self._command = command
        self._value = value
        self._bg = bg
        #: [(归一化色值, 色块 Canvas)]，供 _redraw 画选中态
        self._chips: List[Tuple[str, tk.Canvas]] = []

        from ..spec import normalize_color

        field_label(self, "颜色").pack(fill="x")

        chips = tk.Frame(self, bg=bg)
        chips.pack(fill="x", pady=(px(SP_XS), 0))
        for _name, color in presets:
            chip = tk.Canvas(chips, width=px(22), height=px(22), bg=bg,
                             highlightthickness=0, bd=0, cursor="hand2")
            chip.pack(side="left", padx=(0, px(SP_XS)))
            chip.create_rectangle(px(1), px(1), px(21), px(21), fill=color,
                                  outline=BORDER_SOFT)
            chip.bind("<Button-1>", lambda _e, c=color: self._set(c))
            chip.bind("<Enter>", lambda _e, c=chip: self._on_chip_enter(c))
            chip.bind("<Leave>", lambda _e, c=chip: self._on_chip_leave(c))
            self._chips.append((normalize_color(color), chip))
        self._redraw()

    def _set(self, color: str) -> None:
        from ..spec import normalize_color
        self._value = normalize_color(color)
        self._redraw()
        if self._command is not None:
            self._command(self._value)

    def _on_chip_enter(self, chip: tk.Canvas) -> None:
        """悬停描边；选中色块已有 accent 边，不再叠加，避免互相覆盖。"""
        if chip.find_withtag("sel"):
            return
        chip.delete("hl")
        chip.create_rectangle(px(0.5), px(0.5), px(21.5), px(21.5),
                              outline=BORDER, width=px(1), tags="hl")

    def _on_chip_leave(self, chip: tk.Canvas) -> None:
        chip.delete("hl")

    def _redraw(self) -> None:
        """在「当前选中色」对应的预设色块上画 accent 描边。

        取代旧实现里单独的「当前色块 + 十六进制文本」——预设色块自身即反馈。
        """
        from ..spec import normalize_color
        sel = normalize_color(self._value)
        for color, chip in self._chips:
            chip.delete("sel")
            if color == sel:
                chip.create_rectangle(px(0.5), px(0.5), px(21.5), px(21.5),
                                      outline=ACCENT, width=px(2), tags="sel")

    def get(self) -> str:
        return self._value

    def set(self, color: str, notify: bool = False) -> None:
        from ..spec import normalize_color
        self._value = normalize_color(color)
        self._redraw()
        if notify and self._command is not None:
            self._command(self._value)


class TextArea(tk.Frame):
    """多行文本框：原生 tk.Text + 自绘底线。"""

    def __init__(self, parent, value: str = "", height: int = 3,
                 command: Optional[Callable[[str], None]] = None, bg: str = PANEL) -> None:
        super().__init__(parent, bg=bg)
        self._command = command
        self._bg = bg
        self.text = tk.Text(self, height=height, wrap="word", bg=CONTROL, fg=TEXT,
                            relief="flat", bd=0, highlightthickness=px(1),
                            highlightbackground=FIELD_LINE, highlightcolor=ACCENT,
                            insertbackground=TEXT, font=(UI_FAMILY, sf(10)),
                            padx=px(SP_SM), pady=px(SP_SM))
        self.text.pack(fill="both", expand=True)
        self.text.insert("1.0", value)
        self.text.bind("<<Modified>>", self._on_modified)
        self.text.bind("<KeyRelease>", self._on_modified)

    def _on_modified(self, _event=None) -> None:
        try:
            self.text.edit_modified(False)
        except tk.TclError:
            pass
        if self._command is not None:
            self._command(self.get())

    def get(self) -> str:
        return self.text.get("1.0", "end-1c")

    def set(self, value: str, notify: bool = False) -> None:
        self.text.delete("1.0", "end")
        self.text.insert("1.0", value)
        if notify and self._command is not None:
            self._command(value)


__all__ = [
    "SCALE", "enable_dpi_awareness", "px", "sf", "mix", "round_rect",
    "install_ttk_styles", "section", "field_label", "SliderField", "ToggleSwitch",
    "FluentButton", "ColorField", "TextArea", "UI_FAMILY",
]
