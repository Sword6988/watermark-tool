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
    """带路径在带与带之间应响应取消回调。"""
    saved_ss = render.IMAGE_SS_MAX_PIXELS
    saved_band = render.IMAGE_BAND_PIXELS
    try:
        render.IMAGE_SS_MAX_PIXELS = 0      # factor == 1（带路径前提）
        render.IMAGE_BAND_PIXELS = 1         # 极多极小的带 → 多次进入循环
        spec = WatermarkSpec(font_pct=4.0, margin_pct=3.0, angle=30.0).normalized()
        counter = {"n": 0}
        is_cancelled = lambda: (counter.__setitem__("n", counter["n"] + 1) or counter["n"] >= 3)

        raised = False
        try:
            render.render_image(_rand_img(1500, 1500), spec, scale=1.0,
                                 is_cancelled=is_cancelled)
        except render.Cancelled:
            raised = True

        assert raised, "带路径未响应取消回调（应抛 Cancelled）"
        assert counter["n"] >= 1, "取消回调从未被调用"
    finally:
        render.IMAGE_SS_MAX_PIXELS = saved_ss
        render.IMAGE_BAND_PIXELS = saved_band
