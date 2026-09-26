"""按**字节预算**限流、线程安全的 LRU 缓存（OrderedDict + RLock）。

为什么需要一个类，而不是「字典 + trim 函数」：

1. **并发安全**（审计 S2）。预览线程与批处理线程会**同时**读写同一个缓存：
   一个在 ``get`` 命中旧值，另一个正 trim 到一半 ``del`` 掉它 —— 旧实现
   ``next(iter(d.items()))`` 取到已被别人删掉的键就会 ``KeyError``，且这种失败
   随机出现、几乎无法复现。本类所有操作都在同一把 :class:`threading.RLock` 下，
   ``get`` 与 trim 不可能交错。

2. **真 LRU**（旧实现是 FIFO）。``get`` 命中会把该键移到队尾，于是「常用的一页」
   不会被「后面来的一批一次性尺寸」挤掉 —— 对本项目尤其重要：PDF 图层缓存的键
   含页面尺寸，扫描件每差 1pt 就是一条，FIFO 会让正在被反复复用的那条被冲走。

3. **预算可按 Callable 动态求值**。测试与诊断会把
   ``render.PDF_LAYER_CACHE_BYTES`` 压小以验证裁剪逻辑；预算若在建实例时被固化，
   这种写法就失效了。故 budget 允许是「无参函数」。

代价（**不是**“免费”的真 LRU）：命中一次就要 move_to_end + 持锁，比裸 dict 慢。
但这里的 value 是几百 KB 到上百 MB 的位图 / Pixmap，锁与指针移动的开销（微秒级）
相对对象本身的读写可以忽略。

单条超限的元素**不入缓存**（ ``min_keep`` 只保护"至少留住最新一条"，不保护超限项）：
一张 A0 图层就 122MB，放进去会立刻把别人全清光，然后自己又被判超限 —— 反复重渲染。
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from typing import Callable, Generic, Iterator, Optional, Tuple, TypeVar

Key = TypeVar("Key")
Value = TypeVar("Value")

#: 预算类型：固定字节数，或「每次需要时才求值」的无参函数（见模块 docstring 第 3 点）
Budget = int


class SizedLRU(Generic[Key, Value]):
    """带**字节预算**与互斥锁的 LRU 缓存。

    :param cost: 计算单个 value 字节数的函数（>0）；
    :param budget: 预算上限（字节），或返回它的无参函数；
    :param min_keep: 裁剪时至少保留的**最新**条目数。默认 1 —— 渲染路径是
        「先入缓存、再用」的同步流程，最新那条正在被使用，**不能**被裁掉。

    用法：::

        cache: SizedLRU[tuple, Image.Image] = SizedLRU(cost, lambda: _BYTES)
        hit = cache.get(key)               # None 表示未命中
        cache.set(key, value)              # 超限项自动不入缓存
        len(cache)                         # 条目数（线程安全）
    """

    def __init__(
        self,
        cost: Callable[[Value], int],
        budget: "int | Callable[[], int]",
        min_keep: int = 1,
    ) -> None:
        """构造缓存。**不在此时把 budget 求值**（可以传无参函数使其动态变化）。"""

        self._cost = cost
        self._budget = budget
        self._min_keep = max(0, int(min_keep))
        self._lock = threading.RLock()
        self._data: "OrderedDict[Key, Value]" = OrderedDict()
        self._bytes = 0

    # -- 基本查询 ---------------------------------------------------------

    def __len__(self) -> int:
        """条目数（持锁读取，不会读到正在被裁剪的中间态）。"""
        with self._lock:
            return len(self._data)

    def __contains__(self, key: Key) -> bool:
        """``key in cache`` —— 注意它**不**刷新 LRU 顺序（查询不该改变淘汰顺序）。"""
        with self._lock:
            return key in self._data

    def __iter__(self) -> Iterator[Key]:
        """按「最旧 -> 最新」迭代（快照，迭代期间不持锁）。"""
        with self._lock:
            return iter(list(self._data))

    def values(self) -> "list[Value]":
        """「最旧 -> 最新」的值快照（内部 dict 视图会让调用方绕过锁，故显式拷出）。"""
        with self._lock:
            return list(self._data.values())

    def items(self) -> "list[Tuple[Key, Value]]":
        """「最旧 -> 最新」的键值快照（拷出来，避免外部绕过锁直接改内部表）。"""
        with self._lock:
            return list(self._data.items())

    def clear(self) -> None:
        """清空全部条目并把字节账目归零（测试 / 切换文档时用）。"""
        with self._lock:
            self._data.clear()
            self._bytes = 0

    @property
    def bytes(self) -> int:
        """当前驻留字节数（= 所有条目 ``cost`` 之和）。"""
        with self._lock:
            return self._bytes

    @property
    def budget(self) -> int:
        """当前预算（budget 是函数时现算）。"""
        return int(self._budget() if callable(self._budget) else self._budget)

    # -- 读写 -------------------------------------------------------------

    def get(self, key: Key, default: Optional[Value] = None) -> Optional[Value]:
        """取值；命中则把它移到队尾（**真 LRU** 的关键一步）。"""
        with self._lock:
            if key not in self._data:
                return default
            value = self._data.pop(key)
            self._data[key] = value
            return value

    def set(self, key: Key, value: Value) -> bool:
        """放入一条并裁剪。**单条就超预算时不入缓存**，返回是否真的缓存了。"""
        cost = max(0, int(self._cost(value)))
        budget = self.budget
        if cost > budget:
            return False
        with self._lock:
            if key in self._data:  # 覆盖同键：先把旧计量扣掉，避免重复累计
                self._bytes -= max(0, int(self._cost(self._data[key])))
                del self._data[key]
            self._data[key] = value
            self._bytes += cost
            self._trim_locked()
        return True

    # -- 裁剪 -------------------------------------------------------------

    def trim(self) -> None:
        """把总字节压回预算（公开入口，供想在外部显式触发裁剪的调用方使用）。"""
        with self._lock:
            self._trim_locked()

    def _trim_locked(self) -> None:
        """调用方**必须**已持锁。淘汰最旧的，直到落回预算。

        ``min_keep`` 保留最新的若干条：渲染路径「先入缓存、再用」,最新那条可能
        正处于被使用状态，裁它会让持有它的调用方拿到已释放的对象。
        """
        while self._bytes > self.budget and len(self._data) > self._min_keep:
            _key, oldest = self._data.popitem(last=False)
            self._bytes -= max(0, int(self._cost(oldest)))
        if self._bytes < 0:  # cost 函数在对象生命周期内变化的兜底（不该发生）
            self._bytes = 0


__all__ = ["SizedLRU"]
