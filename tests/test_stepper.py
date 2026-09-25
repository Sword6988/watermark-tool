"""数字框「常驻步进三角」回归测试。

对应实现：``wm/ui/theme.py`` 的 ``SliderField`` + ``_Stepper``。
覆盖交互契约：

1. 三角**常驻显示**（不悬停也在），悬停 / 命中只是改深浅，不切换显隐；
2. 上三角 +step、下三角 −step，并在 ``[lo, hi]`` 处钳制；
3. 手动输入仍然有效，且**未提交**的手输值会成为步进的基准；
4. 显隐（此处为深浅）切换**不引起布局跳动**；
5. 禁用态三角更淡且不响应悬停 / 点击。
"""
from __future__ import annotations

import time
import tkinter as tk
import unittest


def _root():
    """创建根窗口；无显示环境时抛 SkipTest（**不**算通过）。"""
    try:
        root = tk.Tk()
    except Exception as exc:
        raise unittest.SkipTest("需要可用显示环境：%s" % exc)
    root.geometry("460x220")
    return root


def _make_field(root):
    """构造一个带单位的 SliderField 并完成布局，返回 (field, 回调记录列表)。"""
    from wm.ui import theme as T
    calls = []
    field = T.SliderField(root, "旋转角度", 0, 360, 30, 1,
                          lambda v: calls.append(v), unit="°")
    field.pack(fill="x", padx=12, pady=12)
    root.update_idletasks()
    root.update()
    return field, calls


def _img(sp):
    """取步进器当前渲染的位图（paint_aa 存在 ``_aa_image`` 上）。"""
    img = getattr(sp, "_aa_image", None)
    assert img is not None, "步进器应已渲染出抗锯齿位图（paint_aa）"
    return img


def _pix(img, fx, fy):
    """按**比例**取样：fx/fy ∈ [0,1]，测试不依赖具体 DPI 下的像素尺寸。"""
    x = min(img.width - 1, max(0, int(round(fx * img.width))))
    y = min(img.height - 1, max(0, int(round(fy * img.height))))
    return img.getpixel((x, y))


def _hex2rgb(c):
    c = c.lstrip("#")
    return tuple(int(c[i:i + 2], 16) for i in (0, 2, 4))


def _near(c, ref, tol=30):
    return all(abs(c[i] - ref[i]) <= tol for i in range(3))


# 上/下三角的内部采样点（比例坐标）：上三角质心约 (0.5, 0.35)，下三角约 (0.5, 0.68)；
# 两者之间的缝隙带 y≈0.5 —— 命中浅底画不画，看这里就知道。
_UP, _DN, _GAP = (0.5, 0.35), (0.5, 0.68), (0.5, 0.5)


def _tris(sp):
    """返回 (上三角 RGB, 下三角 RGB)，按内部采样点取色。"""
    img = _img(sp)
    return _pix(img, *_UP), _pix(img, *_DN)


def _click(field, frac):
    """在步进器纵向 ``frac`` 处（0=顶、1=底）模拟一次左键点击。"""
    sp = field.stepper
    y = max(2, min(sp._ch - 2, int(sp._ch * frac)))
    sp.event_generate("<Button-1>", x=max(2, sp._cw // 2), y=y)
    field.update()


def test_stepper_always_drawn_with_three_hover_states():
    """常驻 + 三级状态：常态淡灰 → 悬停加深 → 命中半区主题色 + 浅底。"""
    from wm.ui import theme as T
    root = _root()
    try:
        field, _ = _make_field(root)
        sp = field.stepper
        assert sp is not None, "步进器未创建"

        # 1) 常态：三角已在（不悬停也不空），且没有浅底
        up, dn = _tris(sp)
        idle = _hex2rgb(T.STEP_IDLE)
        assert _near(up, idle) and _near(dn, idle), (up, dn)
        bgc = _hex2rgb(T.PANEL)
        assert _near(_pix(_img(sp), *_GAP), bgc), "常态不应铺浅底"

        # 2) 悬停字段：两个三角一起加深
        field.entry.event_generate("<Enter>")
        field.update()
        assert sp._hover is True, "悬停应置 hover 态"
        up, dn = _tris(sp)
        hover = _hex2rgb(T.STEP_HOVER)
        assert _near(up, hover) and _near(dn, hover), (up, dn)
        assert field.entry.winfo_height() == sp._ch, "步进器高度应等于输入框"

        # 3) 命中上半区：上三角转主题色，并铺出浅底
        sp.event_generate("<Motion>", x=max(2, sp._cw // 2),
                          y=max(2, sp._ch // 4))
        field.update()
        up, dn = _tris(sp)
        active = _hex2rgb(T.STEP_ACTIVE)
        assert _near(up, active) and _near(dn, hover), (up, dn)
        row_hover = _hex2rgb(T.ROW_HOVER)
        assert _near(_pix(_img(sp), *_GAP), row_hover), "命中半区应铺浅底"

        # 4) 加减速与上下界钳制
        _click(field, 0.25)   # 上半 → +1
        assert abs(field.get() - 31) <= 1e-9, field.get()
        sp.event_generate("<Motion>", x=max(2, sp._cw // 2),
                          y=sp._ch - max(2, sp._ch // 4))
        _click(field, 0.75)   # 下半 → -1
        assert abs(field.get() - 30) <= 1e-9, field.get()

        field.set(360)
        _click(field, 0.25)
        assert abs(field.get() - 360) <= 1e-9, "上界应钳制"
        field.set(0)
        _click(field, 0.75)
        assert abs(field.get() - 0) <= 1e-9, "下界应钳制"

        # 5) 移开：回到淡灰，**但仍然画着**
        field.entry.event_generate("<Leave>")
        deadline = time.time() + 1.0
        while time.time() < deadline and sp._hover:
            field.update()
            time.sleep(0.01)
        assert sp._hover is False, "移开后应退出 hover 态"
        up, dn = _tris(sp)
        assert _near(up, idle) and _near(dn, idle), (up, dn)
        assert _near(_pix(_img(sp), *_GAP), bgc), "移开后浅底应退去（三角仍常驻）"
    finally:
        root.destroy()


def test_stepper_keeps_manual_input():
    root = _root()
    try:
        field, _ = _make_field(root)
        # 手动输入后提交
        field.var.set("120")
        field._commit_entry()
        assert abs(field.get() - 120) <= 1e-9, "手输提交后应为 120"
        # 未提交的手输值应成为步进基准（而不是旧的内部值）
        field.var.set("50")
        field._spin(1)
        assert abs(field.get() - 51) <= 1e-9, field.get()
        # 非法输入不应改变数值
        field.var.set("abc")
        field._commit_entry()
        assert abs(field.get() - 51) <= 1e-9, "非法输入应回退上次有效值"
        # 注意：after_cancel 必须用**注册该 job 的那个控件**（这里 job 由
        # ``field.after`` 调度），用 root.after_cancel 会留下悬空命令、
        # 导致 destroy 抛 "can't delete Tcl command"。
        if field._invalid_job is not None:
            field.after_cancel(field._invalid_job)
            field._invalid_job = None
    finally:
        root.destroy()


def test_stepper_state_switch_never_shifts_layout():
    """状态切换只改颜色，不动几何 —— 输入框与外框的位置 / 尺寸必须不变。"""
    root = _root()
    try:
        field, _ = _make_field(root)
        sp = field.stepper
        box_w = field.entry_box.winfo_width()
        box_h = field.entry_box.winfo_height()
        entry_x = field.entry.winfo_x()

        field.entry.event_generate("<Enter>")
        field.update()
        sp.event_generate("<Motion>", x=max(2, sp._cw // 2), y=max(2, sp._ch // 4))
        field.update()
        assert field.entry_box.winfo_width() == box_w, "悬停不应改变外框宽度"
        assert field.entry_box.winfo_height() == box_h, "悬停不应改变外框高度"
        assert field.entry.winfo_x() == entry_x, "悬停不应挪动输入框"

        field.entry.event_generate("<Leave>")
        deadline = time.time() + 1.0
        while time.time() < deadline and sp._hover:
            field.update()
            time.sleep(0.01)
        assert field.entry_box.winfo_width() == box_w, "移开后宽度应复原"
        assert field.entry.winfo_x() == entry_x, "移开后位置应复原"
    finally:
        root.destroy()


def test_stepper_disabled_is_dimmed_and_inert():
    """禁用态：三角画得更淡，且不铺浅底、不响应点击。"""
    from wm.ui import theme as T
    root = _root()
    try:
        field, _ = _make_field(root)
        sp = field.stepper
        field.set_enabled(False)
        assert sp._enabled is False, "禁用后步进器应禁用"
        up, dn = _tris(sp)
        off = _hex2rgb(T.STEP_OFF)
        assert _near(up, off) and _near(dn, off), (up, dn)

        before = field.get()
        sp.event_generate("<Motion>", x=max(2, sp._cw // 2), y=max(2, sp._ch // 4))
        _click(field, 0.25)
        assert _near(_pix(_img(sp), *_GAP), _hex2rgb(T.PANEL)), "禁用态不铺浅底"
        assert abs(field.get() - before) <= 1e-9, "禁用态点击不应改值"

        field.set_enabled(True)
        assert sp._enabled is True, "恢复启用后应可交互"
        up, _ = _tris(sp)
        assert _near(up, _hex2rgb(T.STEP_IDLE)), "恢复后应回到常态色"
        _click(field, 0.25)
        assert abs(field.get() - (before + 1)) <= 1e-9, "恢复后点击应生效"
    finally:
        root.destroy()
