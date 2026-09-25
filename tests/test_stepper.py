"""数字框「悬停步进器」回归测试。

对应实现：``wm/ui/theme.py`` 的 ``SliderField`` + ``_HoverStepper``。
覆盖交互契约：

1. 悬停输入框显示步进器，移开后**延时**隐藏（隐藏后不残留绘制项）；
2. 上三角 +step、下三角 −step，并在 ``[lo, hi]`` 处钳制；
3. 手动输入仍然有效，且**未提交**的手输值会成为步进的基准；
4. 禁用态下步进器不绘制、不响应。
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


def _click(field, frac):
    """在步进器纵向 ``frac`` 处（0=顶、1=底）模拟一次左键点击。"""
    sp = field.stepper
    y = max(2, min(sp._ch - 2, int(sp._ch * frac)))
    sp.event_generate("<Button-1>", x=max(2, sp._cw // 2), y=y)
    field.update()


def test_stepper_hover_click_and_clamp():
    root = _root()
    try:
        field, _ = _make_field(root)
        sp = field.stepper
        assert sp is not None, "步进器未创建"
        assert sp._visible is False and not sp.find_all(), "初始应为不可见的空画布"

        field.entry.event_generate("<Enter>")
        field.update()
        assert sp._visible and sp.find_all(), "悬停后步进器应显现"
        assert field.entry.winfo_height() == sp._ch, "步进器高度应等于输入框"

        _click(field, 0.25)   # 上半 → +1
        assert abs(field.get() - 31) <= 1e-9, field.get()
        _click(field, 0.75)   # 下半 → -1
        assert abs(field.get() - 30) <= 1e-9, field.get()

        field.set(360)
        _click(field, 0.25)
        assert abs(field.get() - 360) <= 1e-9, "上界应钳制"
        field.set(0)
        _click(field, 0.75)
        assert abs(field.get() - 0) <= 1e-9, "下界应钳制"

        field.entry.event_generate("<Leave>")
        deadline = time.time() + 1.0
        while time.time() < deadline and sp._visible:
            field.update()
            time.sleep(0.01)
        assert sp._visible is False, "移开后应隐藏"
        assert not sp.find_all(), "隐藏后不应残留绘制项"
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


def test_stepper_disabled_stays_hidden():
    root = _root()
    try:
        field, _ = _make_field(root)
        sp = field.stepper
        field.set_enabled(False)
        assert sp._enabled is False, "禁用后步进器应禁用"
        sp.set_visible(True)
        field.update()
        assert not sp.find_all(), "禁用态不应绘制三角"
        field.set_enabled(True)
        assert sp._enabled is True, "恢复启用后应可交互"
    finally:
        root.destroy()
