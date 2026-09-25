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


def _tris(sp):
    """返回 (上三角 fill, 下三角 fill)，**按质心 y 判定上下**而非绘制顺序。

    命中半区时画布上还会多一层浅底。浅底是 ``round_rect`` 的平滑多边形，顶点数
    远多于三角（20 vs 6），据此剔除顶点最多的那项；剩下的按质心 y 升序即为
    「上、下」。这样即使以后绘制顺序变了，断言依然成立。
    """
    items = sp.find_all()
    assert len(items) >= 2, "步进三角应常驻绘制（至少两个图元）"
    polys = []
    for item in items:
        coords = list(map(float, sp.coords(item)))
        ys = coords[1::2]
        polys.append((sum(ys) / len(ys), len(coords), item))
    most = max(p[1] for p in polys)
    tris = [p for p in polys if p[1] < most] if len(polys) > 2 else polys
    tris.sort(key=lambda p: p[0])
    assert len(tris) == 2, f"应恰好两个三角，实际 {len(tris)}（图元 {len(items)} 个）"
    return sp.itemcget(tris[0][2], "fill"), sp.itemcget(tris[1][2], "fill")


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
        assert up == T.STEP_IDLE and dn == T.STEP_IDLE, (up, dn)
        assert len(sp.find_all()) == 2, "常态不应铺浅底"

        # 2) 悬停字段：两个三角一起加深
        field.entry.event_generate("<Enter>")
        field.update()
        assert sp._hover is True, "悬停应置 hover 态"
        up, dn = _tris(sp)
        assert up == T.STEP_HOVER and dn == T.STEP_HOVER, (up, dn)
        assert field.entry.winfo_height() == sp._ch, "步进器高度应等于输入框"

        # 3) 命中上半区：上三角转主题色，并铺出浅底
        sp.event_generate("<Motion>", x=max(2, sp._cw // 2),
                          y=max(2, sp._ch // 4))
        field.update()
        up, dn = _tris(sp)
        assert up == T.STEP_ACTIVE and dn == T.STEP_HOVER, (up, dn)
        assert len(sp.find_all()) == 3, "命中半区应铺浅底"

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
        assert up == T.STEP_IDLE and dn == T.STEP_IDLE, (up, dn)
        assert len(sp.find_all()) == 2, "移开后三角仍在（常驻），只是回到淡灰"
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
        assert up == T.STEP_OFF and dn == T.STEP_OFF, (up, dn)

        before = field.get()
        sp.event_generate("<Motion>", x=max(2, sp._cw // 2), y=max(2, sp._ch // 4))
        _click(field, 0.25)
        assert len(sp.find_all()) == 2, "禁用态不铺浅底"
        assert abs(field.get() - before) <= 1e-9, "禁用态点击不应改值"

        field.set_enabled(True)
        assert sp._enabled is True, "恢复启用后应可交互"
        up, _ = _tris(sp)
        assert up == T.STEP_IDLE, "恢复后应回到常态色"
        _click(field, 0.25)
        assert abs(field.get() - (before + 1)) <= 1e-9, "恢复后点击应生效"
    finally:
        root.destroy()
