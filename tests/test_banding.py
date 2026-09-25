from __future__ import annotations
import os
import sys
import numpy as np
from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from wm import render
from wm.spec import WatermarkSpec


def _rand_img(w, h):
    return Image.fromarray(np.random.randint(0, 255, (h, w, 3), dtype=np.uint8)).convert("RGB")


def test_banded_matches_nonbanded_pixel_identical():
    """强制走带路径（factor==1）应与整层 alpha_composite 逐像素一致。

    注：原设计稿写 ``IMAGE_SS_MAX_PIXELS = 10**12`` 想 "让 factor 永远 == 1"，但
    ``factor = IMAGE_RENDER_SCALE if pixels <= IMAGE_SS_MAX_PIXELS else 1.0``，
    10**12 反而让一切小图 factor==2（非带路径）。要真正强制 factor==1 必须把阈值压到
    低于图像像素数（此处取 0），并且 banded / reference 必须用**同一张底图**，否则两份
    随机底图会污染像素级对比。
    """
    saved_ss = render.IMAGE_SS_MAX_PIXELS
    saved_band = render.IMAGE_BAND_PIXELS
    try:
        render.IMAGE_SS_MAX_PIXELS = 0      # pixels > 0 → factor 永远 == 1（走带路径）
        render.IMAGE_BAND_PIXELS = 0        # 带路径永远被选中
        spec = WatermarkSpec(font_pct=4.0, margin_pct=3.0, angle=30.0).normalized()
        img = _rand_img(900, 900)           # 同一张底图，否则随机底图会污染对比
        banded = render.render_image(img, spec, scale=1.0)

        render.IMAGE_BAND_PIXELS = 10**12   # 走非带路径
        reference = render.render_image(img, spec, scale=1.0)

        assert np.array_equal(np.asarray(banded), np.asarray(reference)), (
            "带路径与整层合成结果不一致：像素级不等价")
    finally:
        render.IMAGE_SS_MAX_PIXELS = saved_ss
        render.IMAGE_BAND_PIXELS = saved_band


def test_banded_for_large_image_correct():
    """大图（factor==1）带路径应与非带路径逐像素一致，且输出尺寸/模式正确。"""
    saved_ss = render.IMAGE_SS_MAX_PIXELS
    saved_band = render.IMAGE_BAND_PIXELS
    try:
        render.IMAGE_SS_MAX_PIXELS = 0      # factor == 1
        render.IMAGE_BAND_PIXELS = 2000      # 大图必走带路径
        spec = WatermarkSpec(font_pct=4.0, margin_pct=3.0, angle=30.0).normalized()
        img = _rand_img(2000, 1500)         # banded / reference 用同一张底图
        out = render.render_image(img, spec, scale=1.0)

        assert out.size == (2000, 1500), f"输出尺寸错误：{out.size}"
        assert out.mode == "RGBA", f"输出模式错误：{out.mode}"

        render.IMAGE_BAND_PIXELS = 10**12    # 非带路径参考
        ref = render.render_image(img, spec, scale=1.0)

        assert np.array_equal(np.asarray(out), np.asarray(ref)), (
            "大图带路径结果与整层合成不一致：像素级不等价")
    finally:
        render.IMAGE_SS_MAX_PIXELS = saved_ss
        render.IMAGE_BAND_PIXELS = saved_band


def test_banded_cancels_between_bands():
    """带路径在**带与带之间**应响应取消回调。

    两个容易搞错的点（第一版就写错了，特此写明）：

    1. ``IMAGE_BAND_PIXELS`` 只决定「是否走带路径」，**每带多少行由
       ``IMAGE_BAND_ROWS`` 决定**。只把前者压到 1 并不会产生"极多极小的带" ——
       1500px 图仍只有 ``ceil(1500/1024) = 2`` 带，带循环里最多检查 2 次，
       取消其实是在准备阶段触发的，"带之间"根本没被验证到。必须同时压
       ``IMAGE_BAND_ROWS``。
    2. 取消阈值要**绕过准备阶段**的检查点：``render_image`` 进入时 + convert 后
       各 1 次，``_prepare_layer`` 开头 + 块位图渲染后各 1 次 —— 共 4 次。
       阈值取 8，第 5~7 次落在带 1~3（放行，带被真正合成），第 8 次在带 4 前
       抛出，以此证明"带与带之间能中断"（而不是进带循环之前就中断）。
    """
    saved_ss = render.IMAGE_SS_MAX_PIXELS
    saved_band = render.IMAGE_BAND_PIXELS
    saved_rows = render.IMAGE_BAND_ROWS
    try:
        render.IMAGE_SS_MAX_PIXELS = 0      # factor == 1（带路径前提）
        render.IMAGE_BAND_PIXELS = 0        # 带路径永远被选中
        render.IMAGE_BAND_ROWS = 1          # 每行一带 → 900 次带循环
        spec = WatermarkSpec(font_pct=4.0, margin_pct=3.0, angle=30.0).normalized()
        counter = {"n": 0}
        is_cancelled = lambda: (counter.__setitem__("n", counter["n"] + 1) or counter["n"] >= 8)

        raised = False
        try:
            render.render_image(_rand_img(900, 900), spec, scale=1.0,
                                is_cancelled=is_cancelled)
        except render.Cancelled:
            raised = True

        assert raised, "带路径未响应取消回调（应抛 Cancelled）"
        # 4 次准备 + 至少 1 次带循环 = 取消必须发生在带循环里，否则计数到不了 5
        assert counter["n"] >= 5, f"取消发生在进带循环之前（只检查了 {counter['n']} 次）"
        assert counter["n"] == 8, f"应在第 8 次检查时中断，实际 {counter['n']}"
    finally:
        render.IMAGE_SS_MAX_PIXELS = saved_ss
        render.IMAGE_BAND_PIXELS = saved_band
        render.IMAGE_BAND_ROWS = saved_rows
