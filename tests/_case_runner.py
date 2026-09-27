"""单条用例的**子进程入口**——在干净解释器 + 主线程里跑一个用例。

被 ``tests/run_all.py`` 调用（每条用例一次），本身不是测试用例：文件名不以
``test_`` 开头，不会被运行器发现。

为什么要独立子进程（详见 AUDIT.md 与 run_all.py 的历史注释）：
    Tk 解释器**绑定在创建它的线程**上。此前用例跑在子线程里，前序 GUI 用例销毁
    root 后遗留的 ``tkinter.font.Font`` / ``Variable`` 一旦被 GC 回收，其
    ``__del__`` 会跨线程调用 Tcl（``font delete``）→ Tcl 永久阻塞 → 看门狗判超时；
    表现是「单独跑必过、完整回归里偶尔挂」，且挂哪一条会随导入时机漂移。
    放进子进程后：Tk 的创建与销毁都在**主线程**，进程一退出 Tcl 随之释放，
    没有任何跨线程残留。

退出码约定（主运行器据此判定三态）：
    0 —— 通过
    1 —— 失败（traceback 打到 stderr，由主运行器在汇总里展示）
    2 —— 跳过（stderr 末尾一行 ``__SKIP__:<原因>``）
    3 —— 用法错误

stdout 不捕获（继承父进程），用例的正常打印照常实时显示；stderr 由父进程捕获，
这样 Tk 那类「Exception ignored ...」噪音只在用例真失败时才被翻出来。
"""

from __future__ import annotations

import importlib.util
import os
import sys
import traceback
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# 复用运行器里的预导入清单与跳过标记（单一真源，避免两边漂移）
from run_all import SKIP_MARK, _preload_heavy_modules  # noqa: E402


def _load_case(path: str):
    """按文件路径加载测试模块并返回目标函数。"""
    spec = importlib.util.spec_from_file_location("case", path)
    if spec is None or spec.loader is None:
        raise ImportError("无法加载测试模块：%s" % path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["case"] = module
    spec.loader.exec_module(module)
    return module


def main(argv: list) -> int:
    if len(argv) != 3:
        print("usage: _case_runner.py <test_file.py> <case_name>", file=sys.stderr)
        return 3
    path, name = argv[1], argv[2]
    try:
        # 主线程、且此刻还没有任何 Tk 残留 —— 预导入重量级扩展，消除
        # 「首次导入触发 GC -> 回收 Tk 对象 -> 跨线程 __del__」这条路径
        _preload_heavy_modules()
        module = _load_case(path)
        func = getattr(module, name)
        func()
    except unittest.SkipTest as exc:  # 依赖缺失：零断言，必须如实上报
        print("%s%s" % (SKIP_MARK, str(exc) or "（未给出原因）"), file=sys.stderr)
        return 2
    except BaseException:  # noqa: BLE001 —— 任何异常都算失败，交给父进程汇总
        traceback.print_exc()
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
