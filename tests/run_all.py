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

import importlib.util
import os
import subprocess
import sys
import traceback
from typing import Callable, List, Tuple

#: 单个用例超时（秒）。用例跑在**子进程**里，超时由 ``subprocess`` 直接终止进程，
#: 不会留下半死的线程继续改全局状态。
TEST_TIMEOUT = 30.0

#: 子进程的跳过标记：``_case_runner.py`` 把 SkipTest 的原因写到 stderr 的这一行，
#: 主运行器据此区分「跳过」与「失败」（两者退出码不同，但原因要走 stderr 传回）。
SKIP_MARK = "__SKIP__:"

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
    # 注意：``import fitz`` 自 PyMuPDF 1.28 起已弃用（将来会 ImportError），
    # 项目代码统一用 ``import pymupdf as fitz``；这里的预导入也必须用新名，
    # 否则 import fitz 只是把一个待废弃的兼容层拉进来，预热不到真正的模块。
    "pymupdf",
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


def _run_case(path: str, name: str) -> Tuple[str, str]:
    """在**独立子进程**里跑一条用例，返回 ``(状态, 详情)``。

    状态取值：``ok`` / ``skip`` / ``fail`` / ``timeout``。

    这是本运行器的**默认执行方式**（早期是在子线程里跑、失败后再用子进程复跑
    一次兜底）。彻底隔离后带来三个好处：

    1. Tk 的创建与销毁都在子进程的**主线程**，进程一退 Tcl 随之释放 ——
       不再有「销毁 root 后遗留对象被别的线程 GC，``__del__`` 跨线程调用 Tcl
       导致永久阻塞」这条路径，也就不再需要 ``gc.disable()`` 去规避它；
    2. 超时能**真正杀掉**进程（以前只能放弃等待，线程仍在后台跑，它改过的
       模块常量会污染后续用例）；
    3. 用例之间的模块级状态天然隔离，用例内怎么 monkeypatch 都互不影响。

    代价是每条用例多一次解释器启动（约 1~2 秒），以及超时时拿不到卡住的调用栈
    （进程已被终止）—— 前者可接受，后者由「干净解释器里仍超时」这一事实本身
    提供足够信息。

    stdout 继承父进程（用例打印照常显示），stderr 捕获（Tk 那类噪音只在失败时
    才翻出来），跳过原因通过 stderr 里的 ``SKIP_MARK`` 行传回。
    """
    runner = os.path.join(HERE, "_case_runner.py")
    try:
        proc = subprocess.run(
            [sys.executable, runner, path, name],
            timeout=TEST_TIMEOUT,
            stdout=None,              # 继承：用例自身的打印照常显示
            stderr=subprocess.PIPE,   # 捕获：噪音与 traceback 按需展示
            cwd=os.getcwd(),
        )
    except subprocess.TimeoutExpired as exc:
        detail = "测试运行超过 %d 秒（超时，子进程已终止）" % int(TEST_TIMEOUT)
        tail = (exc.stderr or b"").decode("utf-8", "replace").strip()
        if tail:
            detail += "\n" + tail[-2000:]
        return ("timeout", detail)
    except Exception as exc:  # 起不来进程等：按失败处理，绝不静默
        return ("fail", "无法启动用例子进程：%r" % (exc,))

    err = (proc.stderr or b"").decode("utf-8", "replace")
    if proc.returncode == 0:
        return ("ok", "")
    if proc.returncode == 2:
        reason = "（未给出原因）"
        for line in err.splitlines():
            if line.startswith(SKIP_MARK):
                reason = line[len(SKIP_MARK):] or reason
                break
        return ("skip", reason)
    return ("fail", err.strip() or "子进程以退出码 %d 结束（无输出）" % proc.returncode)


def main() -> int:
    """执行全部测试，返回退出码。"""
    files = _discover()
    if not files:
        print("[FAIL] 未发现任何 test_*.py")
        return 1

    # 注意：这里**不再**调用 ``gc.disable()``。
    #
    # 以前必须关掉自动 GC（详见 AUDIT.md）：用例跑在子线程里，而 Tk 解释器绑定在
    # 创建它的线程上，前序 GUI 用例销毁 root 后遗留的 ``tkinter.font.Font`` 一旦
    # 被 GC 回收，其 ``__del__`` 会跨线程调用 Tcl → Tcl 永久阻塞 → 看门狗判超时，
    # 表现为"单独跑 0.01s，完整回归里必挂"。关掉自动 GC 只是**规避**那条路径。
    #
    # 现在每条用例都在独立子进程的主线程里跑（:func:`_run_case`），Tk 的创建与
    # 销毁同线程、进程退出即释放，跨线程 ``__del__`` 不复存在 —— 规避手段可以撤掉，
    # 让 GC 回归默认行为，反而更接近真实运行环境。
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

            # 用例一律跑在独立子进程里（子进程内部是主线程、干净解释器）。
            # 主进程只负责调度与汇总，自己不再执行任何用例代码，因此也不再需要
            # 复原被污染的模块常量 —— 下面的调用只是兜底（若将来回退到进程内执行）。
            _restore_globals(baseline)
            status, detail = _run_case(path, name)
            if status == "ok":
                passed += 1
                print(f"[OK]   {label}")
            elif status == "skip":
                skipped.append((label, detail))
                print(f"[SKIP] {label}")
            elif status == "timeout":
                failures.append((label, detail))
                print(f"[FAIL] {label} (超时 {int(TEST_TIMEOUT)}s)")
            else:
                failures.append((label, detail))
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
