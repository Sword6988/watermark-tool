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

import math
from typing import Callable, List, Optional, Tuple

import tkinter as tk
import tkinter.font as tkfont
from tkinter import ttk
from PIL import Image, ImageDraw, ImageTk

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

#: 数字框步进三角（**常驻显示**，三级状态）：常态淡到"存在但不抢戏"，
#: 悬停字段加深提示可点，命中上/下半区转主题色。禁用态比常态更淡且无反馈。
STEP_IDLE = TEXT_MUTE
STEP_HOVER = TEXT_DIM
STEP_ACTIVE = ACCENT
STEP_OFF = "#c2c5cf"

#: 「旋转角度」圆盘（Canvas 自绘，循环量用圆环表示）。
#: 参考图是**红圈**，但本工程红（``DANGER``）专指「非法输入」，常驻的红色圆环
#: 会被读成报错；且色板铁律要求「类型/状态两套色绝不共用」。故默认跟主题走。
#: 想换成参考图那种红：把 ``DIAL_RING`` / ``DIAL_ARROW`` / ``DIAL_KNOB_EDGE``
#: 一起改成 ``DIAL_REF_RED`` 即可（仅此三行）。
DIAL_REF_RED = "#ff5b5a"        # 参考图配色（备选，未启用）
DIAL_RING = ACCENT              # 轨道圆环
DIAL_RING_OFF = "#c9ccd4"       # 禁用态圆环
DIAL_RING_W = 3                 # 圆环粗细（逻辑 px）
DIAL_LINE = TEXT_DIM            # 方向线（即水印文字走向）
DIAL_LINE_OFF = "#b8bcc6"       # 禁用态方向线
DIAL_ARROW = ACCENT             # 方向箭头
DIAL_ARROW_OFF = "#c2c5cf"      # 禁用态箭头
DIAL_KNOB_EDGE = ACCENT         # 手柄描边
DIAL_KNOB_EDGE_OFF = "#c2c5cf"  # 禁用态手柄描边
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

#: 界面字体家族（微软雅黑优先；鸿蒙只在装了它的机器上才会被选到 —— 见 fonts.py）
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
    """Canvas 上画圆角矩形；r<=0 时退化为普通矩形（保留同一套调用）。

    **不用** ``smooth=True``：那会把所有顶点当控制点拟合贝塞尔样条，
    直边也会被画成微弯的曲线、整体发虚；这里改为「4 段直线 + 4 段真实
    圆弧逼近（每角 8 段，最大偏差 < 0.05px）」，边缘与调用方给的几何严格一致。
    """
    r = min(float(r), (x1 - x0) / 2.0, (y1 - y0) / 2.0)
    if r <= 0:
        return canvas.create_rectangle(x0, y0, x1, y1, **kw)
    # 屏幕系 y 向下：四角圆心 + 各自的起止角（-90°=正上，0°=正右，…）
    corners = (
        (x1 - r, y0 + r, -90.0, 0.0),     # 右上
        (x1 - r, y1 - r, 0.0, 90.0),      # 右下
        (x0 + r, y1 - r, 90.0, 180.0),    # 左下
        (x0 + r, y0 + r, 180.0, 270.0),   # 左上
    )
    points: List[float] = []
    for cx, cy, a0, a1 in corners:
        for i in range(9):
            a = math.radians(a0 + (a1 - a0) * i / 8.0)
            points.append(cx + r * math.cos(a))
            points.append(cy + r * math.sin(a))
    return canvas.create_polygon(points, **kw)


#: 抗锯齿超采样倍率。4x 下用 LANCZOS 缩回 1x，等效盒式滤波：
#: 边缘获得约 1px 的过渡带，圆 / 斜线 / 三角不再有 GDI 直绘的锯齿台阶。
AA_SS = 4

try:                                    # Pillow >= 9.1 的枚举写法，旧版退回常量
    _RESAMPLE = Image.Resampling.LANCZOS
except AttributeError:                  # pragma: no cover
    _RESAMPLE = Image.LANCZOS


def paint_aa(canvas: tk.Canvas,
             painter: Callable[[ImageDraw.ImageDraw, Callable[[float], int]], None],
             bg: str = PANEL) -> None:
    """把 ``painter`` 画的矢量图形以 **4x 超采样 + LANCZOS 缩小**贴到画布上。

    **为什么需要它**：Tk canvas 在 Windows 上走 GDI 直绘，椭圆 / 斜线 /
    多边形**完全没有抗锯齿**（实测一整幅圆盘只有 4 种纯色、0% 过渡色），
    圆环和方向线的边缘全是锯齿台阶 —— 这就是「发虚」观感的来源，而
    canvas 图元没有任何抗锯齿参数可调。

    **做法**：在 ``AA_SS`` 倍尺寸的位图上重画同几何图形（坐标与描边宽度
    一并放大），再 LANCZOS 缩回 1x 贴成单个 image 图元。几何 / 颜色 /
    尺寸与原画法完全一致，只有边缘质量不同。PIL 本身同样无抗锯齿，
    平滑感全部来自超采样 —— 这正是可复现、可预期的部分。

    约定：

    * ``painter(draw, s)`` 接收 Pillow 的 ``ImageDraw`` 与坐标缩放函数
      ``s``（内部完成 ``×AA_SS`` 并取整）；**所有坐标与描边宽度都必须过
      ``s``**，否则会出现 4 倍错位或 1/4 粗细。
    * 整幅位图先铺 ``bg`` 再画图 —— 画布底色由此接管，画布上如需文字等
      原生图元（如 FluentButton），应在 ``paint_aa`` **之后**创建。
    * 渲染结果同时存到 ``canvas._aa_image``（PIL Image），供测试与探针做
      像素级断言；PhotoImage 引用挂在 ``canvas._aa_photo`` 上防 GC。
    """
    w, h = int(canvas.winfo_width()), int(canvas.winfo_height())
    if w <= 1 or h <= 1:
        # 尚未布局（winfo 还是占位值）：退回画布的 -width/-height 选项。
        # 没设尺寸的画布（如 FlatScale，靠 <Configure> 驱动）在这里退出。
        try:
            w = int(round(float(canvas["width"])))
            h = int(round(float(canvas["height"])))
        except Exception:
            return
        if w <= 1 or h <= 1:
            return
    ss = AA_SS
    img = Image.new("RGB", (w * ss, h * ss), bg)
    draw = ImageDraw.Draw(img)
    painter(draw, lambda v: int(round(v * ss)))
    small = img.resize((w, h), _RESAMPLE)
    photo = ImageTk.PhotoImage(small, master=canvas)
    canvas._aa_image = small            # 测试 / 探针的像素断言入口
    canvas._aa_photo = photo            # 持引用，防止 PhotoImage 被 GC 后图消失
    canvas.delete("aa")
    canvas.create_image(0, 0, anchor="nw", image=photo, tags="aa")


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

        r = px(self.KNOB) / 2.0
        glow = not self._disabled and (self._hover or self._press)
        ring_w = px(self.RING)

        def paint(d: ImageDraw.ImageDraw, s: Callable[[float], int]) -> None:
            # 未填充轨道（整条，带描边以便在白底上可辨）
            d.rounded_rectangle([s(pad), s(cy - th / 2.0), s(right), s(cy + th / 2.0)],
                                radius=s(th / 2.0), fill=track_c,
                                outline=edge_c, width=s(px(1)))
            # 已填充轨道：从起点到滑块中心 —— 与未填充段形成明确区分
            if kx > pad + 0.5:
                # 半径按实际长度钳制：填充段极短时 r 若超过半宽，圆角会画歪
                d.rounded_rectangle([s(pad), s(cy - th / 2.0), s(kx), s(cy + th / 2.0)],
                                    radius=s(min(th / 2.0, (kx - pad) / 2.0)),
                                    fill=fill_c)
            # 悬停 / 按下时加一圈柔光，强化「可拖动」的可供性
            if glow:
                d.ellipse([s(kx - r - px(3)), s(cy - r - px(3)),
                           s(kx + r + px(3)), s(cy + r + px(3))],
                          outline=ACCENT_DIM, width=s(px(2)))
            # 投影：先画一个下移 1px 的暗色圆，本体覆盖后底部留一道月牙 —— 浮起感
            d.ellipse([s(kx - r), s(cy - r + px(1)), s(kx + r), s(cy + r + px(1))],
                      fill=shadow_c)
            # 本体：白底 + 主题色描边，与灰色轨道强对比
            d.ellipse([s(kx - r), s(cy - r), s(kx + r), s(cy + r)],
                      fill=knob_fill, outline=knob_line, width=s(ring_w))

        paint_aa(self, paint, bg=self._bg_color)

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


class _AngleDial(tk.Canvas):
    """「旋转角度」专用圆盘：圆环轨道 + 贯穿圆心的方向线 + 手柄 + 箭头。

    **为什么角度字段不用线性滑块**：角度是**循环量**，没有端点。线性轨道上
    0° 与 360° 分居两端、视觉上离得最远，实际却是同一个方向；圆盘天然首尾
    相接，拖过头就是绕回来 —— 形状本身就在表达"这是循环量"。

    **方向线不是装饰**：它贯穿圆心、两端各探出圈外一小段，方向就是水印文字的
    真实走向（0° 水平向右，**顺时针为正**，与读时钟同向）。注意 PIL 的
    ``rotate`` 是逆时针，故 ``wm.layout`` 渲染时传 ``rotate(-angle)`` 补偿 ——
    那边漏了负号，盘上的线就会与文字反着来。
    于是「角度」这个抽象数字变成了一眼就能看懂的方向预览。

    ==========  ==========================================================
    圆环        轨道，手柄沿它走
    方向线      贯穿圆心，即当前角度对应的文字走向
    手柄        白底 + 彩色描边的小圆，落在圆环与方向线的交点上
    箭头        方向线正方向末端、**手柄外侧**的小三角；它同时打破「直径
                对称」的歧义 —— θ 与 θ+180° 画出来是同一条线，靠箭头区分
    ==========  ==========================================================

    交互：拖动手柄转角度，**完全自由 1°** —— 圆盘自身**不做任何吸附**：拖出来的
    值交给 ``SliderField`` 的量化器（``_snap``，步长 1°）取整就够了。原先额外吸附
    到 5° 是为「鼠标停不住整数」打的补丁（半径 40px 时 1px ≈ 1.43°），可代价是想要
    的角度根本拿不到（想设 37° 只能得到 35°），用户明确要求去掉 —— 精度由数字框 +
    步进三角兜底，那里始终是 1°。

    吸附**一律不挂**，包括修饰键：早先做过 Shift 15° / Alt 自由 1° 两档，用户明确
    要求移除 —— 隐藏按键没人发现，同样的精度用数字框能直接拿到。故这里一档都没有。

    靠近圆心（``DEAD_RATIO``）**按下与拖动全程都不取值**：那里的方向对位移的
    敏感度趋于无穷（0.2 半径处 1px ≈ 7.2°），强行取值就是数字疯跳。

    公共 API 与 ``FlatScale`` 保持同一最小集（``get`` / ``set`` / ``state``），
    因此 ``SliderField`` 可以无差别替换 —— 只换绘制与交互，不动上层调用。
    """

    D = 64              # 圆盘直径（逻辑 px）
    KNOB = 8            # 手柄半径
    ARROW_LEN = 8       # 箭头长度
    ARROW_HALF = 3.5    # 箭头半宽
    ARROW_GAP = 2       # 箭头与手柄之间的间隙
    LINE_OVER = 6       # 方向线负方向探出圈外的长度（让它读起来像"轴"而非"射线"）
    DEAD_RATIO = 0.45   # 圆心死区（占半径比例）：全程（按下 + 拖动）都不取值

    def __init__(self, parent, from_: float = 0.0, to: float = 360.0,
                 command: Optional[Callable[[float], None]] = None,
                 bg: str = PANEL,
                 quantize: Optional[Callable[[float], float]] = None,
                 **_ignored) -> None:
        # 圈外余量必须容得下「手柄半径 + 间隙 + 箭头长」：箭头画在手柄**外侧**，
        # 余量不足就会被后画的手柄盖住（原型第一版正是如此，箭头等于没有）。
        self._room = px(self.KNOB) + px(self.ARROW_GAP) + px(self.ARROW_LEN) + px(2)
        side = px(self.D) + self._room * 2
        super().__init__(parent, width=side, height=side, bg=bg,
                         highlightthickness=0, bd=0, cursor="hand2", takefocus=0)
        self._lo, self._hi = float(from_), float(to)
        self._cmd = command
        # 由 SliderField 传入的量化器（按步长取整 + 循环归一）。**拖动时也走它**，
        # 否则圆盘内部留着未量化的小数、旁边数字框显示取整后的值，两者不一致
        # （实测偏差可达 0.5°，外部 set() 时手柄还会悄悄回跳一下）。
        self._quantize = quantize
        self._bg_color = bg
        self._value = self._lo
        self._hover = False
        self._press = False
        self._disabled = False
        # 不能叫 _w / _h —— 那是 tk.Misc 的保留属性（Tcl 路径名）
        self._side = float(side)

        self.bind("<Configure>", self._on_configure)
        self.bind("<Button-1>", self._on_press)
        self.bind("<B1-Motion>", self._on_drag)
        self.bind("<ButtonRelease-1>", self._on_release)
        self.bind("<Enter>", lambda _e: self._set_hover(True))
        self.bind("<Leave>", lambda _e: self._set_hover(False))

    # -- 几何 -------------------------------------------------------------

    def _center(self) -> float:
        return self._side / 2.0

    def _radius(self) -> float:
        return max(px(6), self._center() - self._room)

    def _unit(self):
        """当前角度对应的单位方向向量。

        屏幕 y 轴向下，``dy`` 取**正** —— 于是角度增大在屏幕上表现为**顺时针**
        （3 点钟 0° → 6 点钟 90°），符合读时钟的直觉。

        ⚠️ 这与 ``PIL.Image.rotate`` 的正方向（逆时针）**相反**，所以渲染侧
        ``wm.layout`` 必须传 ``rotate(-angle)`` 补偿；否则盘上的方向线会与
        水印文字的真实走向镜像 —— 那等于骗用户。改这里请连那边一起改。
        """
        rad = math.radians(self._value)
        return math.cos(rad), math.sin(rad)

    def _dist(self, x: float, y: float) -> float:
        c = self._center()
        return math.hypot(x - c, y - c)

    def _value_of(self, x: float, y: float) -> float:
        """屏幕点 -> 角度值。

        **不能**把屏幕角线性映射到 ``[lo, hi]``（``lo + ang/360*span``）：那只在
        ``lo == 0`` 时成立。圆盘的语义是「**值 v 显示在角度 v 的位置上**」——
        值域改成 -180..180 后，线性映射会把 0° 画到 9 点方向去（因为 0 是区间的
        中点）。正确做法：以 ``lo`` 为原点取模，把屏幕角搬进值域，再夹到上界。
        """
        c = self._center()
        # 屏幕 y 向下 ⇒ atan2(y - c, ...) 的正方向即屏幕上的**顺时针**，
        # 与 _unit() 保持一致（反算必须与正算互逆，否则拖到哪儿值就不在哪儿）。
        ang = math.degrees(math.atan2(y - c, x - c)) % 360.0
        value = self._lo + math.fmod(ang - self._lo, 360.0)
        # ±180 是同一条直径：统一取 +180（与 spec.wrap_deg 的半开半闭约定一致）
        if abs(value - self._lo) < 1e-9 and self._lo < 0.0:
            value = self._hi
        return min(max(value, self._lo), self._hi)

    # -- 交互 -------------------------------------------------------------

    def _on_configure(self, _event=None) -> None:
        # 取短边：万一被横向拉伸，也保持正圆而不是椭圆
        self._side = float(max(px(1), min(self.winfo_width(), self.winfo_height())))
        self._redraw()

    def _on_press(self, event) -> None:
        if self._disabled:
            return
        if self._in_dead_zone(event.x, event.y):
            return          # 圆心附近不起拖（取值不稳定），_apply 里还会再挡一次
        self._press = True
        self._apply(event)

    def _in_dead_zone(self, x: float, y: float) -> bool:
        return self._dist(x, y) < self._radius() * self.DEAD_RATIO

    def _on_drag(self, event) -> None:
        if self._disabled or not self._press:
            return
        self._apply(event)

    def _on_release(self, _event=None) -> None:
        self._press = False
        self._redraw()

    def _set_hover(self, on: bool) -> None:
        self._hover = bool(on)
        self._redraw()

    def _apply(self, event) -> None:
        # 死区对**拖动**同样生效：只挡「按下」的话，一旦起拖后把鼠标滑过圆心，
        # 值就会以 7°/px 的速度疯跳 —— 与死区存在的初衷自相矛盾。
        if self._in_dead_zone(event.x, event.y):
            return
        value = self._value_of(event.x, event.y)
        # 圆盘自身**不吸附**：交给 SliderField 的量化器（步长 1°）取整即可，
        # 于是「想要几度就是几度」。原先的 5° 吸附会让人拿不到 37° 这类值，已移除；
        # 修饰键（Shift/Alt）分档同样不挂，隐藏按键没人发现。
        if self._quantize is not None:
            value = self._quantize(value)
        self._value = min(max(value, self._lo), self._hi)
        self._redraw()
        if self._cmd is not None:
            self._cmd(self._value)

    # -- 绘制 -------------------------------------------------------------

    def _redraw(self) -> None:
        self.delete("all")
        c = self._center()
        r = self._radius()
        ux, uy = self._unit()

        if self._disabled:
            ring_c, line_c, arrow_c = DIAL_RING_OFF, DIAL_LINE_OFF, DIAL_ARROW_OFF
            knob_edge, knob_fill = DIAL_KNOB_EDGE_OFF, KNOB_OFF
        else:
            ring_c, line_c, arrow_c = DIAL_RING, DIAL_LINE, DIAL_ARROW
            knob_edge, knob_fill = DIAL_KNOB_EDGE, KNOB

        base = r + px(self.KNOB) + px(self.ARROW_GAP)
        tip = base + px(self.ARROW_LEN)
        over = px(self.LINE_OVER)
        half = px(self.ARROW_HALF)
        kr = px(self.KNOB)
        kx, ky = c + ux * r, c + uy * r
        glow = not self._disabled and (self._hover or self._press)
        shadow = KNOB_OFF if self._disabled else KNOB_SHADOW
        ring_w = px(DIAL_RING_W)

        def paint(d: ImageDraw.ImageDraw, s: Callable[[float], int]) -> None:
            # 轨道圆环（循环量：一整圈，无端点）
            d.ellipse([s(c - r), s(c - r), s(c + r), s(c + r)],
                      outline=ring_c, width=s(ring_w))

            # 方向线：贯穿圆心。正方向一直画到**箭头尖端**（中间一段被手柄盖住），
            # 负方向只探出圈外一小段 —— 与参考图一致：线"穿过"手柄，端头才是箭头。
            d.line([s(c - ux * (r + over)), s(c - uy * (r + over)),
                    s(c + ux * tip), s(c + uy * tip)],
                   fill=line_c, width=s(px(2)))

            # 箭头：整个落在手柄**外侧**
            bx, by = c + ux * base, c + uy * base
            nx, ny = -uy, ux              # 法向（右手系转 90°）
            d.polygon([s(c + ux * tip), s(c + uy * tip),
                       s(bx + nx * half), s(by + ny * half),
                       s(bx - nx * half), s(by - ny * half)],
                      fill=arrow_c)

            # 手柄：白底 + 彩描边，压在圆环与方向线的交点上
            if glow:
                # 悬停 / 按下：一圈柔光，强化「这里可以拖」
                d.ellipse([s(kx - kr - px(3)), s(ky - kr - px(3)),
                           s(kx + kr + px(3)), s(ky + kr + px(3))],
                          outline=ACCENT_DIM, width=s(px(2)))
            d.ellipse([s(kx - kr), s(ky - kr + px(1)),
                       s(kx + kr), s(ky + kr + px(1))], fill=shadow)
            d.ellipse([s(kx - kr), s(ky - kr), s(kx + kr), s(ky + kr)],
                      fill=knob_fill, outline=knob_edge, width=s(px(2)))

        paint_aa(self, paint, bg=self._bg_color)

    # -- 公共 API（与 FlatScale 同构，便于无差别替换） ---------------------

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


class _Stepper(tk.Canvas):
    """数字框右缘的步进三角（上下两个），**常驻显示**。

    为什么不是「悬停才显示」：那样不悬停时数字框右缘会显得空落落，而且用户
    看不出这个数字是可以点着改的（可发现性差）。改成常驻后，用**三级状态**
    代替显隐开关，保证「一直在」又「不抢戏」：

    ==================  ==========================================
    常态                淡灰 ``STEP_IDLE``，安静地提示这里可点
    悬停在字段上        ``STEP_HOVER`` 加深，确认"能交互"
    命中上 / 下半区     ``STEP_ACTIVE`` 主题色 + 浅底，即时反馈
    禁用                ``STEP_OFF`` 更淡，且不响应悬停与点击
    ==================  ==========================================

    其它要点：

    * **始终占位**：固定宽度、状态切换只改颜色 —— 绝不引起布局跳动。
    * 上下两半各自独立热区，命中区比图形略大，便于点中。
    * ``takefocus=0``：点击不夺走输入框焦点，用户可继续手动输入。

    ``boxed=False`` 用于「嵌入数字框」形态（方案 C）：此时外层容器已经画了边框，
    命中时只铺一层浅色底 **不描边**，避免双线。
    """

    W = 13          # 逻辑宽

    def __init__(self, parent, command, bg: str = PANEL, boxed: bool = True) -> None:
        self._cw = px(self.W)
        self._ch = px(20)
        super().__init__(parent, width=self._cw, height=self._ch, bg=bg,
                         highlightthickness=0, bd=0, takefocus=0)
        self._command = command        # Callable[[int], None]：+1 上，-1 下
        self._bg = bg
        self._boxed = bool(boxed)
        self._hover = False            # 鼠标是否在「输入框 / 步进器 / 外框」上
        self._zone = 0                 # 0 无 / +1 上半 / -1 下半
        self._enabled = True
        self.bind("<Button-1>", self._on_click)
        self.bind("<Motion>", self._on_motion)
        self.bind("<Leave>", self._on_leave)
        self._redraw()

    # -- 外部控制 ---------------------------------------------------------

    def set_hover(self, hover: bool) -> None:
        """切换「悬停」态：只改三角深浅，三角本身始终在。"""
        hover = bool(hover)
        if hover == self._hover:
            return
        self._hover = hover
        if not hover:
            self._zone = 0
            self.configure(cursor="")
        self._redraw()

    def set_enabled(self, enabled: bool) -> None:
        enabled = bool(enabled)
        if enabled == self._enabled:
            return
        self._enabled = enabled
        if not enabled:
            self._zone = 0
            self.configure(cursor="")
        self._redraw()

    def set_height(self, height: int) -> None:
        """跟随输入框实际高度，保证两者落在同一水平线上。"""
        height = max(px(14), int(height))
        if height == self._ch:
            return
        self._ch = height
        self.configure(height=height)
        self._redraw()

    # -- 内部 -------------------------------------------------------------

    def _half(self, y: int) -> int:
        """按纵向位置判定命中的是上半（+1）还是下半（-1）。"""
        return 1 if y < self._ch / 2 else -1

    def _on_motion(self, event) -> None:
        zone = self._half(event.y) if self._enabled else 0
        if zone != self._zone:
            self._zone = zone
            self.configure(cursor="hand2" if self._enabled else "")
            self._redraw()

    def _on_leave(self, _event=None) -> None:
        # 只清「命中半区」；是否悬停由 SliderField 的悬停组统一判定
        if self._zone:
            self._zone = 0
            self._redraw()

    def _on_click(self, event) -> None:
        if self._enabled and self._command is not None:
            self._command(self._half(event.y))

    def _fills(self) -> Tuple[str, str]:
        """返回 (上三角色, 下三角色)：禁用 > 命中 > 悬停 > 常态。"""
        if not self._enabled:
            return STEP_OFF, STEP_OFF
        base = STEP_HOVER if self._hover else STEP_IDLE
        up = STEP_ACTIVE if self._zone == 1 else base
        dn = STEP_ACTIVE if self._zone == -1 else base
        return up, dn

    def _redraw(self) -> None:
        self.delete("all")
        w, h = self._cw, self._ch
        # 命中半区才铺浅底（整块铺会让常态也变成一块"按钮"，太吵）
        hit_bg = self._enabled and self._zone
        pad = px(3)
        mid = h / 2
        gap = px(1)
        up_fill, dn_fill = self._fills()

        def paint(d: ImageDraw.ImageDraw, s: Callable[[float], int]) -> None:
            if hit_bg:
                d.rounded_rectangle([0, 0, s(w), s(h)], radius=s(px(3)),
                                    fill=ROW_HOVER,
                                    outline=FIELD_LINE if self._boxed else None,
                                    width=s(px(1)))
            d.polygon([s(w / 2), s(pad), s(w - pad), s(mid - gap), s(pad), s(mid - gap)],
                      fill=up_fill)
            d.polygon([s(pad), s(mid + gap), s(w - pad), s(mid + gap), s(w / 2), s(h - pad)],
                      fill=dn_fill)

        paint_aa(self, paint, bg=self._bg)


def _with_unit(label: str, unit: str) -> str:
    """把单位并入字段名：``("旋转角度", "°") -> "旋转角度 (°)"``。

    百分号这类本身就是"每百"的单位写进括号里更自然（``字号 (%)``）；
    角度符号同理。没有单位时原样返回。
    """
    if not unit:
        return label
    return f"{label} ({unit})"


class SliderField(tk.Frame):
    """「标签 + 数字框 + 滑块（+ 提示）」的组合字段。"""

    def __init__(self, parent, label: str, lo: float, hi: float, value: float,
                 step: float, command: Callable[[float], None], unit: str = "",
                 decimals: int = 0, show_entry: bool = True, hint: Optional[str] = None,
                 dial: bool = False, cyclic: bool = False, bg: str = PANEL) -> None:
        # ``dial=True``：把线性滑块换成**圆盘**（见 ``_AngleDial``）。只给
        # 「旋转角度」用 —— 角度是循环量，圆环天然首尾相接；其余三个量是单向
        # 大小，线性轨道才对。字段的其余部分（标签 / 数字框 / 步进三角 / 校验）
        # 完全共用，所以这里只是一个绘制策略开关。
        super().__init__(parent, bg=bg)
        self._lo, self._hi, self._step = float(lo), float(hi), float(step)
        # 循环量（角度）：合法化是**取模**而不是钳制 —— 否则步进器走到上界就被
        # 卡死（0° 永远回不到 -1°），手输 350° 还会被夹成 180°，方向直接变了。
        # 圆盘只用于循环量，故 dial=True 即隐含 cyclic=True。
        self._cyclic = bool(cyclic) or bool(dial)
        self._decimals = int(decimals)
        self._command = command
        self._bg = bg
        self._enabled = True
        self._value = min(max(float(value), self._lo), self._hi)
        self._hint_text = hint or ""
        self._invalid_job: Optional[str] = None
        self._hover_job: Optional[str] = None
        self._invalid = False
        self._focused = False

        head = tk.Frame(self, bg=bg)
        head.pack(fill="x")
        # 单位并入字段名（如「旋转角度 (°)」）：不再单挂一个尾部单位标签，
        # 避免「数字框 / 步进器 / 单位」三段并排时的错位与视觉噪音。
        self.label_widget = tk.Label(head, text=_with_unit(label, unit), bg=bg, fg=TEXT_DIM,
                                     font=(UI_FAMILY, sf(9)), anchor="w")
        self.label_widget.pack(side="left")

        self.entry_box: Optional[tk.Frame] = None
        self.entry: Optional[tk.Entry] = None
        self.stepper: Optional[_Stepper] = None
        self.var: Optional[tk.StringVar] = None
        if show_entry:
            self.var = tk.StringVar(value=self._format(self._value))
            # 外层「框」：1px 边框由容器承担，同时充当焦点环（聚焦变 accent）；
            # 内层 entry 与步进器都去边框，拼成一个整体，视觉上就是原生 UpDown。
            self.entry_box = tk.Frame(head, bg=bg,
                                      highlightthickness=px(1),
                                      highlightbackground=FIELD_LINE,
                                      highlightcolor=ACCENT)
            self.entry = tk.Entry(self.entry_box, textvariable=self.var, width=5,
                                  justify="right", bg=bg, fg=TEXT,
                                  relief="flat", bd=0, highlightthickness=0,
                                  insertwidth=px(1), font=(UI_FAMILY, sf(11)))
            # 常驻步进器：嵌在框内右缘（boxed=False → 命中时不重复描边）
            self.stepper = _Stepper(self.entry_box, command=self._spin,
                                    bg=bg, boxed=False)
            self.stepper.pack(side="right", padx=(0, px(2)), pady=px(2))
            self.entry.pack(side="right", padx=(px(SP_SM), px(SP_XS)), pady=px(2))
            self.entry_box.pack(side="right")
            self.entry.bind("<Return>", self._commit_entry)
            self.entry.bind("<FocusIn>", self._on_focus_in)
            self.entry.bind("<FocusOut>", self._on_focus_out)
            self.entry.bind("<Up>", lambda _e: self._nudge(1))
            self.entry.bind("<Down>", lambda _e: self._nudge(-1))
            self.entry.bind("<MouseWheel>", self._on_wheel)
            # 步进器高度跟随输入框，保证同一水平线
            self.entry.bind("<Configure>", lambda e: self.stepper.set_height(e.height), add="+")
            # 悬停组合：框 / 输入框 / 步进器 任一进入即显示，全部离开后延时隐藏
            for widget in (self.entry_box, self.entry, self.stepper):
                widget.bind("<Enter>", self._on_field_enter, add="+")
                widget.bind("<Leave>", self._on_field_leave, add="+")

        if dial:
            # 圆盘是固定方形，**不能** fill="x"：横向拉满会把圆拉成椭圆
            self.scale = _AngleDial(self, from_=self._lo, to=self._hi,
                                    command=self._on_scale, bg=bg,
                                    quantize=self._snap)
            self.scale.set(self._value)
            self.scale.pack(pady=(px(SP_SM), 0))
        else:
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
        self._invalid = True
        if self.entry_box is not None:
            self.entry_box.configure(highlightbackground=DANGER, highlightcolor=DANGER)
        self.hint_label.configure(
            text=f"请输入 {self._format(self._lo)} – {self._format(self._hi)} 之间的数值",
            fg=DANGER)
        self._sync_hint_visibility(force_show=True)
        self._invalid_job = self.after(1200, self._clear_invalid)

    def _clear_invalid(self) -> None:
        self._invalid_job = None
        self._invalid = False
        self._sync_box_border()
        self.hint_label.configure(text=self._hint_text, fg=TEXT_FAINT)
        self._sync_hint_visibility()

    def _sync_box_border(self) -> None:
        """外层框边框：非法 > 聚焦 > 常态（三者优先级递减）。"""
        if self.entry_box is None:
            return
        if self._invalid:
            color = DANGER
        elif self._focused:
            color = ACCENT
        else:
            color = FIELD_LINE
        self.entry_box.configure(highlightbackground=color, highlightcolor=color)

    def _on_focus_in(self, _event=None) -> None:
        self._focused = True
        self._sync_box_border()

    def _on_focus_out(self, _event=None) -> None:
        self._focused = False
        self._sync_box_border()
        self._commit_entry()

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
        v = round(value / step) * step
        if self._cyclic:
            span = self._hi - self._lo
            if span > 0:
                v = self._lo + math.fmod(v - self._lo, span)
                if v < self._lo:      # fmod 对负数返回负值，得再绕一圈（如 -190 -> 170）
                    v += span
                # 半开半闭 (lo, hi]：绕回下界时取上界，与 spec.wrap_deg 一致
                # （角度 -180 与 180 是同一方向，统一显示 180）
                if abs(v - self._lo) < 1e-9:
                    v = self._hi
        return min(max(v, self._lo), self._hi)

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
        self.set(self._entry_value() + direction * self._step)
        return "break"

    def _on_wheel(self, event):
        # 仅当输入框持有焦点时才接管滚轮（滚一格改一个步长），否则**放行**事件
        # 让滚动容器正常滚动 —— 旧版无条件 "break" 会吞掉事件，导致鼠标悬停
        # 在数字框上时整个面板滚不动。
        if self.entry is not None and self.focus_get() is self.entry:
            self.set(self._value + (1 if event.delta > 0 else -1) * self._step)
            return "break"
        return None

    def _entry_value(self) -> float:
        """把输入框当前文本解析为**合法值**；失败则回退上次有效值并给校验提示。

        步进器点击与方向键都走这里 —— 这样即使输入框**尚未失焦**（手输了一半就
        去点三角），也会先以「手输的内容」为基准增减，而不是拿旧的内部值算。
        """
        if self.entry is None or self.var is None:
            return self._value
        try:
            return self._snap(float(self.var.get().strip()))
        except (TypeError, ValueError, OverflowError):
            # 解析失败回退上次有效值，并给出明确的校验提示（不再静默）
            self._flag_invalid()
            return self._value

    def _commit_entry(self, _event=None) -> None:
        if self.entry is None or self.var is None:
            return
        value = self._entry_value()
        self.var.set(self._format(value))
        self.scale.set(value)
        self._value = value
        self._emit()

    # -- 悬停步进器 ---------------------------------------------------------

    def _on_field_enter(self, _event=None) -> None:
        """进入「外框 / 输入框 / 步进器」任一处：取消延时并让三角加深。"""
        if self._hover_job is not None:
            try:
                self.after_cancel(self._hover_job)
            except Exception:
                pass
            self._hover_job = None
        if self.stepper is not None:
            self.stepper.set_hover(True)

    def _on_field_leave(self, _event=None) -> None:
        """离开任一处：**延时**取消加深，给三者之间的移动留缓冲，避免闪烁。

        注意：这里**不隐藏**三角 —— 三角是常驻的，只是从"加深"回到"淡灰"。
        """
        if self._hover_job is None:
            self._hover_job = self.after(120, self._unhover_stepper)

    def _unhover_stepper(self) -> None:
        self._hover_job = None
        if self.stepper is not None:
            self.stepper.set_hover(False)

    def _spin(self, direction: int) -> None:
        """步进器点击：先归一化手输内容，再按步长增减（只发一次通知）。"""
        self.set(self._entry_value() + direction * self._step)

    def _emit(self) -> None:
        if self._enabled and self._command is not None:
            self._command(self._value)

    def destroy(self) -> None:
        """销毁前先撤掉挂在**自己**身上的延时任务。

        否则定时器仍留在 Tk 队列里，到点会去调一个已被 ``Misc.destroy`` 删掉的
        Tcl 命令 —— 进不了 Python，只在 Tcl 后台留一条
        ``invalid command name "..."`` 噪声（实测偶发 1/7）。现在无害，但只要
        将来有人把 job 改挂到 root，就会变成"操作已销毁控件"的真异常。

        注意 ``after_cancel`` 必须用 ``self``（谁调度谁取消）—— 这正是本文件
        注释里记过的历史坑。
        """
        for attr in ("_hover_job", "_invalid_job"):
            job = getattr(self, attr, None)
            if job is not None:
                try:
                    self.after_cancel(job)
                except Exception:
                    pass
                setattr(self, attr, None)
        super().destroy()

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
        """禁用态同时变淡：数值 + 标签，滑块与步进器一并禁用。"""
        self._enabled = bool(enabled)
        try:
            self.scale.state(["!disabled"] if enabled else ["disabled"])
        except tk.TclError:
            pass
        if self.entry is not None:
            self.entry.configure(state="normal" if enabled else "readonly",
                                 fg=TEXT if enabled else TEXT_FAINT)
        if self.stepper is not None:
            self.stepper.set_enabled(enabled)
        self.label_widget.configure(fg=TEXT_DIM if enabled else TEXT_MUTE)
        if self.entry_box is not None and not enabled:
            # 禁用时收起可能的焦点环，避免"灰字段 + accent 边框"打架
            self._focused = False
            self._sync_box_border()


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
        # 位图渲染依赖画布实际尺寸：映射前 winfo 是占位值，Configure 后重画一次
        self.canvas.bind("<Configure>", lambda _e: self._redraw())
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
        cw, ch, pad = self._cw, self._ch, px(2)
        knob_d = ch - pad * 2
        knob_x = (cw - pad - knob_d) if self._value else pad

        def paint(d: ImageDraw.ImageDraw, s: Callable[[float], int]) -> None:
            # 胶囊轨道：r = h/2 恰好是半圆端帽
            d.rounded_rectangle([0, 0, s(cw), s(h)], radius=s(h / 2.0),
                                fill=track, outline=track)
            d.ellipse([s(knob_x), s(pad), s(knob_x + knob_d), s(pad + knob_d)],
                      fill=KNOB)

        paint_aa(self.canvas, paint, bg=self._bg)

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
        # 位图渲染依赖画布实际尺寸：映射前 winfo 是占位值，Configure 后重画一次
        self.bind("<Configure>", lambda _e: self._redraw())
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
        cw, ch = self._cw, self._ch

        def paint(d: ImageDraw.ImageDraw, s: Callable[[float], int]) -> None:
            d.rounded_rectangle([s(px(0.5)), s(px(0.5)),
                                 s(cw - px(0.5)), s(ch - px(0.5))],
                                radius=s(px(6)), fill=fill,
                                outline=outline, width=s(px(1)))

        paint_aa(self, paint, bg=self._bg)
        # 文字保持原生图元：Tk 的字体渲染本身就是次像素级，比位图缩放更锐
        self.create_text(cw // 2, ch // 2, text=self._text, fill=fg,
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
    "AA_SS", "paint_aa",
    "install_ttk_styles", "section", "field_label", "SliderField", "ToggleSwitch",
    "FluentButton", "ColorField", "TextArea", "UI_FAMILY",
]
