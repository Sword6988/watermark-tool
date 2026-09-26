"""参数面板（v3）：所有可调水印参数。

**水印恒为平铺铺满整页**：没有「满铺整页（平铺）」开关，也没有「位置调节」区块
（X / Y 偏移、重置居中、预览拖拽）—— 位置与排布由 ``layout`` 固定，界面不提供
任何调节入口。因此：

    * 「四周边距」只有**一种**语义（每个水印四周留白），文案无需随模式切换；
    * 平铺是唯一且固定的行为，界面不再显示「铺满整页 · 预计 N 处水印」之类的
      交代文案（页面尺寸状态、块数估算及其刷新链路已一并移除）。
"""

from __future__ import annotations

from typing import Callable, List, Optional

import tkinter as tk
from tkinter import ttk

from .. import fonts
from ..spec import (
    ANGLE_RANGE, ANGLE_STEP, COLOR_PRESETS, FONT_PCT_RANGE, FONT_PCT_STEP,
    MARGIN_PCT_RANGE, MARGIN_PCT_STEP, OPACITY_PCT_RANGE, OPACITY_PCT_STEP,
    WatermarkSpec, opacity_from_pct, opacity_to_pct,
)
from . import theme as T
from .theme import (ColorField, SliderField, TextArea, field_label, section,
                    style_combobox_popdown)

#: 「四周边距」的说明文案（平铺是唯一模式，语义固定）
_MARGIN_HINT = "每个水印四周留白"
#: 「旋转角度」的说明文案：交代圆盘上那条线的含义与正方向。
#: ⚠️ 文案里**不能**出现「拖动 / 偏移 / 位置调节」字样 —— 平铺是唯一模式，界面
#: 不得有任何位置调节入口（``test_qa_ui_defaults_and_no_position_entry`` 会扫描
#: 面板文案）。所以这里只说"盘上方向线即文字走向"，不提怎么操作。
_ANGLE_HINT = "盘上方向线即文字走向，顺时针为正"


class ScrolledFrame(tk.Frame):
    """竖向滚动容器：内容变高时自动扩展。

    滚轮方案（修复「滚动卡顿 / 时灵时不灵」）：
        * ``bind_all("<MouseWheel>")`` **常驻**，处理器按指针位置分发。
          旧方案用 inner 的 <Enter>/<Leave> 动态挂/解 bind_all，但 Tk 中指针
          移入任何子控件（标签/滑块/输入框都是独立窗口）都会先触发父容器的
          <Leave>（NotifyInferior），把滚轮解绑 —— 悬停在控件上时滚轮失效，
          表现为时灵时不灵的卡顿。
        * 滚动量按**像素**映射（120 = 一格 ≈ 56 逻辑像素，yscrollincrement=1），
          摆脱 Tk 默认「窗口高度 1/10」的大步长跳跃。
        * 短时间内的连续滚轮事件**合并成一帧**再执行（16ms after 节流），
          避免事件洪峰期间每条事件都触发一次完整重绘。
    """

    #: 一格滚轮（WHEEL_DELTA=120）对应的逻辑像素，约等于系统默认 3 行
    PX_PER_NOTCH = 56
    #: 事件合并窗口（毫秒）：单帧之内到达的滚轮事件只触发一次滚动重绘
    COALESCE_MS = 16

    def __init__(self, parent, bg: str = T.PANEL) -> None:
        super().__init__(parent, bg=bg)
        self.canvas = tk.Canvas(self, bg=bg, highlightthickness=0, bd=0,
                                yscrollincrement=1)
        self.vbar = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview,
                                  style="App.Vertical.TScrollbar")
        self.inner = tk.Frame(self.canvas, bg=bg)
        self._win = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.canvas.configure(yscrollcommand=self.vbar.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        self.vbar.pack(side="right", fill="y")
        self.inner.bind("<Configure>", self._on_inner)
        self.canvas.bind("<Configure>", self._on_canvas)
        self._pending_delta = 0.0
        self._wheel_job: Optional[str] = None
        self.canvas.bind_all("<MouseWheel>", self._on_wheel_all)

    def _on_inner(self, _event=None) -> None:
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))
        self._update_vbar()

    def _on_canvas(self, event) -> None:
        # 右侧留 10px 间隔：内容不再贴着滚动条，视觉上更协调
        self.canvas.itemconfigure(self._win, width=max(1, event.width - T.px(10)))

    def _update_vbar(self) -> None:
        """内容不超过可视高度时隐藏滚动条（少一块常驻占位，面板更干净）。"""
        try:
            needed = self.inner.winfo_reqheight() > self.canvas.winfo_height()
        except Exception:
            needed = True
        if needed and not self.vbar.winfo_ismapped():
            self.vbar.pack(side="right", fill="y")
        elif not needed and self.vbar.winfo_ismapped():
            self.vbar.pack_forget()

    # -- 滚轮 --------------------------------------------------------------

    def _inside(self, widget) -> bool:
        """指针所指控件是否在本滚动容器内（含内层 Frame 与画布本身）。

        ⚠️ **必须排除下拉框的弹出列表**：``<cb>.popdown`` 是一个**独立 Toplevel**，
        但它的**窗口路径**挂在 ``inner`` 之下（``<inner>.<cb>.popdown.f.l``）—— 光看
        路径前缀会被误判成"面板内的控件"，于是展开字体列表滚动时，滚轮既滚列表
        又带动整个面板滚（面板跟着跳）。这里按路径里的 ``.popdown`` 显式排除。
        """
        if widget is None:
            return False
        if widget is self.inner or widget is self.canvas or widget is self:
            return True
        path = str(widget)
        if not path.startswith(str(self.inner) + "."):
            return False
        return ".popdown" not in path

    def _on_wheel_all(self, event):
        """常驻的全局滚轮分发：只接管指针落在本容器内的事件。"""
        if not self._inside(getattr(event, "widget", None)):
            return None
        self.accumulate(event.delta)
        return "break"

    def accumulate(self, delta: float) -> None:
        """累积一格滚轮增量，安排在合并窗口结束时一次执行。

        公共入口：也供子控件转发使用（如下拉框不静默改值、改为滚动面板）。
        """
        self._pending_delta += -float(delta)
        if self._wheel_job is None:
            self._wheel_job = self.after(self.COALESCE_MS, self._flush_wheel)

    def _flush_wheel(self) -> None:
        self._wheel_job = None
        pending = self._pending_delta
        self._pending_delta = 0.0
        if pending == 0:
            return
        step = float(T.px(self.PX_PER_NOTCH))
        pixels = int(pending / 120.0 * step)
        # 保留不足一格的余量，慢速滚动也不丢步
        self._pending_delta += pending - pixels * 120.0 / step
        if pixels:
            try:
                self.canvas.yview_scroll(pixels, "units")  # yscrollincrement=1 → 像素
            except tk.TclError:
                pass


class ParamPanel(tk.Frame):
    """左侧参数面板。"""

    def __init__(self, parent, on_change: Callable[[WatermarkSpec], None]) -> None:
        super().__init__(parent, bg=T.PANEL)
        self._on_change = on_change
        self._spec = WatermarkSpec.default()
        # ``_families`` 是英文原名（渲染真值），``_labels`` 是逐项对齐的中文显示名
        self._families: List[str] = []
        self._labels: List[str] = []

        header = tk.Frame(self, bg=T.PANEL)
        header.pack(fill="x", padx=T.px(T.PANEL_PAD), pady=(T.px(T.SP_LG), T.px(T.SP_SM)))
        tk.Label(header, text="水印参数", bg=T.PANEL, fg=T.TEXT,
                 font=(T.UI_FAMILY, T.sf(11), "bold"), anchor="w").pack(side="left")

        body = ScrolledFrame(self, bg=T.PANEL)
        self._body = body
        body.pack(fill="both", expand=True, padx=(T.px(T.PANEL_PAD), 0),
                  pady=(0, T.px(T.SP_LG)))
        root = body.inner
        # 注意：属性名不能用 `_root` —— 那是 tk.Misc 的保留方法（nametowidget 解析
        # 路径时要调用），实例属性遮蔽它会让任何「从面板向下遍历子控件」的标准代码
        # 直接 TypeError。
        self._fields = root

        # -- 水印内容（写什么） ----------------------------------------------
        section(root, "水印内容")
        self.text_area = TextArea(root, value=self._spec.text, height=3, command=self._on_text)
        self.text_area.pack(fill="x")
        tk.Label(root, text="支持多行（回车换行）", bg=T.PANEL, fg=T.TEXT_FAINT,
                 font=(T.UI_FAMILY, T.sf(9)), anchor="w").pack(fill="x", pady=(T.px(T.SP_XS), 0))

        field_label(root, "字体").pack(fill="x", pady=(T.px(T.SP_XXL), 0))
        self.font_combo = ttk.Combobox(root, state="readonly", style="App.TCombobox",
                                       font=(T.UI_FAMILY, T.sf(10)), values=[""])
        self.font_combo.pack(fill="x", pady=(T.px(T.SP_XS), 0))
        self.font_combo.bind("<<ComboboxSelected>>", self._on_font)
        # 滚轮经过收起状态的下拉框：滚动面板，但不静默换字体（ttk 默认会改选中值）；
        # 展开后的列表由 ScrolledFrame._inside 排除，只滚列表、不带面板
        self.font_combo.bind("<MouseWheel>", self._on_combo_wheel)
        # 弹出列表的滚动条默认无 style（走主题宽滚条），这里统一到项目细条样式
        style_combobox_popdown(self.font_combo, list_font=(T.UI_FAMILY, T.sf(10)))

        self.font_pct = SliderField(
            root, "字号", *FONT_PCT_RANGE, self._spec.font_pct, FONT_PCT_STEP,
            self._on_font_pct, unit="%", decimals=0)
        self.font_pct.pack(fill="x", pady=(T.px(T.SP_XXL), 0))

        # -- 版式（怎么排：边距 / 角度决定平铺网格） ---------------------------
        self.sec_tile = section(root, "版式")
        self.margin_pct = SliderField(
            root, "四周边距", *MARGIN_PCT_RANGE, self._spec.margin_pct, MARGIN_PCT_STEP,
            self._on_margin_pct, unit="%", decimals=0, hint=_MARGIN_HINT)
        self.margin_pct.pack(fill="x")
        # 角度是**循环量**（0° 与 360° 是同一个方向），线性轨道上它们却分居两端 ——
        # 用圆盘表示才对；盘上那条贯穿圆心的方向线就是水印文字的真实走向。
        # 正方向取**顺时针**（与读时钟同向）：PIL 的 rotate 是逆时针，故
        # wm.layout 渲染时传 rotate(-angle) 补偿，两处是一对、改一个必须改另一个。
        self.angle = SliderField(root, "旋转角度", *ANGLE_RANGE, self._spec.angle, ANGLE_STEP,
                                 self._on_angle, unit="°", dial=True, hint=_ANGLE_HINT)
        self.angle.pack(fill="x", pady=(T.px(T.SP_XXL), 0))

        # -- 外观（长什么样） ------------------------------------------------
        section(root, "外观")
        self.color = ColorField(root, self._on_color, value=self._spec.color,
                                presets=COLOR_PRESETS)
        self.color.pack(fill="x")
        # 不透明度：界面按百分比 5–100 显示（单位 "%" 并入字段名），内部仍为 0.05–1.0
        # 的 alpha —— 两个方向都经 spec 的换算函数，越界自动夹到最近边界。
        self.opacity = SliderField(root, "不透明度", *OPACITY_PCT_RANGE,
                                   opacity_to_pct(self._spec.opacity), OPACITY_PCT_STEP,
                                   self._on_opacity, unit="%", decimals=0)
        self.opacity.pack(fill="x", pady=(T.px(T.SP_XXL), 0))

    # ------------------------------------------------------------------
    # 事件 -> 规格
    # ------------------------------------------------------------------

    def _emit(self) -> None:
        if self._on_change is not None:
            self._on_change(self.spec())

    def _on_text(self, value: str) -> None:
        self._spec = self._spec.copy(text=value).normalized()
        self._emit()

    def _on_font(self, _event=None) -> None:
        # 下拉框里放的是中文显示名，真值按**下标**回查英文原名（重名项也 unambiguous）
        index = self.font_combo.current()
        if 0 <= index < len(self._families):
            self._spec = self._spec.copy(font_family=self._families[index]).normalized()
            self._emit()

    def _on_combo_wheel(self, event) -> str:
        """下拉框（**收起**状态）上的滚轮：转发给滚动容器并拦截。

        两个方向：滚动整个参数面板（收起的下拉框本身没有可滚内容，不做转发就会
        变成"滚轮死区"），同时拦掉 ttk 默认的「滚轮换值」（悬停一下就把字体改了）。

        列表**展开**时滚轮落在弹出的列表上，走的是列表自己的绑定
        （``ComboboxListbox``），由 ``ScrolledFrame._inside`` 排除在面板之外。
        """
        self._body.accumulate(event.delta)
        return "break"

    def _on_font_pct(self, value: float) -> None:
        self._spec = self._spec.copy(font_pct=value).normalized()
        self._emit()

    def _on_margin_pct(self, value: float) -> None:
        self._spec = self._spec.copy(margin_pct=value).normalized()
        self._emit()

    def _on_angle(self, value: float) -> None:
        self._spec = self._spec.copy(angle=value).normalized()
        self._emit()

    def _on_color(self, value: str) -> None:
        self._spec = self._spec.copy(color=value).normalized()
        self._emit()

    def _on_opacity(self, value: float) -> None:
        self._spec = self._spec.copy(opacity=opacity_from_pct(value)).normalized()
        self._emit()

    # ------------------------------------------------------------------
    # 公共 API
    # ------------------------------------------------------------------

    def spec(self) -> WatermarkSpec:
        """当前参数（已合法化）。"""
        return self._spec.normalized()

    def load_families(self) -> None:
        """从系统发现字体并填入下拉框（微软雅黑优先，鸿蒙排在后面）。

        下拉框显示**中文名**（``_labels``），选中项按下标回查英文原名
        （``_families``）用于渲染。
        """
        self._families = fonts.families()
        if not self._families:
            return
        self._labels = fonts.display_names(self._families)
        self.font_combo.configure(values=self._labels)
        current = self._spec.font_family
        if current not in self._families:
            self._spec = self._spec.copy(font_family=self._families[0]).normalized()
        self._select_family(self._spec.font_family)

    def _select_family(self, family: str) -> None:
        """按英文原名定位下拉框选中项；不在列表里则回退第一项。"""
        if not self._families:
            return
        index = self._families.index(family) if family in self._families else 0
        self.font_combo.current(index)

    def _selected_family(self) -> str:
        """下拉框**当前选中项**对应的英文原名（显示名只是标签，真值按下标回查）。

        字体表还没发现（``load_families`` 未调用）时沿用 ``_spec`` 现值，不做改动。
        """
        if not self._families:
            return self._spec.font_family
        index = self.font_combo.current()
        if 0 <= index < len(self._families):
            return self._families[index]
        return self._spec.font_family

    def load_spec(self, spec: WatermarkSpec) -> None:
        """把一份参数灌回界面（载入配置 / 重置时用）。

        ⚠️ ``SliderField.set(..., notify=False)`` 会按各自步长 snap、并把越界值夹
        回区间，但**不**回调 ``_on_*`` —— 于是内部 ``_spec`` 会停在**未 snap** 的
        原值上：界面显示 12.0、控件取值 12.0、渲染却用 12.4（不透明度同理：显示
        30%、内部 0.304）。所以灌完控件后必须用**控件的实际取值反向回写** ``_spec``，
        把「显示 = 控件 = 真值」变成一条不变式。
        """
        self._spec = spec.normalized()
        self.text_area.set(self._spec.text)
        self._select_family(self._spec.font_family)
        self.font_pct.set(self._spec.font_pct, notify=False)
        self.margin_pct.set(self._spec.margin_pct, notify=False)
        self.angle.set(self._spec.angle, notify=False)
        self.color.set(self._spec.color)
        self.opacity.set(opacity_to_pct(self._spec.opacity), notify=False)
        # 回写：以控件的实际取值为准（已 snap 到步长 / 已夹到区间 / 已按显示名解析）
        self._spec = self._spec.copy(
            text=self.text_area.get(),
            font_family=self._selected_family(),
            font_pct=self.font_pct.get(),
            margin_pct=self.margin_pct.get(),
            angle=self.angle.get(),
            color=self.color.get(),
            opacity=opacity_from_pct(self.opacity.get()),
        ).normalized()


__all__ = ["ParamPanel", "ScrolledFrame"]
