"""「旋转角度」圆盘控件回归测试。

对应实现：``wm/ui/theme.py`` 的 ``_AngleDial``（由 ``SliderField(dial=True)`` 使用）。
覆盖的核心契约 —— 这些都是「换成圆盘」之所以成立的前提：

1. **几何反算**：屏幕点 -> 角度的映射正确（0° 在 3 点钟方向、逆时针为正）；
2. **方向线真的跟着角度转**：0° 是水平线、90° 是竖直线且箭头朝上；
   这条守的是「盘上的线 = 水印文字走向」这个承诺，画错就等于骗用户；
3. **循环量语义**：0°/360° 相邻，环绕时不炸、不越界；
4. Shift 吸附 15°；圆心死区内按下不取值（那里方向极不稳定）；
5. 禁用态更淡且完全不响应；
6. ``set()`` **不**回调 command（与 ttk.Scale / FlatScale 行为一致，
   否则 ``SliderField.set`` 里的显式 ``_emit()`` 会变成重复通知）。
"""
from __future__ import annotations

import math
import tkinter as tk
import unittest


def _root():
    """创建根窗口；无显示环境时抛 SkipTest（**不**算通过）。"""
    try:
        root = tk.Tk()
    except Exception as exc:
        raise unittest.SkipTest("需要可用显示环境：%s" % exc)
    root.geometry("300x300")
    return root


def _make_dial(root, value=0.0):
    """直接构造圆盘（不经 SliderField），返回 (dial, 回调记录)。"""
    from wm.ui import theme as T
    calls = []
    dial = T._AngleDial(root, from_=0.0, to=360.0, command=lambda v: calls.append(v))
    dial.set(value)
    dial.pack(padx=8, pady=8)
    root.update_idletasks()
    root.update()
    return dial, calls


class _Ev:
    """最小事件替身：只为携带 (x, y, state)，避开 Tk 合成事件对 Shift 掩码的挑剔。"""

    def __init__(self, x, y, state=0):
        self.x, self.y, self.state = x, y, state


def _pts(dial, value):
    """返回「在圆环上位于 ``value`` 角度」处的画布坐标。"""
    c = dial._center()
    r = dial._radius()
    rad = math.radians(value)
    return c + math.cos(rad) * r, c - math.sin(rad) * r


def _line_coords(dial):
    """取出方向线（type == 'line'）的坐标；找不到即断言失败。"""
    for item in dial.find_all():
        if dial.type(item) == "line":
            return [float(v) for v in dial.coords(item)]
    raise AssertionError("圆盘上找不到方向线图元")


def test_dial_maps_click_position_to_angle():
    """几何反算：3 点钟 → 0°、12 点钟 → 90°、9 点钟 → 180°、6 点钟 → 270°。"""
    root = _root()
    try:
        dial, calls = _make_dial(root)
        for expect, where in ((0, "3 点钟"), (90, "12 点钟"),
                              (180, "9 点钟"), (270, "6 点钟")):
            x, y = _pts(dial, expect)
            got = dial._value_of(x, y)
            assert abs(((got - expect + 180) % 360) - 180) < 1e-6, \
                f"{where} 应映射到 {expect}°，实际 {got!r}"

        # 走真实绑定：在 45° 处按下，应改值并回调一次。
        # 容差 2.5°：事件坐标必须是整数像素，而圆环半径约 40px ⇒ 1px ≈ 1.4°。
        calls.clear()
        x, y = _pts(dial, 45)
        dial.event_generate("<Button-1>", x=round(x), y=round(y))
        root.update()
        assert abs(dial.get() - 45) < 2.5, f"点击 45° 处应约为 45，实际 {dial.get()!r}"
        assert len(calls) == 1, f"按下应恰好回调一次，实际 {len(calls)} 次"

        # 按住拖动：继续移动应持续更新
        x2, y2 = _pts(dial, 200)
        dial.event_generate("<B1-Motion>", x=round(x2), y=round(y2))
        root.update()
        assert abs(dial.get() - 200) < 2.5, f"拖动到 200° 处应约为 200，实际 {dial.get()!r}"
        assert len(calls) == 2, f"拖动应各发一次，实际 {len(calls)} 次"
    finally:
        root.destroy()


def test_dial_direction_line_follows_angle():
    """方向线必须真的跟着角度转 —— 这是「线即文字走向」承诺的守门人。"""
    root = _root()
    try:
        dial, _ = _make_dial(root)

        dial.set(0)
        x0, y0, x1, y1 = _line_coords(dial)
        assert abs(y1 - y0) <= 1.0, f"0° 应为水平线，实际 {y0}->{y1}"
        assert x1 > x0, "0° 的正方向应朝屏幕右侧（与水印文字走向一致）"

        dial.set(90)
        x0, y0, x1, y1 = _line_coords(dial)
        assert abs(x1 - x0) <= 1.0, f"90° 应为竖直线，实际 {x0}->{x1}"
        assert y1 < y0, "90° 的正方向应朝屏幕上方（逆时针为正）"

        # 「贯穿圆心」的正确判据有两条，缺一不可：
        #   a) 圆心落在**这条线**上（点到直线距离 ≈ 0）——注意不能拿「中点」判，
        #      正方向那条要一直伸到箭头尖端，所以线本身就是**非对称**的；
        #   b) 两个端点分居圆心两侧（说明是贯穿，而非从圆心单侧发出）。
        c = dial._center()
        for value in (0, 37, 120, 300):
            dial.set(value)
            x0, y0, x1, y1 = _line_coords(dial)
            dx, dy = x1 - x0, y1 - y0
            length = math.hypot(dx, dy)
            assert length > 0, "方向线长度不应为 0"
            dist = abs(dx * (c - y0) - dy * (c - x0)) / length
            assert dist <= 1.0, f"{value}° 的圆心到方向线距离 {dist:.2f}px（应 ≈0）"
            cross = (x0 - c) * (x1 - c) + (y0 - c) * (y1 - c)
            assert cross < 0, f"{value}° 的方向线未贯穿圆心（两端同侧）"
    finally:
        root.destroy()


def test_dial_is_cyclic_and_never_escapes_range():
    """循环量语义：贴到 0°/360° 附近不跳变，越界值一律钳制。"""
    root = _root()
    try:
        dial, calls = _make_dial(root)

        # 从 359° 顺时针拖过 0°：应落在 0..5 的邻域，而不是炸到 180 或负值
        for probe in (359, 1):
            x, y = _pts(dial, probe)
            dial._on_press(_Ev(x, y))
            got = dial.get()
            assert 0.0 <= got <= 360.0, f"值越界：{got!r}"
        assert abs(min(dial.get(), 360 - dial.get())) <= 5.0, \
            f"跨 0° 拖动后应仍在 0° 附近，实际 {dial.get()!r}"

        # set() 是上层（数字框 / 步进器）的入口，必须钳制而不是抛错
        dial.set(360)
        assert dial.get() == 360.0, dial.get()
        dial.set(999)
        assert dial.get() == 360.0, "上界应钳制"
        dial.set(-40)
        assert dial.get() == 0.0, "下界应钳制"

        # set() 不得回调：否则 SliderField.set 里的 _emit() 会变成重复通知
        calls.clear()
        dial.set(123)
        dial.set(45)
        assert calls == [], f"set() 不应回调 command，实际收到 {calls!r}"
    finally:
        root.destroy()


def test_dial_shift_snaps_to_15_and_center_is_dead():
    """Shift 吸附 15°；圆心死区内按下不取值（避免方向抖动导致数值乱跳）。"""
    root = _root()
    try:
        dial, calls = _make_dial(root)

        # 不带 Shift：落在哪儿是哪儿（只要在环附近）
        x, y = _pts(dial, 37)
        dial._on_press(_Ev(x, y))
        raw = dial.get()
        assert abs(raw - 37) < 1.5, raw

        # 带 Shift（掩码 0x1）：吸附到 15° 的整数倍（就近取整：98 更近 105 而非 90）
        for probe, expect in ((37, 30), (53, 60), (7, 0), (98, 105)):
            x, y = _pts(dial, probe)
            dial._on_press(_Ev(x, y, state=0x1))
            got = dial.get()
            assert abs(got - expect) <= 1e-9, f"{probe}° + Shift 应吸附到 {expect}，实际 {got!r}"
            assert abs(got % 15) < 1e-9, "吸附结果必须是 15 的整数倍"

        # 圆心死区：按下应完全被忽略（值不变、也不回调）
        c = dial._center()
        before = dial.get()
        calls.clear()
        dial._on_press(_Ev(c, c))
        dial._on_press(_Ev(c + dial._radius() * 0.2, c))
        assert dial.get() == before, "圆心附近按下不应改值"
        assert calls == [], "圆心附近按下不应回调"
    finally:
        root.destroy()


def test_dial_disabled_is_dimmed_and_inert():
    """禁用态：配色降到 OFF 组，且按下 / 拖动完全不响应。"""
    from wm.ui import theme as T
    root = _root()
    try:
        dial, calls = _make_dial(root, value=30)

        def ring_fill():
            for item in dial.find_all():
                if dial.type(item) == "oval":
                    return dial.itemcget(item, "outline")
            raise AssertionError("圆盘上找不到圆环图元")

        assert ring_fill() == T.DIAL_RING, ring_fill()
        assert dial.state() == (), "初始应为启用态"

        dial.state(["disabled"])
        assert dial._disabled is True
        assert ring_fill() == T.DIAL_RING_OFF, "禁用态圆环应变淡"

        before = dial.get()
        calls.clear()
        x, y = _pts(dial, 200)
        dial._on_press(_Ev(x, y))
        dial._on_drag(_Ev(*_pts(dial, 300)))
        assert dial.get() == before, "禁用态不应改值"
        assert calls == [], "禁用态不应回调"

        dial.state(["!disabled"])
        assert dial._disabled is False
        assert ring_fill() == T.DIAL_RING, "恢复启用后配色应复原"
    finally:
        root.destroy()


def test_sliderfield_dial_mode_wires_up_and_keeps_precision():
    """走真实字段：dial=True 用圆盘、其余仍是线性滑块，且数值精度靠数字框兜住。"""
    from wm.ui import theme as T
    root = _root()
    try:
        calls = []
        angle = T.SliderField(root, "旋转角度", 0, 360, 30, 1,
                              lambda v: calls.append(v), unit="°", dial=True)
        angle.pack(fill="x", padx=10)
        font_pct = T.SliderField(root, "字号", 1, 40, 4, 1,
                                 lambda v: calls.append(v), unit="%")
        font_pct.pack(fill="x", padx=10)
        root.update_idletasks()
        root.update()

        assert isinstance(angle.scale, T._AngleDial), "角度字段应使用圆盘"
        assert isinstance(font_pct.scale, T.FlatScale), "其余字段应保持线性滑块"
        # 圆盘是方形，**不能**横向填满（否则圆会被拉成椭圆）
        assert angle.scale.winfo_width() == angle.scale.winfo_height(), \
            (angle.scale.winfo_width(), angle.scale.winfo_height())
        assert font_pct.scale.winfo_width() > font_pct.scale.winfo_height(), \
            "线性滑块应当是扁的"

        # 盘上拖动 -> 经字段的 _snap 落到整数步长
        dial = angle.scale
        dial._on_press(_Ev(*_pts(dial, 42.4)))
        root.update()
        assert calls, "拖动圆盘应触发字段回调"
        assert abs(calls[-1] - round(calls[-1])) < 1e-9, f"应吸附到整数步长：{calls[-1]!r}"
        assert abs(calls[-1] - 42) <= 1.5, calls[-1]

        # 圆盘同样支持「禁用 -> 启用」这条既有链路
        angle.set_enabled(False)
        assert dial._disabled is True, "字段禁用应透传到圆盘"
        angle.set_enabled(True)
        assert dial._disabled is False, "字段启用应透传到圆盘"
    finally:
        root.destroy()
