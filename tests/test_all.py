"""水印工具测试套件（pytest 风格裸 assert，可直接被 tests/run_all.py 执行）。

覆盖（对应需求 §8）：
    1. 平铺四边落在 margin 处 / 无裁切（多尺寸，含 320×240）
    2. 平铺相邻起点差：整轴均分 + 可见空隙 >= 2*margin
    3. 两路径一致性（图片 vs PDF，595×842）
    4. PDF 透明度（alpha 被保留）
    5. spec 校验 / 夹紧 / 非法值归一化不抛异常
    6. 输出路径不覆盖原文件、同名自动加序号
    7. 界面无「平铺开关 / 位置调节」残留入口（平铺是唯一模式）
    8. 参数回灌一致性（load_spec 后「显示 = 控件 = 真值」）与下拉框样式守卫
    附：判墨探针自证（未加水印的空白画布必须无墨迹）

注：水印**恒为平铺铺满整页**，没有平铺开关与位置偏移参数，因此原先的「单块
居中 / 偏移 / 夹紧」与「平铺 ↔ 位置互斥联动」用例已随能力一并删除。

判墨方法：取全图**中位色**作背景基准，用**逐通道最大偏差 ≥ 阈值**判墨。
**不要**用亮度阈值 —— 低不透明度浅色水印叠加白底后亮度仍很高，会被误判为空白。
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest

import numpy as np
from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import pymupdf as fitz  # noqa: E402  PyMuPDF（``import fitz`` 自 1.28 起已弃用）
from wm import layout, media, render  # noqa: E402
from wm.spec import WatermarkSpec, normalize_color  # noqa: E402

DEV = 10  # 判墨阈值：通道最大偏差
WHITE = (255, 255, 255)


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------

def _canvas(width: int, height: int) -> Image.Image:
    return Image.new("RGB", (width, height), WHITE)


def _ink_mask(img: Image.Image, dev: int = DEV) -> np.ndarray:
    """中位色 + 逐通道最大偏差判墨，返回布尔掩码。"""
    arr = np.asarray(img.convert("RGB")).astype(np.int16)
    bg = np.median(arr.reshape(-1, 3), axis=0)
    return np.abs(arr - bg).max(axis=2) >= dev


def _ink_bbox(img: Image.Image, dev: int = DEV):
    """返回墨迹外接框 ``(x0, y0, x1, y1)``（含端点）；无墨迹返回 None。"""
    mask = _ink_mask(img, dev)
    ys, xs = np.where(mask)
    if len(xs) == 0:
        return None
    return int(xs.min()), int(ys.min()), int(xs.max()), int(ys.max())


def _axis_gaps(starts) -> list:
    """相邻起点差序列。"""
    return [starts[i + 1] - starts[i] for i in range(len(starts) - 1)]


#: 直接调用 axis_starts 时用严格容差；从 placements 反推时因 round(...,6) 去重会
#: 把浮点噪声放大到 ~1e-6，故给 10 倍余量（真实的「间距不均」量级是 px 以上）。
_TOL_DIRECT = 1e-6
_TOL_DERIVED = 1e-5


def _assert_axis_ok(starts, page: float, ink: float, margin: float,
                    tol: float = _TOL_DIRECT) -> None:
    """校验一条轴的起点序列（v3 口径）：

        * 首块墨迹起点 == margin、末块墨迹终点 == page - margin（首尾贴齐）；
        * 相邻起点差**完全相等**（整轴均分）；
        * 起点差 >= ``ink + 2*margin``，且**可见空隙 = 起点差 - ink >= 2*margin**
          （这才是「相邻空隙不小于设定值」的正确判据 —— 旧用例误把起点差当空隙，
          正是它放过了「末列间隙过小 / 墨迹重叠」这个缺陷）。
    """
    assert starts, "起点序列为空"
    if len(starts) == 1:
        # 退化路径（放不下两块 / 墨迹比页面还大）：实现契约是「单块居中」，
        # 此时 margin 无法同时满足，故只断言落点为居中。
        assert abs(starts[0] - (page - ink) / 2.0) <= tol, \
            f"单块退化应居中：{starts[0]} vs {(page - ink) / 2.0}"
        return
    assert abs(starts[0] - margin) <= tol, f"首块未贴左边距线：{starts[0]} vs {margin}"
    assert abs((starts[-1] + ink) - (page - margin)) <= tol, "末块未贴右边距线"
    gaps = _axis_gaps(starts)
    assert max(gaps) - min(gaps) <= tol, f"间距不均：{gaps}"
    assert min(gaps) >= ink + 2.0 * margin - tol, f"起点差 {min(gaps)} < ink+2*margin"
    assert min(gaps) - ink >= 2.0 * margin - tol, f"可见空隙 {min(gaps) - ink} < 2*margin"


def _axis_starts_from_placements(placements, ink, axis: int):
    """从实际平铺坐标反推该轴的「墨迹起点」序列（与实现解耦的交叉验证）。"""
    if axis == 0:
        return sorted({round(px + ink[0], 6) for px, py in placements})
    return sorted({round(py + ink[1], 6) for px, py in placements})


def _render_pdf_page(pdf_path: str, index: int = 0, dpi: int = 72) -> Image.Image:
    doc = fitz.open(pdf_path)
    try:
        page = doc[index]
        pix = page.get_pixmap(dpi=dpi)
        return Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
    finally:
        doc.close()


def _blank_pdf(path: str, width: float, height: float, pages: int = 1) -> None:
    doc = fitz.open()
    for _ in range(pages):
        doc.new_page(width=width, height=height)
    doc.save(path, garbage=4, deflate=True)
    doc.close()


# ---------------------------------------------------------------------------
# 1. 平铺：四边落在 margin 处 / 无裁切
# ---------------------------------------------------------------------------

#: (宽, 高, 字号%, 边距%) —— v3 均分算法下**所有**组合都应四边贴齐
_TILE_CASES = [
    (320, 240, 4.0, 1.0),
    (320, 240, 4.0, 3.0),
    (595, 842, 4.0, 1.0),
    (595, 842, 4.0, 3.0),
    (595, 842, 8.0, 5.0),
    (595, 842, 4.0, 0.0),
    (800, 600, 4.0, 3.0),
    (800, 600, 4.0, 0.0),
    (1240, 1754, 4.0, 3.0),
    (1200, 400, 4.0, 3.0),
    (400, 400, 4.0, 3.0),
    (1000, 300, 4.0, 3.0),
]


def _tile_bbox(width: int, height: int, font_pct: float, margin_pct: float,
               text: str = "机密文件"):
    """渲染平铺水印并返回 (墨迹 bbox, margin 像素)。"""
    spec = WatermarkSpec(text=text, font_pct=font_pct, margin_pct=margin_pct,
                         angle=30.0, opacity=1.0, color="#D32F2F").normalized()
    margin = margin_pct / 100.0 * min(width, height)
    out = render.render_image(_canvas(width, height), spec)
    return _ink_bbox(out), margin


def test_tile_bbox_sits_exactly_at_margin():
    """墨迹 bbox 四边恰好落在距页边 margin 处（容差 ≤ 2.5 px），且严格在页内、无裁切。"""
    for width, height, font_pct, margin_pct in _TILE_CASES:
        bbox, margin = _tile_bbox(width, height, font_pct, margin_pct)
        assert bbox is not None, f"{width}x{height} f={font_pct} m={margin_pct} 未检测到水印"
        x0, y0, x1, y1 = bbox
        assert 0 <= x0 <= x1 <= width - 1, f"{width}x{height} 水平越界 {bbox}"
        assert 0 <= y0 <= y1 <= height - 1, f"{width}x{height} 垂直越界 {bbox}"
        tag = f"{width}x{height} f={font_pct} m={margin_pct}"
        assert abs(x0 - margin) <= 2.5, f"{tag} 左边距偏差 {x0 - margin:+.2f}"
        assert abs(y0 - margin) <= 2.5, f"{tag} 上边距偏差 {y0 - margin:+.2f}"
        assert abs((width - 1 - x1) - margin) <= 2.5, f"{tag} 右边距偏差 {(width - 1 - x1) - margin:+.2f}"
        assert abs((height - 1 - y1) - margin) <= 2.5, f"{tag} 下边距偏差 {(height - 1 - y1) - margin:+.2f}"


def test_tile_no_visible_blank_band_any_size():
    """多尺寸 × 多文本 × 多边距：四边都贴齐边距线，且两轴间距完全均匀。"""
    texts = ["机密文件", "内部资料\n请勿外传", "CONFIDENTIAL"]
    sizes = [(320, 240), (600, 800), (1000, 300), (900, 900), (450, 1200)]
    for width, height in sizes:
        for text in texts:
            for margin_pct in (0.0, 1.0, 3.0, 8.0):
                if margin_pct == 0.0 and text != "机密文件":
                    continue  # 边距 0 已由别处覆盖，避免用例过慢
                bbox, margin = _tile_bbox(width, height, 3.0, margin_pct, text=text)
                assert bbox is not None, f"{width}x{height} {text!r} 无水印"
                x0, y0, x1, y1 = bbox
                assert 0 <= x0 <= x1 <= width - 1
                assert 0 <= y0 <= y1 <= height - 1
                assert abs(x0 - margin) <= 2.5, (width, height, text, margin_pct, x0, margin)
                assert abs(y0 - margin) <= 2.5
                assert abs((width - 1 - x1) - margin) <= 2.5, (width, height, text, margin_pct)
                assert abs((height - 1 - y1) - margin) <= 2.5


def test_tile_geometry_hits_margin_exactly_in_page_coords():
    """纯几何校验：首块墨迹起点 == margin、末块墨迹终点 == page - margin（1e-6），
    且由实际 placements 反推的两轴起点序列满足均分 + 最小可见空隙。"""
    for width, height, font_pct, margin_pct in _TILE_CASES:
        spec = WatermarkSpec(text="机密文件", font_pct=font_pct,
                             margin_pct=margin_pct, angle=30.0, opacity=1.0).normalized()
        margin = margin_pct / 100.0 * min(width, height)
        size = layout.font_px(spec, width, height)
        ((bw, bh), ink) = layout.block_geometry(spec, size)
        ink_w, ink_h = ink[2] - ink[0], ink[3] - ink[1]

        xs = layout.axis_starts(width, ink_w, margin)
        ys = layout.axis_starts(height, ink_h, margin)
        _assert_axis_ok(xs, width, ink_w, margin)
        _assert_axis_ok(ys, height, ink_h, margin)

        placements = layout.compute_placements(width, height, bw, bh, spec, ink)
        _assert_axis_ok(_axis_starts_from_placements(placements, ink, 0), width, ink_w,
                        margin, tol=_TOL_DERIVED)
        _assert_axis_ok(_axis_starts_from_placements(placements, ink, 1), height, ink_h,
                        margin, tol=_TOL_DERIVED)


# ---------------------------------------------------------------------------
# 2. 平铺间距：整轴均分 + 最小可见空隙（v3 口径）
# ---------------------------------------------------------------------------

def test_axis_starts_step_formula():
    """首尾贴齐 + 相邻起点差**完全相等** + 起点差 >= ink+2*margin
    + 可见空隙(起点差-ink) >= 2*margin。"""
    cases = [
        (320, 40, 7.2), (1000, 60, 30), (200, 80, 5), (300, 100, 50),
        (595, 89, 17.85), (842, 63, 17.85), (1200, 60, 24), (150, 20, 1.5),
        # 缺陷复现组合：A4、font=4%、margin=1%（旧算法末两行重叠 44.9 px）
        (842, 63, 5.95), (595, 89, 5.95),
        # 放不下两块 -> 单块居中
        (100, 90, 30), (100, 150, 5),
    ]
    for page, ink, margin in cases:
        starts = layout.axis_starts(page, ink, margin)
        _assert_axis_ok(starts, page, ink, margin)


def test_axis_starts_extra_gap_fallback():
    """``extra_gap`` 回退路径（极端参数触发 MAX_TILES 保护）仍满足均分 + 不重叠。"""
    width, height = 1240, 1754
    spec = WatermarkSpec(text="机密文件", font_pct=0.5, margin_pct=0.0,
                         angle=30.0, opacity=1.0).normalized()
    size = layout.font_px(spec, width, height)
    ((bw, bh), ink) = layout.block_geometry(spec, size)
    ink_w, ink_h = ink[2] - ink[0], ink[3] - ink[1]
    margin = 0.0

    placements = layout.compute_placements(width, height, bw, bh, spec, ink)
    assert 0 < len(placements) <= layout.MAX_TILES, f"块数未收敛：{len(placements)}"

    xs = _axis_starts_from_placements(placements, ink, 0)
    ys = _axis_starts_from_placements(placements, ink, 1)
    # 回退只会拉大间距，故仍应满足：完全均分、起点差 >= ink、可见空隙 >= 2*margin(=0)
    assert max(_axis_gaps(xs)) - min(_axis_gaps(xs)) <= _TOL_DERIVED
    assert max(_axis_gaps(ys)) - min(_axis_gaps(ys)) <= _TOL_DERIVED
    assert min(_axis_gaps(xs)) >= ink_w - _TOL_DERIVED
    assert min(_axis_gaps(ys)) >= ink_h - _TOL_DERIVED
    # 首尾仍贴齐（extra_gap 不改变两端贴齐的性质）
    assert abs(xs[0] - margin) <= _TOL_DERIVED
    assert abs((xs[-1] + ink_w) - (width - margin)) <= _TOL_DERIVED
    assert abs(ys[0] - margin) <= _TOL_DERIVED
    assert abs((ys[-1] + ink_h) - (height - margin)) <= _TOL_DERIVED


def test_tile_adjacent_blocks_never_overlap():
    """反向守卫：扫描参数网格，逐对相邻块的墨迹 bbox **互不相交**
    （x / y 两轴都不得重叠），彻底排除「末列间隙过小 / 墨迹重叠」这类缺陷。"""
    font_pcts = (1.0, 2.0, 4.0, 8.0, 15.0)
    margin_pcts = (0.0, 1.0, 3.0, 5.0)
    pages = ((320, 240), (800, 600), (595, 842), (1240, 1754))
    checked = 0
    for width, height in pages:
        for font_pct in font_pcts:
            for margin_pct in margin_pcts:
                spec = WatermarkSpec(text="机密文件", font_pct=font_pct,
                                     margin_pct=margin_pct, angle=30.0).normalized()
                margin = margin_pct / 100.0 * min(width, height)
                size = layout.font_px(spec, width, height)
                ((bw, bh), ink) = layout.block_geometry(spec, size)
                ink_w, ink_h = ink[2] - ink[0], ink[3] - ink[1]
                placements = layout.compute_placements(width, height, bw, bh, spec, ink)
                assert placements

                xs = _axis_starts_from_placements(placements, ink, 0)
                ys = _axis_starts_from_placements(placements, ink, 1)
                _assert_axis_ok(xs, width, ink_w, margin, tol=_TOL_DERIVED)
                _assert_axis_ok(ys, height, ink_h, margin, tol=_TOL_DERIVED)

                # 同轴相邻墨迹区间不得重叠（起点差 >= 墨迹长度）
                if len(xs) > 1:
                    for a, b in zip(xs, xs[1:]):
                        assert b - a >= ink_w - _TOL_DERIVED, f"水平重叠：{b - a} < {ink_w}"
                if len(ys) > 1:
                    for a, b in zip(ys, ys[1:]):
                        assert b - a >= ink_h - _TOL_DERIVED, f"垂直重叠：{b - a} < {ink_h}"

                # 块数不多时再做一次全对墨迹 bbox 相交检查
                if len(placements) <= 400:
                    rects = [(x + ink[0], y + ink[1], x + ink[2], y + ink[3])
                             for x, y in placements]
                    for i in range(len(rects)):
                        ax0, ay0, ax1, ay1 = rects[i]
                        for j in range(i + 1, len(rects)):
                            bx0, by0, bx1, by1 = rects[j]
                            overlap_x = min(ax1, bx1) - max(ax0, bx0) > 1e-6
                            overlap_y = min(ay1, by1) - max(ay0, by0) > 1e-6
                            assert not (overlap_x and overlap_y), (
                                f"墨迹重叠 {width}x{height} f={font_pct} m={margin_pct}: "
                                f"{rects[i]} ∩ {rects[j]}")
                checked += 1
    assert checked == len(pages) * len(font_pcts) * len(margin_pcts)


def test_tile_positions_match_axis_starts():
    """compute_placements 的平铺坐标 = 两轴起点笛卡尔积 - 墨迹偏移。"""
    width, height = 600, 800
    spec = WatermarkSpec(margin_pct=3.0, font_pct=4.0, angle=30.0).normalized()
    margin = 3.0 / 100.0 * min(width, height)
    size = layout.font_px(spec, width, height)
    ((bw, bh), ink) = layout.block_geometry(spec, size)
    ink_w, ink_h = ink[2] - ink[0], ink[3] - ink[1]
    xs = layout.axis_starts(width, ink_w, margin)
    ys = layout.axis_starts(height, ink_h, margin)
    placements = layout.compute_placements(width, height, bw, bh, spec, ink)
    assert len(placements) == len(xs) * len(ys)
    expected = {(round(x - ink[0], 6), round(y - ink[1], 6)) for y in ys for x in xs}
    got = {(round(px, 6), round(py, 6)) for px, py in placements}
    assert got == expected


# ---------------------------------------------------------------------------
# 3. 两路径一致性（图片 vs PDF，595×842）
# ---------------------------------------------------------------------------

def test_image_and_pdf_paths_agree():
    """同尺寸（595×842）下图片路径与 PDF 路径的水印墨迹 bbox 偏差 <= 2 px。"""
    width, height = 595, 842
    spec = WatermarkSpec(margin_pct=3.0, font_pct=4.0, angle=30.0,
                         opacity=1.0, color="#D32F2F").normalized()

    image_bbox = _ink_bbox(render.render_image(_canvas(width, height), spec))

    with tempfile.TemporaryDirectory() as tmp:
        src = os.path.join(tmp, "blank.pdf")
        dst = os.path.join(tmp, "wm.pdf")
        _blank_pdf(src, width, height)
        render.render_pdf(src, dst, spec)
        blank = np.asarray(_render_pdf_page(src)).astype(np.int16)
        marked = np.asarray(_render_pdf_page(dst)).astype(np.int16)
    mask = np.abs(marked - blank).max(axis=2) >= DEV
    ys, xs = np.where(mask)
    assert len(xs) > 0, "PDF 路径未检测到水印"
    pdf_bbox = (int(xs.min()), int(ys.min()), int(xs.max()), int(ys.max()))

    assert image_bbox is not None
    for a, b in zip(image_bbox, pdf_bbox):
        assert abs(a - b) <= 2, f"两路径 bbox 偏差 {abs(a - b)}：图片 {image_bbox} PDF {pdf_bbox}"


# ---------------------------------------------------------------------------
# 4. PDF 透明度（alpha 保留）
# ---------------------------------------------------------------------------

def test_pdf_alpha_preserved():
    """opacity=0.2 的红色水印：页面角落仍是白（透明保留），
    墨迹像素是被白底稀释的粉红（不是实心红）——证明 alpha 没丢。"""
    width, height = 595, 842
    spec = WatermarkSpec(text="机密文件", margin_pct=5.0, font_pct=6.0,
                         angle=0.0, opacity=0.2, color="#D32F2F").normalized()
    with tempfile.TemporaryDirectory() as tmp:
        src = os.path.join(tmp, "blank.pdf")
        dst = os.path.join(tmp, "wm.pdf")
        _blank_pdf(src, width, height)
        render.render_pdf(src, dst, spec)
        blank = np.asarray(_render_pdf_page(src)).astype(np.int16)
        marked = np.asarray(_render_pdf_page(dst)).astype(np.int16)

    # (a) 角落必须是白的 —— 透明层没有被当成不透明整页覆盖
    corner = marked[3, 3]
    assert int(corner.min()) >= 250, f"角落被覆盖，alpha 丢失：{tuple(corner)}"

    # (b) 墨迹像素必须是「红叠白」的稀释色，不是实心红
    mask = np.abs(marked - blank).max(axis=2) >= 18
    ys, xs = np.where(mask)
    assert len(xs) > 0, "未检测到水印墨迹"
    ink_pixels = marked[mask]
    median = np.median(ink_pixels, axis=0)
    r, g, b = int(median[0]), int(median[1]), int(median[2])
    assert g > 150, f"墨迹过暗（疑似 alpha 丢失变成实心红）：{(r, g, b)}"
    spread = r - g
    assert 5 <= spread <= 120, f"墨迹色不像半透明红叠白底：{(r, g, b)}"


# ---------------------------------------------------------------------------
# 5. spec 校验 / 夹紧 / 归一化
# ---------------------------------------------------------------------------

def test_spec_clamping_never_raises():
    """非法 / 越界输入被归一化，不抛异常。"""
    spec = WatermarkSpec(text="", font_family="  ", font_pct=-5, margin_pct=999,
                         angle=-30, opacity=5, color="not-a-color").normalized()
    assert spec.text.strip()
    assert spec.font_family.strip()
    assert spec.font_pct == 0.5
    assert spec.margin_pct == 20.0
    assert spec.angle == 0.0
    assert spec.opacity == 1.0
    assert spec.color == "#d32f2f"

    assert WatermarkSpec(font_pct="abc").normalized().font_pct == 4.0
    assert WatermarkSpec(opacity=None).normalized().opacity == 0.30
    assert WatermarkSpec(angle=10 ** 9).normalized().angle == 360.0
    assert WatermarkSpec().lines() == ["机密文件"]
    assert WatermarkSpec(text="a\nb").lines() == ["a", "b"]


def test_spec_roundtrip_and_bad_inputs():
    """to_dict / from_dict 往返；from_dict 收到垃圾输入也不崩。

    已删除的 ``tile`` / ``offset_x_pct`` / ``offset_y_pct`` 键出现在旧配置里时
    必须被**静默忽略**（而不是把整份配置判为非法）。
    """
    spec = WatermarkSpec(text="内部", font_pct=7.5, margin_pct=2.5, angle=45,
                         opacity=0.5, color="#1565C0").normalized()
    restored = WatermarkSpec.from_dict(spec.to_dict())
    assert restored == spec
    for junk in (None, [], "x", {"font_pct": "zzz", "unknown": 1}, {}):
        assert isinstance(WatermarkSpec.from_dict(junk), WatermarkSpec)  # type: ignore[arg-type]
    legacy = dict(spec.to_dict(), tile=False, offset_x_pct=12.0, offset_y_pct=-7.0)
    assert WatermarkSpec.from_dict(legacy) == spec


def test_normalize_color_variants():
    """颜色写法归一化。"""
    assert normalize_color("#d32f2f") == "#d32f2f"
    assert normalize_color("#D32F2F") == "#d32f2f"
    assert normalize_color("red") == "#ff0000"
    assert normalize_color(None) == "#d32f2f"
    assert normalize_color("???") == "#d32f2f"


# ---------------------------------------------------------------------------
# 6. 输出路径
# ---------------------------------------------------------------------------

def test_output_path_never_overwrites():
    """输出路径不覆盖原文件；同名自动加序号。"""
    with tempfile.TemporaryDirectory() as tmp:
        src = os.path.join(tmp, "photo.png")
        Image.new("RGB", (20, 20), WHITE).save(src)

        first = media.plan_output(src)
        assert first != src
        assert first.endswith("_watermarked.png")
        assert os.path.dirname(os.path.abspath(first)) == os.path.abspath(tmp)
        assert not os.path.exists(first)

        # 造出该文件后再规划 -> 必须换名
        Image.new("RGB", (20, 20), WHITE).save(first)
        second = media.plan_output(src)
        assert second != first
        assert second.endswith("_watermarked(1).png")
        assert not os.path.exists(second)

        # 指定输出目录
        other = os.path.join(tmp, "out")
        third = media.plan_output(src, out_dir=other, suffix="_wm")
        assert os.path.dirname(os.path.abspath(third)) == os.path.abspath(other)
        assert third.endswith("_wm.png")


def test_kind_detection_and_document():
    """类型识别与 Document 基本能力。"""
    assert media.kind_of("a.PNG") == media.KIND_IMAGE
    assert media.kind_of("a.pdf") == media.KIND_PDF
    assert media.kind_of("a.docx") is None
    assert media.is_supported("x.jpeg") and not media.is_supported("x.txt")

    with tempfile.TemporaryDirectory() as tmp:
        img_path = os.path.join(tmp, "p.jpg")
        Image.new("RGB", (120, 80), WHITE).save(img_path)
        doc = media.Document(img_path)
        try:
            assert doc.kind == media.KIND_IMAGE
            assert doc.page_size(0) == (120.0, 80.0)
            assert doc.page_count == 1
        finally:
            doc.close()

        pdf_path = os.path.join(tmp, "d.pdf")
        _blank_pdf(pdf_path, 200, 300, pages=3)
        doc = media.Document(pdf_path)
        try:
            assert doc.kind == media.KIND_PDF
            assert doc.page_count == 3
            assert abs(doc.page_size(0)[0] - 200) < 0.5
        finally:
            doc.close()


# ---------------------------------------------------------------------------
# 附加：判墨探针自证
# ---------------------------------------------------------------------------

def test_probe_self_proof_on_blank_canvas():
    """未加水印的原画布跑同一套判墨逻辑，必须判为「无墨迹」。"""
    for width, height in ((320, 240), (595, 842), (1000, 300)):
        blank = _canvas(width, height)
        assert _ink_bbox(blank) is None, f"{width}x{height} 空白画布被误判为有墨迹"
        # 加一点极浅灰纹（模拟纸张纹理）也应判为无墨，验证阈值不过敏
        textured = blank.copy()
        for y in range(0, height, 20):
            for x in range(width):
                textured.putpixel((x, y), (248, 248, 248))
        assert _ink_bbox(textured, dev=18) is None


def test_watermark_is_detected_by_probe():
    """带水印的页面必须被判为有墨迹（否则前面的断言全是假通过）。"""
    spec = WatermarkSpec(text="机密文件", margin_pct=3.0, font_pct=4.0,
                         opacity=0.30, color="#D32F2F").normalized()
    out = render.render_image(_canvas(595, 842), spec)
    assert _ink_bbox(out) is not None


# ---------------------------------------------------------------------------
# 7. 界面无残留入口（平铺是唯一模式，位置不可调）
# ---------------------------------------------------------------------------

def _make_root():
    """创建根窗口；缺 tkinterdnd2 / 无显示环境时抛 SkipTest（**不**算通过）。"""
    try:
        import tkinterdnd2
        return tkinterdnd2.TkinterDnD.Tk()
    except Exception as exc:
        raise unittest.SkipTest("需要 tkinterdnd2 / 可用显示环境：%s" % exc)


def test_ui_no_tile_switch_and_no_position_controls():
    """界面不得残留「平铺开关 / 位置调节」入口，也不再有任何平铺交代文案。

    * 参数面板：没有 ``tile_switch``、没有 ``pos_box``（偏移输入 / 重置居中 / 拖拽提示）
       —— 用 ``hasattr`` 兜住「控件被删但属性名还在」这类半删状态；
    * 预览画布：没有命中框接口（``set_hit_rect``），不再接受拖拽定位；
    * ``WatermarkSpec``：不含 ``tile`` / ``offset_x_pct`` / ``offset_y_pct`` 字段；
    * 「四周边距」只有平铺一种语义（文案含「留白」）；
    * 平铺是唯一且固定的行为：面板不再显示「铺满整页 · 预计 N 处水印」，
      连对应的 ``tile_hint`` 控件本身也应移除（不留半删状态）。
    """
    import dataclasses

    from wm.spec import WatermarkSpec

    field_names = {f.name for f in dataclasses.fields(WatermarkSpec)}
    assert "tile" not in field_names, "平铺开关应已移除"
    assert "offset_x_pct" not in field_names and "offset_y_pct" not in field_names, \
        "位置偏移字段应已移除"

    from wm.ui.preview import PreviewCanvas
    assert not hasattr(PreviewCanvas, "set_hit_rect"), "预览不应再提供拖拽命中框接口"

    root = _make_root()
    try:
        from wm.ui import app as ui_app
        application = ui_app.App(root)
        panel = application.panel
        root.update_idletasks()

        assert not hasattr(panel, "tile_switch"), "平铺开关残留"
        assert not hasattr(panel, "pos_box"), "位置调节区块残留"
        assert not hasattr(panel, "reset_btn"), "重置居中按钮残留"
        assert not hasattr(application, "_effective_offset_pct"), "偏移回算残留"
        assert not hasattr(application.preview, "_hit"), "命中框状态残留"

        assert "留白" in panel.margin_pct.hint_label.cget("text"), "边距文案应为平铺语义"
        assert not hasattr(panel, "tile_hint"), "平铺交代文案控件应已移除"
    finally:
        root.destroy()


def test_ui_load_spec_keeps_display_control_and_spec_in_sync():
    """``load_spec`` 后必须「显示值 = 控件值 = 内部 spec」。

    【QA 独立发现 - 源码缺陷】``SliderField.set(..., notify=False)`` 会把值 snap 到
    步长并夹回区间，但**不**回调 ``_on_*``，于是 ``_spec`` 停在未 snap 的原值：
    界面显示 12.0、渲染却用 12.4（不透明度：显示 30%、内部 0.304）。修复方式是
    灌完控件后用**控件的实际取值反向回写** spec，此用例即为该不变式的护栏。
    """
    root = _make_root()
    try:
        from wm.spec import WatermarkSpec, opacity_from_pct
        from wm.ui import app as ui_app
        panel = ui_app.App(root).panel
        root.update_idletasks()

        # (a) 非步长 / 非整值：三方必须完全一致
        panel.load_spec(WatermarkSpec(font_pct=12.4, margin_pct=12.4, angle=12.4,
                                      opacity=0.304))
        spec = panel.spec()
        for field, truth in ((panel.font_pct, spec.font_pct),
                             (panel.margin_pct, spec.margin_pct),
                             (panel.angle, spec.angle)):
            assert abs(field.get() - truth) <= 1e-9, (field.get(), truth)
            assert abs(float(field.var.get()) - truth) <= 1e-9, (field.var.get(), truth)
        assert abs(opacity_from_pct(panel.opacity.get()) - spec.opacity) <= 1e-9

        # (b) 越界脏值：控件夹回区间后，spec 必须跟着夹回
        panel.load_spec(WatermarkSpec(font_pct=999.0, margin_pct=-20.0, opacity=5.0))
        spec = panel.spec()
        assert spec.font_pct == 30.0 and spec.margin_pct == 0.0 and spec.opacity == 1.0
        assert abs(panel.font_pct.get() - spec.font_pct) <= 1e-9
        assert abs(panel.margin_pct.get() - spec.margin_pct) <= 1e-9

        # (c) 字体不在系统列表里：下拉框回退首项，spec 必须跟着回退
        if panel._families:
            panel.load_spec(WatermarkSpec(font_family="__肯定不存在的字体__"))
            index = panel.font_combo.current()
            assert 0 <= index < len(panel._families)
            assert panel.spec().font_family == panel._families[index]
    finally:
        root.destroy()


def test_preview_requests_are_serialized():
    """预览请求必须串行：忙时只记 pending，空闲时只启动一个 worker。

    用不执行 target 的假线程验证闸门，避免测试真的渲染；同时同步调用一次错误路径，
    证明 ``_preview_worker`` 的 ``finally`` 在异常出口也会释放 ``_preview_busy``。
    """
    root = _make_root()
    try:
        from wm.ui import app as ui_app

        application = ui_app.App(root)
        root.update_idletasks()
        assert application._preview_busy is False
        assert application._preview_pending is False

        class StubDocument:
            path = os.path.join(tempfile.gettempdir(), "wm_preview_gate_stub.png")

        created = []

        class StubThread:
            def __init__(self, target=None, args=(), daemon=None):
                self.target = target
                self.args = args
                self.daemon = daemon
                self.started = False
                created.append(self)

            def start(self):
                self.started = True

        application.doc = StubDocument()
        application.preview.canvas_size = lambda: (320, 240)
        application._need_loading = False
        original_thread = ui_app.threading.Thread
        ui_app.threading.Thread = StubThread
        try:
            application._preview_busy = True
            application._preview_pending = False
            application._render_preview_async()
            assert len(created) == 0, "busy 时不得创建第二个预览线程"
            assert application._preview_pending is True, "busy 时应记住最新 pending 请求"

            application._preview_busy = False
            application._preview_pending = True
            application._render_preview_async()
            assert len(created) == 1, "空闲时应恰好创建一个预览线程"
            assert created[0].started is True, "新建的预览线程必须启动"
            assert application._preview_busy is True
            assert application._preview_pending is False
        finally:
            ui_app.threading.Thread = original_thread

        # worker 会把不存在路径转换成 error 消息而非向外抛异常；取走消息避免队列残留。
        application._preview_busy = True
        missing = os.path.join(tempfile.gettempdir(), "wm_preview_worker_missing_file.png")
        if os.path.exists(missing):
            os.remove(missing)
        application._preview_worker(999, missing, 0, application.panel.spec(), 320, 240)
        assert application._preview_busy is False, "worker 异常出口必须释放预览闸门"
        result = application._queue.get_nowait()
        assert result.get("kind") == "preview" and "error" in result
        assert application._queue.empty(), "错误结果取走后队列应为空"
    finally:
        # App 会持续安排轮询；显式取消，避免销毁 Tk 后遗留 ``invalid command name`` 噪声。
        try:
            for after_id in root.tk.call("after", "info"):
                root.after_cancel(after_id)
        except Exception:
            pass
        root.destroy()


def test_ui_popdown_styling_rejects_non_combobox():
    """``style_combobox_popdown`` 对非下拉框必须返回 False 且**不留残留窗口**。

    【QA 独立发现 - 源码缺陷】Tcl 的 ``ttk::combobox::PopdownWindow`` 对任意 Tk 窗口
    （Label / Entry…）都会「成功」返回，并凭空预建出 ``<widget>.popdown`` 这个无意义
    的残留窗口。修复方式是先判控件类型；这里同时保留正向断言（真下拉框仍然生效），
    防止守卫把功能本身也挡掉。
    """
    import tkinter as tk
    from tkinter import ttk

    root = _make_root()
    try:
        from wm.ui.theme import style_combobox_popdown

        label = tk.Label(root)
        assert style_combobox_popdown(label) is False, "非下拉框应直接拒绝"
        assert int(root.tk.call("winfo", "exists", str(label) + ".popdown")) == 0, \
            "给 Label 预建出了残留的 .popdown 窗口"
        assert style_combobox_popdown(object()) is False, "非 Tk 对象应安全返回 False"

        combo = ttk.Combobox(root)
        assert style_combobox_popdown(combo) is True, "真下拉框仍应生效"
    finally:
        root.destroy()


def test_pdf_rotated_page_has_no_missing_band():
    """``/Rotate 90/270`` 的页面不得缺一条带（P0-1 回归）。

    ``page.rect`` 是**已应用旋转**的视觉尺寸（595×842 + /Rotate 90 => 842×595），
    而 ``insert_image`` 落在**未旋转**坐标系。历史上没做补偿，于是宽 842 的图层被
    塞进宽 595 的页 —— 一侧**整条无水印**（实测该段覆盖率 0.00%、总覆盖率从 6.98%
    掉到 3.88%），且预览是对的、导出是错的，用户完全看不出来。

    这里用**横向四段覆盖率**判定：平铺本该整页均匀，任何一段接近 0 就是缺带。
    """
    import shutil

    spec = WatermarkSpec(text="机密", font_pct=4.0, margin_pct=3.0, angle=30.0,
                         opacity=1.0, color="#d32f2f").normalized()
    tmp = tempfile.mkdtemp(prefix="wm_rot_")
    try:

        def build(path, w, h, rot):
            doc = fitz.open()
            page = doc.new_page(width=w, height=h)
            page.set_rotation(rot)
            doc.save(path)
            doc.close()

        def quarters(pdf_path):
            doc = fitz.open(pdf_path)
            pix = doc[0].get_pixmap(dpi=72)
            img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
            doc.close()
            arr = np.asarray(img).astype(np.int16)
            # 红色墨迹判据（水印色 #d32f2f 且 opacity=1.0）
            ink = ((arr[:, :, 0] > 150) & (arr[:, :, 1] < 180) & (arr[:, :, 2] < 180))
            w = ink.shape[1]
            return [float(ink[:, i * w // 4:(i + 1) * w // 4].mean()) for i in range(4)]

        src = os.path.join(tmp, "src.pdf")
        dst = os.path.join(tmp, "out.pdf")
        for rot in (90, 270):
            build(src, 595, 842, rot)
            render.render_pdf(src, dst, spec)
            q = quarters(dst)
            assert min(q) > max(q) * 0.5, \
                f"/Rotate {rot} 出现缺带：四段覆盖率 {[round(x, 4) for x in q]}"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_pdf_rotated_page_matches_unrotated_of_same_visual_size():
    """旋转页的输出必须与「同视觉尺寸的无旋转页」几乎一致（方向也不能反）。

    平铺图案转 180° 照样铺满整页，所以四段覆盖率**分辨不出方向错误** —— 必须拿它跟
    一张视觉尺寸相同的无旋转页逐像素比：方向对了两者应当近乎相同（实测 0.02/255）。
    """
    import shutil

    spec = WatermarkSpec(text="机密", font_pct=4.0, margin_pct=3.0, angle=30.0,
                         opacity=1.0, color="#d32f2f").normalized()
    tmp = tempfile.mkdtemp(prefix="wm_rotdir_")
    try:

        def build(path, w, h, rot):
            doc = fitz.open()
            page = doc.new_page(width=w, height=h)
            page.set_rotation(rot)
            doc.save(path)
            doc.close()

        def shoot(pdf_path):
            doc = fitz.open(pdf_path)
            pix = doc[0].get_pixmap(dpi=72)
            img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
            doc.close()
            return np.asarray(img).astype(np.int16)

        a_src, a_out = os.path.join(tmp, "a.pdf"), os.path.join(tmp, "a_out.pdf")
        b_src, b_out = os.path.join(tmp, "b.pdf"), os.path.join(tmp, "b_out.pdf")
        build(a_src, 595, 842, 90)     # 视觉尺寸 842×595
        build(b_src, 842, 595, 0)      # 视觉尺寸同样是 842×595
        render.render_pdf(a_src, a_out, spec)
        render.render_pdf(b_src, b_out, spec)
        diff = float(np.abs(shoot(a_out) - shoot(b_out)).mean())
        assert diff < 2.0, f"旋转页与同视觉尺寸无旋转页的平均像素差 {diff:.2f} 过大"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_block_bitmap_rejects_absurd_text():
    """超长大字号文本必须在**分配内存之前**快速失败（P0-2 回归）。

    极端组合下块尺寸会涨到十亿级像素：旧实现先空转二十几秒，再抛一个用户看不懂的
    ``DecompressionBombError``。现在应在毫秒级给出人话提示，且不能误伤常规合法用法。
    旋转后才膨胀的细长文本由 ``test_block_guard_uses_rotated_bounds`` 单独覆盖。
    """
    import time

    absurd = WatermarkSpec(text="A" * 2000, font_pct=30.0).normalized()
    started = time.perf_counter()
    try:
        layout._render_block_bitmap(absurd, 357.0)
        raise AssertionError("极端文本没有被拦截")
    except ValueError as exc:
        assert "过大" in str(exc), f"报错要给人话，实际：{exc}"
        assert time.perf_counter() - started < 8.0, \
            "必须快速失败（<8s），不能先空转二十几秒"

    # 常规合法用法不能被误伤。``"A"*200`` 不属于合法清单：它旋转前仅 5.8MP，
    # 旋转后却达 247.7MP / 约 945MB RGBA、耗时 2.74s，正是本护栏要消灭的内存炸弹。
    for text, pct in (("机密文件", 30.0), ("内部资料\n请勿外传", 12.0)):
        ok = WatermarkSpec(text=text, font_pct=pct).normalized()
        bitmap = layout._render_block_bitmap(ok, 178.5)
        assert bitmap.size[0] > 0 and bitmap.size[1] > 0


def test_block_guard_uses_rotated_bounds():
    """细长文本必须按**旋转后**包围盒早失败，且阈值保留最重合法用法的余量。

    ``"A"*200`` @178.5px / 30° 的旋转前画布仅 5.8MP，旋转后却是
    247.7MP / 约 945MB RGBA / 2.74s；旧的旋转前判据会放过它。测试临时禁止
    ``Image.new``，既证明守卫发生在分配前，也使「退回旧判据」的变异安全快速失败。
    """
    import time

    assert layout._rotated_pixel_estimate(123, 456, 0.0) == 123 * 456, \
        "angle=0 时必须保持原来的 width*height 判据"

    # 8000×8000 页面、font_pct=30%、PDF 2× 光栅的最重合法基准：文本「机密」、
    # 字号 4800px、30°，当前字体实测旋转后约 134.7MP；165MP 至少留 20% 余量。
    font = layout.fonts.pil_font("HarmonyOS Sans SC", 4800.0)
    ascent, descent = font.getmetrics()
    line_height = max(1, ascent + descent)
    padding = max(2, int(round(line_height * 0.08)))
    legal_width = int(np.ceil(font.getlength("机密"))) + padding * 2
    legal_height = line_height + padding * 2
    legal_pixels = layout._rotated_pixel_estimate(legal_width, legal_height, 30.0)
    assert legal_pixels <= layout.MAX_BLOCK_PIXELS
    assert layout.MAX_BLOCK_PIXELS >= legal_pixels * 1.20, \
        f"最重合法用法仅留 {(layout.MAX_BLOCK_PIXELS / legal_pixels - 1) * 100:.1f}% 余量"

    bomb = WatermarkSpec(text="A" * 200, font_pct=4.0, angle=30.0).normalized()
    original_new = layout.Image.new
    allocation_attempted = False

    def reject_allocation(*_args, **_kwargs):
        nonlocal allocation_attempted
        allocation_attempted = True
        raise AssertionError("旋转后超限的水印到达了 Image.new，守卫没有 fail-fast")

    layout.clear_caches()
    layout.Image.new = reject_allocation
    started = time.perf_counter()
    try:
        try:
            layout._render_block_bitmap(bomb, 178.5)
            raise AssertionError("247.7MP 的旋转后水印块没有被拦截")
        except ValueError as exc:
            assert "旋转后" in str(exc) and "过大" in str(exc), str(exc)
    finally:
        layout.Image.new = original_new
        layout.clear_caches()
    assert not allocation_attempted, "必须在 Image.new 之前完成旋转后尺寸判定"
    assert time.perf_counter() - started < 5.0, \
        "旋转后尺寸守卫必须毫秒级失败（<5s）"


def test_render_cache_respects_byte_budget():
    """块位图缓存按**字节**限流，不能只按条数（P0-4 回归）。

    单条块位图可达上百 MB，旧实现按条数（512）限流，拖一次字号滑杆就能驻留几个 GB。
    """
    layout.clear_caches()
    try:
        for pct in range(1, 31):
            layout._render_block_bitmap(
                WatermarkSpec(font_pct=float(pct)).normalized(), pct * 40.0)
        total = sum(layout._bitmap_cost(v) for v in layout._RENDER_CACHE.values())
        assert total <= layout._RENDER_CACHE_BYTES, \
            f"缓存 {total / 1024 / 1024:.0f}MB 超过预算 {layout._RENDER_CACHE_BYTES // 1024 // 1024}MB"
    finally:
        layout.clear_caches()


def test_image_render_scale_is_adaptive():
    """大图不超采样（防 OOM），小图保留超采样（保边缘精度）。

    2× 超采样的收益只有 0.14% 像素差，代价却是 10 倍耗时与数 GB 内存；但直接全部
    降到 1× 会让墨迹边缘抗锯齿变弱，与 PDF 路径的 bbox 差超过 2px 容差。故按像素
    预算自适应。
    """
    assert render.IMAGE_RENDER_SCALE == 2.0, "小图仍应超采样"
    assert render.IMAGE_SS_MAX_PIXELS > 0
    # 小图：走超采样（与 PDF 路径同倍率，边缘一致）
    small = _canvas(595, 842)
    render.render_image(small, WatermarkSpec().normalized())
    # 大图：不超采样，且必须能跑完不爆内存
    big = _canvas(3000, 3000)
    out = render.render_image(big, WatermarkSpec().normalized())
    assert out.size == big.size


# ---------------------------------------------------------------------------
# 8. 发布前健壮性回归（P1-3 / P1-2 / P1-6a）
# ---------------------------------------------------------------------------

def test_pdf_output_is_written_via_part_then_replace():
    """PDF 输出必须经 ``.part`` + 原子替换落地，**任何异常路径都不留 .part**（P1-3）。

    旧实现 ``doc.tobytes(garbage=4, deflate=True)`` 先把整份输出 PDF 全量驻留内存再
    一次性写盘：实测 30 页 / 输出 86MB 的样本，峰值工作集增量 **194.7MB**；改成
    ``doc.save(dst + ".part")`` 流式写盘后 **33.6MB**（-83%，耗时 9.14s -> 9.07s）。
    换实现的同时，「不覆盖源文件 / 不留半成品 / 不留垃圾」这条语义不能退化：

        * 成功            -> 目标存在且是合法 PDF、**无** ``.part`` 残留；
        * 取消            -> 抛 ``Cancelled``、目标**从不出现**、无 ``.part``；
        * ``os.replace`` 失败（目标是目录）-> 抛 OSError，连**上一轮遗留**的
          ``.part`` 也一并清掉；
        * 写盘路径不可用（目录不存在）-> 无目标、无 ``.part``。

    另外用 ``fitz.Document.save`` 探针直接断言**写的是 ``dst + ".part"`` 而不是 dst**
    —— 这一条专门盯住「退化回直接 save(dst)」（那样失败时会留下半个目标文件）。
    """
    import shutil

    spec = WatermarkSpec(text="机密", font_pct=4.0, margin_pct=3.0,
                         angle=30.0, opacity=0.30).normalized()
    tmp = tempfile.mkdtemp(prefix="wm_part_")
    try:
        src = os.path.join(tmp, "src.pdf")
        _blank_pdf(src, 595, 842, pages=3)

        saved: list = []
        original_save = fitz.Document.save

        def spy_save(self, *args, **kwargs):
            saved.append(str(args[0]) if args else str(kwargs.get("filename")))
            return original_save(self, *args, **kwargs)

        fitz.Document.save = spy_save
        try:
            # (a) 成功路径
            dst = os.path.join(tmp, "ok.pdf")
            assert render.render_pdf(src, dst, spec) == 3
            assert os.path.exists(dst), "目标文件不存在"
            with open(dst, "rb") as handle:
                assert handle.read(5) == b"%PDF-", "目标不是合法 PDF"
            assert not os.path.exists(dst + ".part"), "成功路径留下了 .part 残留"
            assert saved == [dst + ".part"], f"必须写 .part 而不是 dst：{saved}"

            # (b) 取消：必须在**写盘之前**停下
            cancelled = os.path.join(tmp, "cancelled.pdf")
            try:
                render.render_pdf(src, cancelled, spec, is_cancelled=lambda: True)
                raise AssertionError("取消回调没有被消费")
            except render.Cancelled:
                pass
            assert not os.path.exists(cancelled), "取消后不应产生目标文件"
            assert not os.path.exists(cancelled + ".part"), "取消后留下了 .part 残留"

            # (c) os.replace 失败（目标是目录）：连上一轮遗留的 .part 也要清掉
            blocker = os.path.join(tmp, "blocker")
            os.mkdir(blocker)
            stale = blocker + ".part"
            with open(stale, "wb") as handle:
                handle.write(b"stale")
            try:
                render.render_pdf(src, blocker, spec)
                raise AssertionError("目标是目录时应当失败")
            except OSError:
                pass
            assert not os.path.exists(stale), "os.replace 失败后 .part 没被清掉"

            # (d) 写盘路径不可用
            missing = os.path.join(tmp, "no_such_dir", "out.pdf")
            try:
                render.render_pdf(src, missing, spec)
                raise AssertionError("目标目录不存在时应当失败")
            except Exception:
                pass
            assert not os.path.exists(missing)
            assert not os.path.exists(missing + ".part"), "写盘失败后留下了 .part 残留"
        finally:
            fitz.Document.save = original_save
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_pdf_layer_cache_respects_byte_budget():
    """PDF 图层缓存按**字节**限流，且同尺寸场景命中率不降（P1-2）。

    旧实现按 ``(w, h, rot)`` 缓存 ``fitz.Pixmap`` 且**永不释放**。实测（60 页）：
        * 同尺寸 A4：1 条 / 7.6MB（没问题）；
        * **各差 1pt**（扫描件极常见）：60 条 / 累计 498.6MB 全留，
          峰值工作集增量 **1043.8MB**。
    现在：单条超预算不入缓存，入缓存后按插入顺序裁剪；实测同一样本降为
    驻留 **251.2MB**（29 条）、峰值增量 **787.9MB**，耗时 10.24s -> 9.88s。
    """
    import shutil

    spec = WatermarkSpec(text="机密", font_pct=4.0, margin_pct=3.0,
                         angle=30.0, opacity=0.30).normalized()
    original_budget = render.PDF_LAYER_CACHE_BYTES
    original_layer = render.render_output_layer
    misses: list = []

    def counting_layer(page_w, page_h, spec_, scale=1.0):
        misses.append((page_w, page_h))
        return original_layer(page_w, page_h, spec_, scale)

    def build(path, sizes):
        doc = fitz.open()
        for width, height in sizes:
            doc.new_page(width=width, height=height)
        doc.save(path, garbage=4, deflate=True)
        doc.close()

    def resident_bytes() -> int:
        return sum(render._pixmap_cost(pix) for pix in render._PDF_LAYER_CACHE.values())

    tmp = tempfile.mkdtemp(prefix="wm_layercache_")
    try:
        # (a) 同尺寸 8 页：只该渲染一次图层（预算没有误伤命中率）
        render.clear_pdf_layer_cache()
        render.render_output_layer = counting_layer
        try:
            same = os.path.join(tmp, "same.pdf")
            build(same, [(300, 420)] * 8)
            out = os.path.join(tmp, "same_out.pdf")
            assert render.render_pdf(same, out, spec) == 8
            assert len(misses) == 1, f"同尺寸 8 页只该渲染一次图层，实际 {len(misses)} 次"
            assert len(render._PDF_LAYER_CACHE) == 1
            single = resident_bytes()
            assert 0 < single < 20 * 1024 * 1024, f"单条图层 {single} 字节，量级不对"

            # (b) 8 页各差 1pt + 预算压到「约 2.5 条」：必须裁剪，但不清空
            misses.clear()
            render.clear_pdf_layer_cache()
            budget = int(single * 2.5)
            render.PDF_LAYER_CACHE_BYTES = budget
            scan = os.path.join(tmp, "scan.pdf")
            build(scan, [(300 + i, 420 + i) for i in range(8)])
            out2 = os.path.join(tmp, "scan_out.pdf")
            assert render.render_pdf(scan, out2, spec) == 8
            assert len(misses) == 8, f"尺寸各异的 8 页应各渲染一次，实际 {len(misses)} 次"
            assert len(render._PDF_LAYER_CACHE) < 8, "缓存没有被裁剪（尺寸各异却全留）"
            assert len(render._PDF_LAYER_CACHE) >= 2, "预算不该退化成「只留一条」"
            assert resident_bytes() <= budget, (
                f"缓存 {resident_bytes()} 字节超过预算 {budget}")

            # (c) 单条就超预算 -> 一条都不入缓存，但**功能不受影响**
            render.clear_pdf_layer_cache()
            render.PDF_LAYER_CACHE_BYTES = 1
            out3 = os.path.join(tmp, "nobudget_out.pdf")
            assert render.render_pdf(scan, out3, spec) == 8
            assert len(render._PDF_LAYER_CACHE) == 0, "单条超预算就不该入缓存"
            with open(out3, "rb") as handle:
                assert handle.read(5) == b"%PDF-", "不入缓存时输出仍应正常"

            # (d) 缓存键必须含参数指纹：先让 spec A 入缓存，再以同尺寸 spec B
            # 紧接渲染；两次之间故意不清缓存，否则这个护栏会退化为空断言。
            misses.clear()
            render.clear_pdf_layer_cache()
            render.PDF_LAYER_CACHE_BYTES = original_budget
            out4a = os.path.join(tmp, "spec_a_out.pdf")
            assert render.render_pdf(same, out4a, spec) == 8
            assert len(misses) == 1, "前置：spec A 应渲染一层并写入模块级缓存"
            other = spec.copy(text="内部资料", color="#1565c0")
            out4b = os.path.join(tmp, "spec_b_out.pdf")
            assert render.render_pdf(same, out4b, other) == 8
            assert len(misses) == 2, "换参数后必须重新渲染图层，不能命中 spec A 的缓存"
            assert resident_bytes() <= render.PDF_LAYER_CACHE_BYTES

            # (e) 缓存键也必须含光栅倍率：1× 与 2× 的页面/spec 完全相同，但图层
            # 分辨率不同。漏掉 scale 会让第二次静默复用低清图层。
            misses.clear()
            render.clear_pdf_layer_cache()
            scale1 = os.path.join(tmp, "scale1_out.pdf")
            assert render.render_pdf(same, scale1, spec, scale=1.0) == 8
            assert len(misses) == 1, "前置：scale=1.0 应渲染一层"
            scale2 = os.path.join(tmp, "scale2_out.pdf")
            assert render.render_pdf(same, scale2, spec, scale=2.0) == 8
            assert len(misses) == 2, "scale 改变后必须重新渲染，不能复用另一倍率的图层"
            assert len(render._PDF_LAYER_CACHE) == 2, "同一 spec 的两个倍率应各占一条缓存"
            assert resident_bytes() <= render.PDF_LAYER_CACHE_BYTES
        finally:
            render.render_output_layer = original_layer
            render.PDF_LAYER_CACHE_BYTES = original_budget
            render.clear_pdf_layer_cache()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    assert original_budget == 256 * 1024 * 1024, "预算被改动过，请同步更新本用例"


def test_render_image_cancels_between_expensive_steps():
    """``render_image`` 必须在**耗时步骤之间**响应取消（P1-6a）。

    旧实现没有取消点，点「取消」要跑满整张才停。检查点的位置由实测决定：
        * 2000×2000（4MP，走 2× 超采样）：``layer.resize`` 0.29s / 总 0.51s —— 最贵；
        * 4000×4000（16MP，1×）：总 0.06s，其中 ``convert`` / 图层 / 合成各占一块；
        * 平铺循环本身很便宜（156 块 0.05s），故只每 64 块查一次。
    用例用「回调被调用的次数 + 位置」钉住这些检查点：
        * 一进来就取消 -> 立刻抛 ``Cancelled``（回调只被调 1 次，耗时远小于完整渲染）；
        * 第 3 次检查才取消 -> 抛 ``Cancelled`` 且回调恰好被调 3 次（证明是**中途**停）；
        * 从不取消 -> 回调被调 >= 3 + ceil(块数/64) 次（证明检查点真的穿插在各步骤
          之间，包括平铺循环里），且结果与不传回调**逐像素一致**（既有调用方不受影响）。
    """
    import time

    spec = WatermarkSpec(text="机密文件", font_pct=0.5, margin_pct=3.0,
                         angle=30.0, opacity=0.30).normalized()
    side = 4000
    img = _canvas(side, side)

    size = layout.font_px(spec, side, side)
    (block_w, block_h), ink = layout.block_geometry(spec, size)
    blocks = len(layout.compute_placements(side, side, block_w, block_h, spec, ink))
    assert blocks >= 65, f"本用例需要 >= 65 块才能覆盖「每 64 块一查」，实际 {blocks}"

    # (a) 基线：不传回调的完整渲染
    started = time.perf_counter()
    reference = render.render_image(img, spec)
    full = time.perf_counter() - started
    assert _ink_bbox(reference) is not None, "基线渲染没有水印"

    # (b) 一进来就取消：必须在任何耗时步骤之前抛
    calls: list = []

    def immediate() -> bool:
        calls.append(1)
        return True

    started = time.perf_counter()
    try:
        render.render_image(img, spec, is_cancelled=immediate)
        raise AssertionError("取消回调没有被消费")
    except render.Cancelled:
        pass
    elapsed = time.perf_counter() - started
    assert len(calls) == 1, f"应立即在入口处取消，回调被调用 {len(calls)} 次"
    assert elapsed < max(0.001, full * 0.5), \
        f"取消耗时 {elapsed:.4f}s 未显著小于完整渲染 {full:.4f}s"

    # (c) 第 3 次检查才取消：证明是**中途**停，不是跑完才看
    counter = {"n": 0}

    def third() -> bool:
        counter["n"] += 1
        return counter["n"] >= 3

    try:
        render.render_image(img, spec, is_cancelled=third)
        raise AssertionError("取消回调没有被消费")
    except render.Cancelled:
        pass
    assert counter["n"] == 3, f"应在第 3 个检查点停下，实际回调被调用 {counter['n']} 次"

    # (d) 从不取消：检查点数量足够，且输出与不传回调完全一致
    counter2 = {"n": 0}

    def never() -> bool:
        counter2["n"] += 1
        return False

    same = render.render_image(img, spec, is_cancelled=never)
    minimum = 3 + (blocks + 63) // 64  # 入口 + convert 后 + 块位图后 + 循环内每 64 块
    assert counter2["n"] >= minimum, \
        f"检查点太少：{counter2['n']} < {minimum}（块数 {blocks}）"
    assert same.size == reference.size and same.mode == reference.mode
    assert np.array_equal(np.asarray(same), np.asarray(reference)), \
        "传了取消回调就改变输出 —— 既有调用方会被波及"


# ---------------------------------------------------------------------------
# 9. 图片 EXIF 方向契约（P1-5）
#
# 契约由 software-engineer-7 提供（他改 wm/media.py，不写测试；本节用例由我补在
# tests/test_all.py 里）。期望值均按本机 Pillow 实测写死，不是宽松断言。
# ---------------------------------------------------------------------------

def test_no_deprecated_fitz_import_in_media_and_main():
    """P1-7：``wm.media`` / ``main`` 的导入不得触发 PyMuPDF 的弃用提示。

    ⚠️ 判据关键（实测踩到的坑，别改成只看退出码 / 只看 stderr）：PyMuPDF 1.28 的
    弃用提示是**直接写到 stdout**，**不走** ``warnings`` 模块 —— 实测
    ``python -W error::DeprecationWarning -c "import fitz"``：退出码 **0**、
    stderr **为空**，提示全在 stdout（``warning: The `fitz` API is deprecated ...``）。
    所以「看退出码」或「只看 stderr」都会变成**假绿灯**。这里做两件事：

        * 先跑一个 ``import fitz`` 的**对照组**，确认本机提示确实能被捕获 ——
          判据一旦失效（将来 PyMuPDF 改了输出方式）就立刻报错，而不是安静地假通过；
        * 再逐个导入被测模块，断言 stdout+stderr 里都没有该提示。
    """
    import subprocess

    def run_snippet(code: str):
        return subprocess.run([sys.executable, "-c", code], cwd=ROOT,
                              capture_output=True, text=True)

    control = run_snippet("import fitz")
    assert "deprecated" in (control.stdout + control.stderr).lower(), (
        "判据失效：本机 `import fitz` 没有产生可捕获的弃用提示，本用例会变成假绿灯；"
        "请改用「检查 sys.modules 里有没有 fitz」之类的判据")

    for module in ("wm.media", "main"):
        proc = run_snippet(f"import {module}")
        assert proc.returncode == 0, f"{module}: {proc.stderr}"
        combined = (proc.stdout + proc.stderr).lower()
        assert "deprecated" not in combined, f"{module}: {proc.stdout}{proc.stderr}"


# ---------------------------------------------------------------------------
# 9. 生命周期与错误处理（P1-9 / P1-10 / P1-11 回归）
# ---------------------------------------------------------------------------

def test_ui_close_releases_pending_after_and_document():
    """关闭窗口：挂起的 after 清空 + self.doc 置 None + 源文件句柄释放（P1-9 回归）。

    ``root.destroy()`` 会删掉 after 注册的 Tcl 命令，但**已排队的定时器不会消失**，
    而且 Tk 解释器在 root 销毁后仍然活着 —— 谁再泵一次事件循环，Tcl 就去执行一个
    已删除的命令，于是报 ``invalid command name``。所以判据要干脆：
    **销毁后 ``after info`` 必须为空**。

    另注：实测真正会锁源文件句柄的是 **PDF（PyMuPDF）**，不是 GIF / 多帧 TIFF ——
    Pillow 在 ``load()`` 之后就关掉了图片 fp。所以用 PDF 做句柄判据。
    """
    root = _make_root()
    try:
        from wm.ui import app as ui_app
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "a.pdf")
            doc = fitz.open()
            doc.new_page(width=120, height=90)
            doc.save(path)
            doc.close()
            application = ui_app.App(root)
            application.add_files([path])
            application._select_file(0)
            root.update_idletasks()
            assert application.doc is not None, "前置：文档应已打开"
            assert tuple(root.tk.call("after", "info")), "前置：应有挂起的 after"
            application._on_close()
            assert not tuple(root.tk.call("after", "info")), \
                "关闭后不得仍有挂起的 after"
            assert application._closing is True and application._cancel is True
            assert application.doc is None, "关闭后 doc 必须置 None"
            assert application._poll_job is None and application._preview_job is None
            os.rename(path, path + ".ren")   # PDF 句柄没放开会是 WinError 32
            os.rename(path + ".ren", path)
    finally:
        try:
            if root.winfo_exists():
                root.destroy()
        except Exception:
            pass

    # ``pythonw`` / 某些嵌入宿主会让 sys.stderr 为 None。用不可写属性的最小 root
    # 强制走守卫安装失败分支，确认警告改写到 sys.__stderr__，而不是丢到 stdout。
    import io

    class LockedRoot:
        __slots__ = ()

        def destroy(self) -> None:
            return

    probe = ui_app.App.__new__(ui_app.App)
    probe.root = LockedRoot()
    fallback = io.StringIO()
    original_stderr = sys.stderr
    original_dunder_stderr = sys.__stderr__
    try:
        sys.stderr = None
        sys.__stderr__ = fallback
        probe._guard_root_destroy()
    finally:
        sys.stderr = original_stderr
        sys.__stderr__ = original_dunder_stderr
    assert "未能安装 destroy 守卫" in fallback.getvalue(), \
        "sys.stderr=None 时必须回退到 sys.__stderr__"


def test_ui_file_list_is_readonly_while_batch_runs():
    """批处理进行中：拖放 / 删除 / 清空必须被拒并给出原因（P1-10 回归）。

    分母错乱的根源是「批处理在跑、列表还能改」。这里用一个**真活着的线程**模拟
    批处理，而不是只看标志位。
    """
    import threading

    root = _make_root()
    try:
        from wm.ui import app as ui_app
        with tempfile.TemporaryDirectory() as tmp:
            paths = []
            for name in ("a.png", "b.png"):
                p = os.path.join(tmp, name)
                Image.new("RGB", (32, 24), WHITE).save(p)
                paths.append(p)
            application = ui_app.App(root)
            application.add_files(paths)
            root.update_idletasks()
            stop = threading.Event()
            thread = threading.Thread(target=lambda: stop.wait(0.5), daemon=True)
            thread.start()
            application._batch_thread = thread
            assert thread.is_alive()
            assert application._on_drop(
                type("E", (), {"data": "{%s}" % paths[0]})()) == "break"
            application.files_list.selection_set(0)
            application._remove_selected()
            application._clear_files()
            assert len(application.files) == 2, "处理中不得改动文件列表"
            assert application.status.cget("text") == "处理中，暂不能修改文件列表"
            stop.set()
            thread.join(timeout=3.0)
            application._batch_thread = None
            application.files_list.selection_set(0)
            application._remove_selected()
            assert len(application.files) == 1, "线程结束后应恢复可修改"
    finally:
        root.destroy()


def test_ui_tk_callback_error_is_reported_on_status_bar():
    """Tk 回调异常必须进 stderr 且状态栏显形（P1-11 回归）。

    冻结版 ``sys.stderr`` 被重定向到 ``runtime.log``，用户完全看不到回调异常。
    这里**故意不用 messagebox** —— 无头/自检流程里弹模态框会永久挂住
    （有 UI 用例要跑 300 次 ``root.update()``）。
    """
    import io
    from contextlib import redirect_stderr

    from wm.ui import theme as T

    root = _make_root()
    try:
        from wm.ui import app as ui_app
        application = ui_app.App(root)
        root.update_idletasks()
        buffer = io.StringIO()
        with redirect_stderr(buffer):
            try:
                raise RuntimeError("boom-probe")
            except RuntimeError as exc:
                application._on_tk_error(type(exc), exc, exc.__traceback__)
        assert "boom-probe" in buffer.getvalue(), \
            "异常必须进 stderr（冻结版即 runtime.log）"
        assert application.status.cget("text").startswith("内部错误：")
        assert str(application.status.cget("fg")) == str(T.DANGER), "状态栏必须标红"
    finally:
        root.destroy()


def test_image_exif_orientation_is_applied_and_written_back():
    """源图 EXIF 方向必须在 Document 层转正，且转正后的 EXIF 能写回输出（P1-5 回归）。

    手机竖拍照片大量是 ``Orientation=6``。旧实现直接返回未转正的尺寸、输出丢光 EXIF，
    于是「预览/输出与资源管理器里看到的方向不一致」。

    注意这里**不用** ``App._write_image``（它的第三参是 params 字典），而走
    ``App._save_image(out, dst, exif=...)`` 这个对外口径。
    """
    from wm.ui import app as ui_app

    with tempfile.TemporaryDirectory() as tmp:
        source = os.path.join(tmp, "src.jpg")
        probe = Image.new("RGB", (40, 20), WHITE)
        exif = probe.getexif()
        exif[274] = 6              # Orientation = 竖拍（顺时针 90°）
        exif[315] = "wm-exif-probe"  # Artist
        probe.save(source, exif=exif)

        doc = media.Document(source)
        try:
            width, height = doc.page_size()
            assert (width, height) == (20.0, 40.0), \
                f"Orientation=6 必须转正成 20x40，实际 {width}x{height}"
            assert doc.exif_bytes, "转正后 EXIF 字节不应为空"

            out = os.path.join(tmp, "out.jpg")
            ui_app.App._save_image(Image.new("RGBA", (20, 40)), out, exif=doc.exif_bytes)
            saved = Image.open(out).getexif()
            assert saved.get(315) == "wm-exif-probe", \
                f"其它 EXIF 条目必须保留，实际 {dict(saved)}"
            assert saved.get(274) in (None, 1), \
                f"方向标签必须清除（否则会二次旋转），实际 {saved.get(274)}"
        finally:
            doc.close()


def test_image_exif_absent_orientation_one_are_untouched():
    """无 EXIF / Orientation=1 必须保持像素与惰性打开（P1-5 回归）。

    PNG 是最常见的无 EXIF 场景。除守住尺寸与元数据契约外，还逐字节比较解码后
    像素，并在首次取像素前确认 Pillow 尚未建立解码核心；否则仅打开 20MP 图片就会
    无谓驻留约 76MB 像素缓冲。
    """
    with tempfile.TemporaryDirectory() as tmp:
        plain = os.path.join(tmp, "plain.png")
        pixels = np.arange(40 * 20 * 3, dtype=np.uint8).reshape((20, 40, 3))
        Image.fromarray(pixels).save(plain)
        doc = media.Document(plain)
        try:
            assert doc.page_size() == (40.0, 20.0), "无 EXIF PNG 尺寸不得变化"
            assert doc.exif_bytes is None, "无 EXIF 时 exif_bytes 必须是 None"
            assert doc._image is None, \
                "无 EXIF 图片打开时不得立即解码或驻留像素"
            assert doc.page_image().tobytes() == pixels.tobytes(), \
                "无 EXIF PNG 的解码像素必须逐字节不变"
        finally:
            doc.close()

        upright = os.path.join(tmp, "upright.jpg")
        probe = Image.new("RGB", (40, 20), WHITE)
        exif = probe.getexif()
        exif[274] = 1
        probe.save(upright, exif=exif)
        doc = media.Document(upright)
        try:
            assert doc.page_size() == (40.0, 20.0), "Orientation=1 不得改变尺寸"
            assert doc.exif_bytes, "Orientation=1 的 EXIF 应保留"
            assert doc._image is None, \
                "Orientation=1 图片打开时不得立即解码或驻留像素"
            with Image.open(upright) as expected:
                assert doc.page_image().tobytes() == expected.convert("RGB").tobytes(), \
                    "Orientation=1 JPEG 的解码像素必须逐字节不变"
        finally:
            doc.close()


def test_multiframe_frame_count_survives_exif_transpose():
    """多帧图的 frame_count 不能被 exif_transpose 吞掉（P1-5 回归）。

    ``ImageOps.exif_transpose()`` 返回的副本只含当前帧，``n_frames`` 会从 2 变 1。
    实现必须在**转正之前**记录原始帧数，否则「仅处理首帧」的用户提示会消失。
    """
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "anim.gif")
        # 两帧必须**内容不同**：全同的两帧会被 Pillow 的 GIF 编码器合并成 1 帧，
        # 那样这条用例就成了假通过（踩过）。
        first = Image.new("RGB", (16, 12), (255, 0, 0))
        second = Image.new("RGB", (16, 12), (0, 0, 255))
        first.save(path, save_all=True, append_images=[second])
        assert Image.open(path).n_frames == 2, "前置：源图必须真是 2 帧"

        doc = media.Document(path)
        try:
            assert doc.frame_count == 2, \
                f"多帧图 frame_count 必须仍是 2，实际 {doc.frame_count}"
            assert doc.page_size() == (16.0, 12.0), "首帧尺寸不得被转正流程改变"
        finally:
            doc.close()


def test_pdf_document_has_no_exif_and_no_deprecated_fitz_import():
    """PDF 分支 exif_bytes 为 None；且全仓库不得再有已弃用的 import fitz（P1-5/P1-7）。"""
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "a.pdf")
        doc = fitz.open()
        doc.new_page(width=120, height=90)
        doc.save(path)
        doc.close()

        wrapped = media.Document(path)
        try:
            assert wrapped.exif_bytes is None, "PDF 没有 EXIF"
        finally:
            wrapped.close()

    # P1-7：``import fitz`` 自 PyMuPDF 1.28 起已弃用，升版会直接 ImportError。
    # 用源码扫描守住，比等它哪天炸掉便宜。
    import re as _re

    root_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    offenders = []
    for dirpath, _dirnames, filenames in os.walk(root_dir):
        if "__pycache__" in dirpath or "_smoke" in dirpath or "build" in dirpath:
            continue
        for name in filenames:
            if not name.endswith(".py"):
                continue
            full = os.path.join(dirpath, name)
            with open(full, encoding="utf-8") as handle:
                for line in handle:
                    if _re.match(r"\s*import\s+fitz\b", line):
                        offenders.append(f"{os.path.relpath(full, root_dir)}: {line.strip()}")
    assert not offenders, "仍存在已弃用的 import fitz：\n" + "\n".join(offenders)
