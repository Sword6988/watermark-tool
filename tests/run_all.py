"""不依赖 pytest 的测试运行器。

自动发现并执行本目录下所有 ``test_*.py`` 里以 ``test_`` 开头的函数，
打印 ``[OK]`` / ``[SKIP]`` / ``[FAIL]`` 统计，失败则以非 0 退出码结束。

用法（在 watermark-tool/ 目录下）：
    "<系统 Python>" tests/run_all.py

结果三态（关键：绝不能让「零断言」被算成通过）：
    * ``[OK]``   —— 用例跑完且断言全成立；
    * ``[SKIP]`` —— 用例主动抛 ``unittest.SkipTest``（依赖缺失 / 无显示环境）。
      此时**没有任何断言被执行**，必须在汇总里单独报出来，否则换一台没装
      ``tkinterdnd2`` 的机器就会显示「全绿」的**虚假绿灯**；
    * ``[FAIL]`` —— 抛异常。

跳过协议用标准库的 ``unittest.SkipTest``：pytest / unittest 也认这个异常，
将来真装上 pytest 可以直接复用同一批用例。

注意：Windows 中文控制台是 GBK，输出不用 ✓ / ✗ / emoji，只用 [OK] / [FAIL]。
"""

from __future__ import annotations

import gc
import importlib.util
import os
import sys
import threading
import traceback
import unittest
from typing import Callable, List, Tuple

#: 单个用例超时（秒）。Windows 下不能用 signal 定时器（非主线程不安全），
#: 故用 worker 线程 + join(timeout) 实现看门狗；超时即记为失败，不阻塞后续。
TEST_TIMEOUT = 30.0

#: 超时后再给的宽限（秒）。超时的用例线程仍活着时，它的 ``finally`` 还没跑；
#: 多等一会儿让它自己收尾（恢复它改过的模块常量），比我们硬恢复更干净。
GRACE_AFTER_TIMEOUT = 5.0

#: 用例之间必须复原的模块级常量（**看门狗超时会让用例的 finally 永远不执行**，
#: 这些全局就会被永久改写，污染后面所有用例 —— 实测过：test_banding 超时把
#: ``IMAGE_SS_MAX_PIXELS`` 留在 0、``IMAGE_BAND_PIXELS`` 留在 1，随后
#: ``test_qa_image_and_pdf_paths_agree_multi`` 因图片路径不再超采样而误报
#: 「两路径偏差 2.5」）。故在**每个用例开始前**兜底复原到模块导入时的基线。
GUARDED_GLOBALS = {
    "wm.render": ("IMAGE_RENDER_SCALE", "IMAGE_SS_MAX_PIXELS",
                  "IMAGE_BAND_PIXELS", "IMAGE_BAND_ROWS",
                  "PDF_LAYER_CACHE_BYTES"),
}

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def _snapshot_globals() -> dict:
    """记录 ``GUARDED_GLOBALS`` 里各模块常量的当前值。"""
    snap: dict = {}
    for mod_name, names in GUARDED_GLOBALS.items():
        module = sys.modules.get(mod_name)
        if module is None:
            continue
        snap[mod_name] = {n: getattr(module, n) for n in names if hasattr(module, n)}
    return snap


def _restore_globals(snap: dict) -> None:
    """把常量复原到快照值；模块尚未导入则跳过（下轮自然会被快照到）。"""
    for mod_name, kv in snap.items():
        module = sys.modules.get(mod_name)
        if module is None:
            continue
        for name, value in kv.items():
            setattr(module, name, value)


def _load_module(path: str):
    """按文件路径加载模块。"""
    name = os.path.splitext(os.path.basename(path))[0]
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"无法加载测试模块：{path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _discover() -> List[str]:
    """返回所有 test_*.py 的绝对路径。"""
    return sorted(
        os.path.join(HERE, name)
        for name in os.listdir(HERE)
        if name.startswith("test_") and name.endswith(".py")
    )


#: 需要在主线程预导入的重量级扩展模块（见 :func:`_preload_heavy_modules`）。
PRELOAD_MODULES = (
    "numpy", "numpy.random", "numpy.linalg",
    "PIL", "PIL.Image", "PIL.ImageDraw", "PIL.ImageFont", "PIL.ImageFilter",
    "fitz",
)


def _preload_heavy_modules() -> None:
    """在主线程、且**还没有任何 Tk 残留**时把重量级扩展模块导入完毕。

    为什么必须做（实测死锁，排查过程见 AUDIT.md）：

    某个用例线程里**首次**导入 ``numpy.random`` 会加载 .pyd、分配大量对象并
    触发 GC；GC 若顺手回收了前序 GUI 用例销毁 root 后遗留的
    ``tkinter.font.Font``，其 ``__del__`` 会跨线程调用 Tcl（``font delete``），
    而 Tcl 解释器是绑定在**创建它的线程**上的 —— 从别的线程调用会**永久阻塞**，
    表现为"用例莫名超时 30s"（实测 ``test_banded_cancels_between_bands``，
    单独跑 0.01s，在完整回归里必挂）。

    预导入把这次"首次导入"提前到主线程、干净的时刻，从而消除触发路径。
    注意这**不治本**：只要还有"子线程里创建并销毁 Tk"，这个风险就存在；
    彻底方案是每个用例跑在独立子进程里（见 AUDIT.md 的后续建议）。
    """
    for name in PRELOAD_MODULES:
        try:
            __import__(name)
        except Exception:
            pass  # 依赖缺失时不管：用例自己会按 SkipTest 或 FAIL 显形


def _baseline_globals() -> dict:
    """模块导入时的原始常量值：先 import 一次，再快照。"""
    _preload_heavy_modules()
    for mod_name in GUARDED_GLOBALS:
        try:
            __import__(mod_name)
        except Exception:
            # 依赖缺失时跳过：等它真正被导入时会在用例循环里补快照
            pass
    return _snapshot_globals()


def main() -> int:
    """执行全部测试，返回退出码。"""
    files = _discover()
    if not files:
        print("[FAIL] 未发现任何 test_*.py")
        return 1

    # 关掉自动 GC：它是「用例莫名超时 30s」的直接触发者。
    #
    # 机理（实测，详见 AUDIT.md）：本运行器把每个用例放在**子线程**里跑，而 Tk
    # 解释器绑定在**创建它的线程**上。前序 GUI 用例销毁 root 后，``tkinter.font.Font``
    # 之类会变成只可被 GC 回收的循环垃圾；一旦 GC 在某个**别的**用例线程里跑起来，
    # 其 ``__del__`` 就会跨线程调用 Tcl（``font delete``）→ Tcl 永久阻塞 →
    # 该用例被看门狗判为超时。表现是"单独跑 0.01s，完整回归里必挂"，且挂哪一条
    # 会随导入时机漂移（修好 A 就挪到 B）。
    #
    # 关掉自动 GC 后，这类对象在本轮内不再被回收（引用计数能清的照清，测试里的
    # 大对象基本都是引用计数回收的），跨线程 ``__del__`` 这条路径就不存在了。
    # 注意这仍属**规避**：彻底方案是让每个用例跑在独立子进程里。
    gc.disable()
    baseline = _baseline_globals()
    passed = 0
    skipped: List[Tuple[str, str]] = []
    failures: List[Tuple[str, str]] = []
    for path in files:
        filename = os.path.basename(path)
        try:
            module = _load_module(path)
        except Exception:
            failures.append((f"{filename}::<import>", traceback.format_exc()))
            print(f"[FAIL] {filename}::<import>")
            continue
        names = sorted(n for n in dir(module) if n.startswith("test_"))
        for name in names:
            func: Callable = getattr(module, name)
            if not callable(func):
                continue
            label = f"{filename}::{name}"

            # 上一个用例（可能被看门狗中断、finally 未执行）留下的污染先清掉
            _restore_globals(baseline)

            # 看门狗：worker 线程跑用例、捕获异常；主线程 join 限时，超时记为失败。
            result: dict = {}

            def _run():
                try:
                    func()
                    result["ok"] = True
                except unittest.SkipTest as exc:  # 依赖缺失：零断言，必须显形
                    result["skip"] = str(exc) or "（未给出原因）"
                except Exception:
                    result["fail"] = traceback.format_exc()

            worker = threading.Thread(target=_run, daemon=True)
            worker.start()
            worker.join(timeout=TEST_TIMEOUT)
            if worker.is_alive():
                # 诊断：超时多为偶发，看结果根本猜不出卡在哪 —— 把卡住线程的
                # 调用栈打出来（sys._current_frames 能拿到**别的**线程的栈）。
                frame = sys._current_frames().get(worker.ident)
                if frame is not None:
                    print("  [超时诊断] 卡在：")
                    for line in traceback.format_stack(frame)[-8:]:
                        print("    " + line.rstrip())
                # 仍活着 = 超时。先给个宽限让它自己跑完 finally（那样常量恢复得
                # 最干净），再兜底复原 —— 否则污染会顺着跑到后面的用例上。
                worker.join(timeout=GRACE_AFTER_TIMEOUT)
                _restore_globals(baseline)
                failures.append((label, "测试运行超过 %d 秒（超时）" % int(TEST_TIMEOUT)))
                print(f"[FAIL] {label} (超时 30s)")
                continue
            _restore_globals(baseline)
            if result.get("ok"):
                passed += 1
                print(f"[OK]   {label}")
            elif "skip" in result:
                skipped.append((label, result["skip"]))
                print(f"[SKIP] {label}")
            else:
                failures.append((label, result.get("fail", "（未知失败）")))
                print(f"[FAIL] {label}")

    total = passed + len(skipped) + len(failures)
    print("-" * 72)
    print(f"总计 {total} | 通过 {passed} | 跳过 {len(skipped)} | 失败 {len(failures)}")
    if skipped:
        # 跳过 = 什么都没验证，把原因列全，避免被当成验收通过的证据
        print("[WARN] 以下用例未执行（零断言），结果不代表功能正常：")
        for label, reason in skipped:
            print(f"  [SKIP] {label} —— {reason}")
    for label, tb in failures:
        print("=" * 72)
        print(label)
        print(tb)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
