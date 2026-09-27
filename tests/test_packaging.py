# -*- coding: utf-8 -*-
"""``packaging/build.py`` 的历史产物清理逻辑（按**轮次**而非条目计数）。

这条线的 bug 有一种很讨厌的形态：**不报错，只是悄悄多占磁盘**。
一轮构建会把多个产物挪进 ``build/_obsolete``（单文件版 = exe + pyi-one 中间目录），
而早先 ``keep`` 数的是"条目"，于是稳态随产物个数漂移 —— 曾经不知不觉锁死在
136 MB。下面几条钉住"一轮多项时也只保留 keep 轮"这个契约，以及 2026-09-27
引入的同轮时间戳 :data:`_STAMP`。
"""
import os
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "packaging"))

import build as B  # noqa: E402


def _round_keys(obsolete: str):
    """返回 [(该轮条目数, 排序位)]，用于断言分组结果。"""
    B.OBSOLETE = obsolete
    return [(len(paths), i) for i, (_, paths) in enumerate(B._obsolete_rounds())]


def test_rounds_group_entries_by_shared_timestamp() -> None:
    """同一时间戳后缀的多个产物算**一轮**，而不是多个独立条目。"""
    with tempfile.TemporaryDirectory() as tmp:
        for name in ("WatermarkTool.exe_090000", "pyi-one_090000",
                     "WatermarkTool.exe_100000", "pyi-one_100000"):
            open(os.path.join(tmp, name), "w").close()
        saved = B.OBSOLETE
        try:
            B.OBSOLETE = tmp
            rounds = B._obsolete_rounds()
        finally:
            B.OBSOLETE = saved

        assert len(rounds) == 2, "4 个条目应归为 2 轮，实际 %d" % len(rounds)
        assert sorted(len(p) for _, p in rounds) == [2, 2], rounds
        # 升序返回，且每轮内的名字共享时间戳
        for _, paths in rounds:
            stamps = {os.path.basename(p).rsplit("_", 1)[1] for p in paths}
            assert len(stamps) == 1, "同一轮内的时间戳应相同: %s" % paths


def test_entries_without_timestamp_never_collapse() -> None:
    """认不出时间戳的历史条目各自成组 —— 不会因解析失败被合成一轮而整批误删。"""
    with tempfile.TemporaryDirectory() as tmp:
        for name in ("legacy_dir", "another_old_thing"):
            open(os.path.join(tmp, name), "w").close()
        saved = B.OBSOLETE
        try:
            B.OBSOLETE = tmp
            rounds = B._obsolete_rounds()
        finally:
            B.OBSOLETE = saved
        assert len(rounds) == 2, rounds


def test_prune_keeps_rounds_not_entries() -> None:
    """核心契约：keep=2 保留**最近 2 轮**，与一轮产出几项无关。

    旧实现按条目计数时，一轮占掉多个名额，稳态会随产物个数漂移。
    """
    with tempfile.TemporaryDirectory() as tmp:
        deleted = []

        def _fake_remove(path: str) -> None:
            deleted.append(os.path.basename(path))
            p = os.path.join(tmp, path)
            if os.path.isfile(p):
                os.remove(p)

        saved_ob, saved_rm = B.OBSOLETE, B._rmtree_guarded
        try:
            B.OBSOLETE = tmp
            B._rmtree_guarded = _fake_remove

            # 构造 3 轮，其中一轮产出 3 项（模拟将来 move_aside 变多）
            waves = [
                ["WatermarkTool.exe_090000", "pyi-one_090000"],
                ["WatermarkTool.exe_100000", "pyi-one_100000"],
                ["WatermarkTool.exe_110000", "pyi-one_110000", "extra_110000"],
            ]
            for wave in waves:
                for name in wave:
                    open(os.path.join(tmp, name), "w").close()
                os.utime(os.path.join(tmp, wave[0]),
                         (time.time() + waves.index(wave), time.time() + waves.index(wave)))

            deleted.clear()
            B.prune_obsolete(keep=2)

            assert sorted(deleted) == sorted(waves[0]), deleted  # 只删最旧的一整轮
            assert len(B._obsolete_rounds()) == 2, "应剩 2 轮"
        finally:
            B.OBSOLETE = saved_ob
            B._rmtree_guarded = saved_rm


def test_prune_is_noop_when_under_limit() -> None:
    """不足 keep 轮时一个都不删，且不抛异常。"""
    with tempfile.TemporaryDirectory() as tmp:
        open(os.path.join(tmp, "WatermarkTool.exe_090000"), "w").close()
        deleted = []
        saved_ob, saved_rm = B.OBSOLETE, B._rmtree_guarded
        try:
            B.OBSOLETE = tmp
            B._rmtree_guarded = lambda p: deleted.append(p)
            B.prune_obsolete(keep=2)
        finally:
            B.OBSOLETE = saved_ob
            B._rmtree_guarded = saved_rm
        assert deleted == [], deleted


def test_move_aside_uses_one_shared_stamp_per_run() -> None:
    """同一进程内的多个 move_aside 共用同一枚时间戳 —— 否则整轮无法归组。"""
    with tempfile.TemporaryDirectory() as tmp:
        obs = os.path.join(tmp, "_obsolete")
        for name in ("a.exe", "b_pyi"):
            open(os.path.join(tmp, name), "w").close()
        saved = B.OBSOLETE
        try:
            B.OBSOLETE = obs
            B.move_aside(os.path.join(tmp, "a.exe"))
            B.move_aside(os.path.join(tmp, "b_pyi"))
            names = sorted(os.listdir(obs))
        finally:
            B.OBSOLETE = saved

        assert len(names) == 2, names
        stamps = {n.rsplit("_", 1)[1] for n in names}
        assert len(stamps) == 1, "同轮的两个产物必须共用一枚时间戳，实际 %s" % names
        assert stamps == {B._STAMP}, stamps
