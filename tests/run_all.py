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
import sys
import threading
import traceback
import unittest
from typing import Callable, List, Tuple

#: 单个用例超时（秒）。Windows 下不能用 signal 定时器（非主线程不安全），
#: 故用 worker 线程 + join(timeout) 实现看门狗；超时即记为失败，不阻塞后续。
TEST_TIMEOUT = 30.0

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


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


def main() -> int:
    """执行全部测试，返回退出码。"""
    files = _discover()
    if not files:
        print("[FAIL] 未发现任何 test_*.py")
        return 1

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
                # 仍活着 = 超时：记为失败，但残留线程会在进程退出时随 daemon 终结。
                failures.append((label, "测试运行超过 %d 秒（超时）" % int(TEST_TIMEOUT)))
                print(f"[FAIL] {label} (超时 30s)")
                continue
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
