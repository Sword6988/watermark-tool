"""P0 加固项的回归用例：专门盯「异常路径不能让用户只能杀进程」。

这里的每一条都对应审计里一条**严重**问题，且都是**修复前必挂、修复后必过**：

* S2 —— 结果轮询里单个回调抛错会断掉整条 ``after`` 链（进度冻结、按钮全灰）；
* S3 —— 批处理 ``on_done`` 丢失会让界面永久停在「处理中」；
* S4 —— 日志目录不可写时，崩溃兜底自身崩溃（双击 exe 完全无反应）；
* S1 —— 关窗时先删明文、后等线程，会把正在读的文件在它脚下删掉（附带验证
  ``_release_all_plain`` 逐个兜错，一个删不掉不会连累其余）。
"""

from __future__ import annotations

import os
import queue
import sys
import tempfile
import unittest
from typing import List

from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import main as entry
from wm.spec import WatermarkSpec
from wm.ui import app as ui_app
from wm.ui import batch

WHITE = (255, 255, 255)


def _png(tmp: str, name: str = "img.png") -> str:
    path = os.path.join(tmp, name)
    Image.new("RGB", (80, 60), WHITE).save(path)
    return path


# ---------------------------------------------------------------------------
# S3：on_done 必须送达
# ---------------------------------------------------------------------------

def test_batch_on_done_reaches_even_when_progress_callback_throws() -> None:
    """on_progress 抛错不能吞掉 on_done —— 否则界面永远停在「处理中」。"""
    with tempfile.TemporaryDirectory() as tmp:
        files = [_png(tmp, "a.png"), _png(tmp, "b.png")]
        done: List[tuple] = []

        def _boom(*_a, **_k):
            raise RuntimeError("进度回调炸了")

        # 异常仍会向上传播（这是有意的：由 _batch_worker 兜底），这里只关心
        # on_done **有没有先送达** —— 那才是界面解除 busy 的唯一信号。
        try:
            batch.run_batch(
                files, WatermarkSpec().normalized(), tmp,
                is_cancelled=lambda: False,
                on_progress=_boom,
                on_log=lambda text: None,
                on_done=lambda s, f, c: done.append((s, f, c)))
        except RuntimeError:
            pass
        assert done, "on_progress 抛错后 on_done 丢失：界面会永久 busy"


def test_batch_on_done_reaches_even_when_read_path_throws() -> None:
    """``read_path`` 在 per-file try **之外** —— 它抛错以前会直接带崩整批。"""
    with tempfile.TemporaryDirectory() as tmp:
        files = [_png(tmp)]
        done: List[tuple] = []

        def _boom(_path: str) -> str:
            raise TypeError("明文映射表坏了")

        try:
            batch.run_batch(
                files, WatermarkSpec().normalized(), tmp,
                is_cancelled=lambda: False,
                on_progress=lambda *a, **k: None,
                on_log=lambda text: None,
                on_done=lambda s, f, c: done.append((s, f, c)),
                read_path=_boom)
        except TypeError:
            pass
        assert done, "read_path 抛错后 on_done 丢失"
        assert done[0][0] == 0, f"没有任何文件成功，实际 {done[0][0]}"


# ---------------------------------------------------------------------------
# S2：轮询链不能断
# ---------------------------------------------------------------------------

class _RootStub:
    """只提供 ``after`` / ``after_cancel`` 的根窗口替身（不建真实 Tk 窗口）。"""

    def __init__(self) -> None:
        self.scheduled = 0

    def after(self, _ms: int, _fn) -> str:
        self.scheduled += 1
        return "job-%d" % self.scheduled

    def after_cancel(self, _job) -> None:
        pass


def _bare_app() -> ui_app.App:
    """造一个只带轮询所需字段的 App（与 QA 用例同样不走 __init__）。"""
    instance = ui_app.App.__new__(ui_app.App)
    instance.root = _RootStub()
    instance._queue = queue.Queue()
    instance._closing = False
    instance._preview_pending = False
    instance._preview_busy = False
    instance._poll_job = None
    return instance


def test_poll_reschedules_after_handler_exception() -> None:
    """单条结果处理失败后，轮询必须继续排下一次（否则进度永久冻结）。"""
    instance = _bare_app()
    instance._queue.put({"kind": "progress", "value": 1.0, "text": "x"})
    instance._handle_result = lambda _item: (_ for _ in ()).throw(RuntimeError("boom"))

    instance._poll_results()

    assert instance.root.scheduled == 1, \
        "处理异常后没有续接 after：轮询链已断（界面会永久 busy）"
    assert instance._poll_job


def test_poll_does_not_reschedule_when_closing() -> None:
    """关窗后不能再排 after（销毁后 Tcl 会报 invalid command name）。"""
    instance = _bare_app()
    instance._closing = True
    instance._handle_result = lambda _item: None
    instance._poll_results()
    assert instance.root.scheduled == 0, "关窗后仍在续接轮询"


# ---------------------------------------------------------------------------
# S4：日志目录兜底自身不能崩
# ---------------------------------------------------------------------------

def test_user_log_dir_falls_back_when_localappdata_unwritable() -> None:
    """%LOCALAPPDATA% 不可写时降级到临时目录，绝不抛（它是崩溃兜底的一环）。"""
    original = os.environ.get("LOCALAPPDATA")
    try:
        with tempfile.TemporaryDirectory() as tmp:
            blocker = os.path.join(tmp, "iam-a-file")
            with open(blocker, "w", encoding="utf-8") as handle:
                handle.write("x")
            # 把 base 指到一个**文件**：makedirs 会抛 NotADirectoryError（OSError 子类）
            os.environ["LOCALAPPDATA"] = blocker
            path = entry.user_log_dir()
            assert path, "必须返回一个可用路径"
            assert blocker not in path, f"没有降级，仍指向不可写位置：{path}"
    finally:
        if original is None:
            os.environ.pop("LOCALAPPDATA", None)
        else:
            os.environ["LOCALAPPDATA"] = original


def test_user_log_dir_normal_path_still_works() -> None:
    """正常环境下仍写 %LOCALAPPDATA%（降级不能变成默认路径）。"""
    with tempfile.TemporaryDirectory() as tmp:
        original = os.environ.get("LOCALAPPDATA")
        os.environ["LOCALAPPDATA"] = tmp
        try:
            path = entry.user_log_dir()
            assert path.startswith(tmp), f"正常环境不该降级：{path}"
            assert os.path.isdir(path)
        finally:
            if original is None:
                os.environ.pop("LOCALAPPDATA", None)
            else:
                os.environ["LOCALAPPDATA"] = original


# ---------------------------------------------------------------------------
# S1：明文清理要全量兜错
# ---------------------------------------------------------------------------

def test_release_all_plain_continues_after_one_failure() -> None:
    """一个明文删不掉，其余仍要删 —— 不能因为第一个失败就全都留下。"""
    instance = ui_app.App.__new__(ui_app.App)
    released: List[str] = []

    import wm.dlp as dlp
    original_release = dlp.release

    def _fake_release(path):
        if path.endswith("bad"):
            raise OSError("占用中")
        released.append(path)

    dlp.release = _fake_release
    try:
        instance._plain = {"a": "plain-a", "b": "plain-bad", "c": "plain-c"}
        instance._release_all_plain()
    finally:
        dlp.release = original_release

    assert "plain-a" in released and "plain-c" in released, \
        f"第一个失败后其余明文没被清理：{released}"
    assert not instance._plain_map(), "清理后映射表应为空"


__all__ = [name for name in dir() if name.startswith("test_")]
