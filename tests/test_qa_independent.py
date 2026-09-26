"""QA 独立测试层（严过关 / Yan）—— 面向「证明它能用 / 找出它不能用」。

与 ``tests/test_all.py``（工程师自测）**互补且刻意不同角度**：
    * A 预览同构 WYSIWYG：把预览路径（小尺寸 fit-to-canvas 渲染）与输出路径
      （全分辨率）按比例比对，而非只测几何；
    * B **真实像素**贴边：遍历多页 PDF 的每一页、混合尺寸页、图片路径，用
      「逐通道最大偏差」判墨（不用亮度阈值）；
    * C PDF 专项：alpha 保留（读回 PDF 渲成图取角落像素）、多页、混合尺寸、
      图片/PDF 两路径一致性、加密 / 0 页 / 损坏 PDF 的错误处理；
    * D 异常与边界输入（含重复处理命名递增 + 原文件哈希不变）；
    * E 批量与取消（进度单调、失败隔离）；
    * F 界面联动（10 轮反复切换、默认态、即时生效、DND 解析）；
    * G 性能量级（给出实测秒数）。

依赖：系统 Python（tkinter）+ Pillow + PyMuPDF + numpy。pytest 未装，用裸 assert，
由 ``tests/run_all.py`` 发现执行。Windows 控制台为 GBK：只用 [OK]/[FAIL]，不用 emoji。
"""

from __future__ import annotations

import hashlib
import os
import queue as _queue
import sys
import tempfile
import time
import unittest

import numpy as np
from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import pymupdf as fitz  # noqa: E402  PyMuPDF（``import fitz`` 自 1.28 起已弃用）
from wm import layout, media, render  # noqa: E402
from wm.spec import WatermarkSpec  # noqa: E402

DEV = 10  # 判墨阈值（通道最大偏差）
WHITE = (255, 255, 255)


# ---------------------------------------------------------------------------
# 通用工具
# ---------------------------------------------------------------------------

def _canvas(width: int, height: int) -> Image.Image:
    return Image.new("RGB", (width, height), WHITE)


def _ink_mask(img: Image.Image, dev: int = DEV) -> np.ndarray:
    """中位色 + 逐通道最大偏差判墨（**不用亮度阈值**：浅色水印叠白底亮度仍高）。"""
    arr = np.asarray(img.convert("RGB")).astype(np.int16)
    bg = np.median(arr.reshape(-1, 3), axis=0)
    return np.abs(arr - bg).max(axis=2) >= dev


def _ink_bbox(img: Image.Image, dev: int = DEV):
    mask = _ink_mask(img, dev)
    ys, xs = np.where(mask)
    if len(xs) == 0:
        return None
    return int(xs.min()), int(ys.min()), int(xs.max()), int(ys.max())


def _blank_pdf(path: str, sizes, pages: int = 1) -> None:
    doc = fitz.open()
    for i in range(pages):
        w, h = sizes[i % len(sizes)] if isinstance(sizes, list) else sizes
        doc.new_page(width=w, height=h)
    doc.save(path, garbage=4, deflate=True)
    doc.close()


def _write_zero_page_pdf(path: str) -> None:
    """手工构造 0 页 PDF（PyMuPDF 不允许 ``save`` 0 页文档，故手写字节）。"""
    raw = (b"%PDF-1.4\n"
           b"1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
           b"2 0 obj<</Type/Pages/Kids[]/Count 0>>endobj\n"
           b"trailer<</Root 1 0 R/Size 3>>\n%%EOF\n")
    with open(path, "wb") as handle:
        handle.write(raw)


def _render_pdf_page(path: str, index: int = 0, dpi: int = 144) -> Image.Image:
    doc = fitz.open(path)
    try:
        pix = doc[index].get_pixmap(dpi=dpi)
        return Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
    finally:
        doc.close()


def _preview_from_spec(page_w: float, page_h: float, spec: WatermarkSpec,
                       canvas_w: int = 520, canvas_h: int = 420):
    """复刻 ``App._preview_worker`` 的 fit-to-canvas 缩放：返回 (预览RGB图, 显示尺寸, scale)。"""
    pad = 12
    avail_w = max(40, canvas_w - 2 * pad)
    avail_h = max(40, canvas_h - 2 * pad)
    scale = min(avail_w / page_w, avail_h / page_h)
    scale = max(0.02, min(scale, 4.0))
    disp_w = max(1, int(round(page_w * scale)))
    disp_h = max(1, int(round(page_h * scale)))

    base = _canvas(int(page_w), int(page_h)).convert("RGBA").resize(
        (disp_w, disp_h), Image.LANCZOS)
    layer = render.render_overlay_layer(page_w, page_h, spec, scale=scale)
    if layer.size != base.size:
        layer = layer.resize(base.size, Image.LANCZOS)
    preview = Image.alpha_composite(base, layer).convert("RGB")
    return preview, (disp_w, disp_h), scale


def _new_root():
    """创建根窗口；缺 tkinterdnd2 / 无显示环境时抛 SkipTest（**不**算通过）。"""
    try:
        import tkinterdnd2
        return tkinterdnd2.TkinterDnD.Tk()
    except Exception as exc:
        raise unittest.SkipTest("需要 tkinterdnd2 / 可用显示环境：%s" % exc)


# ===========================================================================
# A. 预览同构（WYSIWYG）
# ===========================================================================

#: 平铺是唯一模式，故用例只遍历「页面尺寸 × 旋转角」
_PREVIEW_CASES = [
    (320, 240, 0.0), (320, 240, 30.0), (320, 240, 90.0),
    (2480, 3508, 0.0), (2480, 3508, 30.0), (2480, 3508, 90.0),
]


def test_qa_preview_output_bbox_homomorphism():
    """预览（小尺寸渲染后放大到输出尺寸）与全分辨率输出的墨迹 bbox 相对偏差 ≤ 1.5%。

    覆盖：旋转 0/30/90 × 320×240 与 2480×3508 两种差异极大的页面。
    """
    worst = 0.0
    detail = []
    for width, height, angle in _PREVIEW_CASES:
        spec = WatermarkSpec(text="机密文件", font_pct=4.0, margin_pct=3.0,
                             angle=angle, opacity=1.0, color="#d32f2f").normalized()
        preview, _disp, _scale = _preview_from_spec(width, height, spec)
        preview_up = preview.resize((width, height), Image.LANCZOS)
        full = render.render_image(_canvas(width, height), spec).convert("RGB")
        pb, fb = _ink_bbox(preview_up), _ink_bbox(full)
        assert pb is not None and fb is not None, (width, height, angle, pb, fb)
        dev_px = max(abs(pb[i] - fb[i]) for i in range(4))
        rel_pct = dev_px / max(width, height) * 100.0
        worst = max(worst, rel_pct)
        detail.append(f"{width}x{height} a={angle}: {rel_pct:.3f}%")
    assert worst <= 1.5, "预览/输出 bbox 相对偏差超 1.5%：\n" + "\n".join(detail)


def test_qa_preview_draws_every_scheduled_tile():
    """预览与输出在**每个排布块**中心都有墨迹（等价于行列数一致，且不是栅格投影假象）。"""
    for width, height, angle in ((2480, 3508, 0.0), (2480, 3508, 30.0),
                                 (2480, 3508, 90.0), (900, 600, 45.0)):
        spec = WatermarkSpec(text="机密文件", font_pct=4.0, margin_pct=3.0,
                             angle=angle, opacity=1.0, color="#d32f2f").normalized()
        size = layout.font_px(spec, width, height)
        ((bw, bh), ink) = layout.block_geometry(spec, size)
        placements = layout.compute_placements(width, height, bw, bh, spec, ink)

        preview, _d, scale = _preview_from_spec(width, height, spec)
        full = render.render_image(_canvas(width, height), spec).convert("RGB")
        parr = np.asarray(preview).astype(np.int16)
        farr = np.asarray(full).astype(np.int16)

        def has_ink(arr, cx, cy, rad):
            x0, x1 = max(0, int(cx - rad)), min(arr.shape[1], int(cx + rad) + 1)
            y0, y1 = max(0, int(cy - rad)), min(arr.shape[0], int(cy + rad) + 1)
            if x1 <= x0 or y1 <= y0:
                return False
            return bool((arr[y0:y1, x0:x1] < 200).any())

        for x, y in placements:
            cx = x + (ink[0] + ink[2]) / 2.0
            cy = y + (ink[1] + ink[3]) / 2.0
            assert has_ink(parr, cx * scale, cy * scale, 2), \
                f"预览缺少块 @({cx:.1f},{cy:.1f}) {width}x{height} a={angle}"
            assert has_ink(farr, cx, cy, int(round(size * 0.3))), \
                f"输出缺少块 @({cx:.1f},{cy:.1f}) {width}x{height} a={angle}"


def test_qa_preview_output_coverage_within_2pp():
    """预览与输出的墨迹覆盖率偏差 ≤ 2 个百分点（同分辨率比对，规避上采样模糊偏置）。

    【QA 独立发现 - 源码侧预览保真度】平铺模式下预览把水印块**按缩放后的字号**
    直接光栅化（11px 左右），而输出是全分辨率字形再整体缩小；同一份 spec 下两者
    的字形密度不同，导致墨迹覆盖率偏差可达约 4pp。位置 bbox 仍是精确的（见上一条）。
    此断言即为此缺陷的护栏。
    """
    worst = 0.0
    detail = []
    for width, height, angle in ((320, 240, 90.0), (2480, 3508, 0.0),
                                 (2480, 3508, 30.0), (2480, 3508, 90.0)):
        spec = WatermarkSpec(text="机密文件", font_pct=4.0, margin_pct=3.0,
                             angle=angle, opacity=1.0, color="#d32f2f").normalized()
        preview, disp, _scale = _preview_from_spec(width, height, spec)
        full = render.render_image(_canvas(width, height), spec).convert("RGB")
        full_small = full.resize(disp, Image.LANCZOS)
        pm = _ink_mask(preview).mean() * 100.0
        fm = _ink_mask(full_small).mean() * 100.0
        dev = abs(pm - fm)
        worst = max(worst, dev)
        detail.append(f"{width}x{height} a={angle}: "
                      f"prev={pm:.2f}% full={fm:.2f}% dev={dev:.2f}pp")
    assert worst <= 2.0, "预览/输出覆盖率偏差超 2pp：\n" + "\n".join(detail)


# ===========================================================================
# B. 真实渲染的像素级贴边
# ===========================================================================

def test_qa_probe_self_proof_blank_canvas_no_ink():
    """探针自证：未加水印的原画布跑同一套判墨逻辑必须判为「无墨迹」。"""
    for width, height in ((320, 240), (595, 842), (1000, 300)):
        assert _ink_bbox(_canvas(width, height)) is None, f"{width}x{height} 空白被误判有墨"


def test_qa_image_path_ink_exactly_at_margin():
    """图片路径：渲染层墨迹 bbox（alpha>0）四边**恰好**落于距页边 margin 处（≤1px）。"""
    for width, height in ((320, 240), (595, 842), (400, 400), (300, 800), (800, 600)):
        for margin_pct in (1.0, 3.0, 8.0):
            spec = WatermarkSpec(text="机密文件", font_pct=4.0,
                                 margin_pct=margin_pct, angle=30.0, opacity=1.0,
                                 color="#d32f2f").normalized()
            margin = margin_pct / 100.0 * min(width, height)
            layer = render.render_overlay_layer(width, height, spec, scale=1.0)
            bb = layer.getchannel("A").getbbox()
            assert bb is not None, (width, height, margin_pct)
            expected = (margin, margin, width - margin, height - margin)
            for got, exp in zip(bb, expected):
                assert abs(got - exp) <= 1.0, (
                    f"层内贴边偏差 {width}x{height} m={margin_pct}%: {bb} vs "
                    f"{tuple(round(v, 2) for v in expected)}")


def test_qa_pdf_tile_margin_every_page():
    """多页 PDF（A4）**每一页**平铺墨迹 bbox 四边都在距页边 margin 处（≤2px）。"""
    width, height = 595, 842
    spec = WatermarkSpec(text="机密文件", font_pct=4.0, margin_pct=3.0,
                         angle=30.0, opacity=1.0, color="#d32f2f").normalized()
    margin = 3.0 / 100.0 * min(width, height)
    with tempfile.TemporaryDirectory() as tmp:
        src = os.path.join(tmp, "in.pdf")
        dst = os.path.join(tmp, "out.pdf")
        _blank_pdf(src, (width, height), pages=3)
        render.render_pdf(src, dst, spec)
        for index in range(3):
            blank = np.asarray(_render_pdf_page(src, index)).astype(np.int16)
            marked = np.asarray(_render_pdf_page(dst, index)).astype(np.int16)
            mask = np.abs(marked - blank).max(axis=2) >= DEV
            ys, xs = np.where(mask)
            assert len(xs) > 0, f"第 {index + 1} 页无水印"
            k = 144 / 72.0
            left = xs.min() / k - margin
            top = ys.min() / k - margin
            right = ((width * k - 1) - xs.max()) / k - margin
            bottom = ((height * k - 1) - ys.max()) / k - margin
            tag = f"第{index + 1}页 L{left:+.2f} T{top:+.2f} R{right:+.2f} B{bottom:+.2f}"
            for value in (left, top, right, bottom):
                assert abs(value) <= 2.0, f"A4 贴边超 2px：{tag}"


def test_qa_pdf_tile_margin_across_sizes():
    """跨页面尺寸：PDF 平铺墨迹四边都在距页边 margin 处（≤2px，需求口径）。

    【QA 独立发现 - 源码缺陷】``render.render_overlay_layer`` 用**未缩放**字号的
    墨迹 bbox 计算摆放，却按 ``size*scale`` 光栅化块位图；PDF 路径固定
    ``PDF_RENDER_SCALE=2.0``，块内墨迹偏移与摆放不协变，导致短边较小 / 极端宽高比
    的页面贴边偏差达 ~3-4.6px；320×240、margin=1% 时顶行墨迹被推到页面边缘
    （dev≈-2.4pt，命中「完整不裁切 / 贴边」契约）。常见 A4/Letter/A5 仍 ≤2px。
    此断言即为此缺陷的护栏。
    """
    sizes = [(595, 842), (612, 792), (420, 595), (400, 400), (842, 1191),
             (320, 240), (300, 800), (200, 200)]
    spec = WatermarkSpec(text="机密文件", font_pct=4.0, margin_pct=3.0,
                         angle=30.0, opacity=1.0, color="#d32f2f").normalized()
    detail = []
    for width, height in sizes:
        margin = 3.0 / 100.0 * min(width, height)
        with tempfile.TemporaryDirectory() as tmp:
            src = os.path.join(tmp, "in.pdf")
            dst = os.path.join(tmp, "out.pdf")
            _blank_pdf(src, (width, height))
            render.render_pdf(src, dst, spec)
            blank = np.asarray(_render_pdf_page(src)).astype(np.int16)
            marked = np.asarray(_render_pdf_page(dst)).astype(np.int16)
        mask = np.abs(marked - blank).max(axis=2) >= DEV
        ys, xs = np.where(mask)
        assert len(xs) > 0, f"{width}x{height} 无水印"
        k = 144 / 72.0
        devs = [xs.min() / k - margin, ys.min() / k - margin,
                ((width * k - 1) - xs.max()) / k - margin,
                ((height * k - 1) - ys.max()) / k - margin]
        worst = max(abs(v) for v in devs)
        detail.append(f"{width}x{height} margin={margin:.1f} dev={[round(v, 2) for v in devs]} max={worst:.2f}")
        assert worst <= 2.0, f"{width}x{height} PDF 贴边超 2px：{detail[-1]}"
    assert True


# ===========================================================================
# C. PDF 专项
# ===========================================================================

def test_qa_pdf_alpha_preserved_corners():
    """opacity=0.2 浅色水印：PDF 空白角落仍接近纯白（alpha 未被当不透明整页覆盖），
    墨迹是被白底稀释的浅粉（≠ 实心红）。独立用「读回 PDF → 渲成图 → 取角落像素」验证。"""
    width, height = 595, 842
    spec = WatermarkSpec(text="机密文件", font_pct=6.0, margin_pct=5.0,
                         angle=0.0, opacity=0.2, color="#d32f2f").normalized()
    with tempfile.TemporaryDirectory() as tmp:
        src = os.path.join(tmp, "in.pdf")
        dst = os.path.join(tmp, "out.pdf")
        _blank_pdf(src, (width, height))
        render.render_pdf(src, dst, spec)
        src_arr = np.asarray(_render_pdf_page(src)).astype(np.int16)
        marked = np.asarray(_render_pdf_page(dst)).astype(np.int16)
    for coords in ((3, 3), (838, 3), (3, 591)):
        px = marked[coords]
        assert int(px.min()) >= 250, f"角落 {coords} 被覆盖，alpha 丢失：{tuple(int(v) for v in px)}"
    mask = np.abs(marked - src_arr).max(axis=2) >= 18
    ys, xs = np.where(mask)
    assert len(xs) > 0, "未检测到墨迹"
    r, g, b = (int(v) for v in np.median(marked[mask], axis=0))
    assert g > 150 and r > g >= b - 5, f"墨迹不像半透明红叠白：{(r, g, b)}"


def test_qa_pdf_mixed_size_pages_all_watermarked():
    """混合尺寸多页 PDF：每一页都要有水印且不越界。"""
    sizes = [(595, 842), (400, 400), (842, 595), (300, 800)]
    spec = WatermarkSpec(text="机密文件", font_pct=4.0, margin_pct=3.0,
                         angle=30.0, opacity=1.0, color="#d32f2f").normalized()
    with tempfile.TemporaryDirectory() as tmp:
        src = os.path.join(tmp, "in.pdf")
        dst = os.path.join(tmp, "out.pdf")
        _blank_pdf(src, sizes, pages=4)
        render.render_pdf(src, dst, spec)
        for index, (width, height) in enumerate(sizes):
            blank = np.asarray(_render_pdf_page(src, index)).astype(np.int16)
            marked = np.asarray(_render_pdf_page(dst, index)).astype(np.int16)
            mask = np.abs(marked - blank).max(axis=2) >= DEV
            ys, xs = np.where(mask)
            assert len(xs) > 0, f"混合尺寸第 {index + 1} 页（{width}x{height}）无水印"
            k = 144 / 72.0
            assert 0 <= xs.min() and xs.max() <= width * k, "水平越界"
            assert 0 <= ys.min() and ys.max() <= height * k, "垂直越界"


def test_qa_image_and_pdf_paths_agree_multi():
    """同尺寸（A4）下图片路径与 PDF 路径**每一页**的水印墨迹 bbox 偏差 ≤ 2px。"""
    width, height = 595, 842
    spec = WatermarkSpec(text="机密文件", font_pct=4.0, margin_pct=3.0,
                         angle=30.0, opacity=1.0, color="#d32f2f").normalized()
    img_bbox = _ink_bbox(render.render_image(_canvas(width, height), spec))
    assert img_bbox is not None
    with tempfile.TemporaryDirectory() as tmp:
        src = os.path.join(tmp, "in.pdf")
        dst = os.path.join(tmp, "out.pdf")
        _blank_pdf(src, (width, height), pages=2)
        render.render_pdf(src, dst, spec)
        k = 144 / 72.0  # _render_pdf_page 默认 dpi=144 -> 像素 = 2×点；折回点坐标再比对
        for index in range(2):
            blank = np.asarray(_render_pdf_page(src, index)).astype(np.int16)
            marked = np.asarray(_render_pdf_page(dst, index)).astype(np.int16)
            mask = np.abs(marked - blank).max(axis=2) >= DEV
            ys, xs = np.where(mask)
            pdf_bbox = (xs.min() / k, ys.min() / k, xs.max() / k, ys.max() / k)
            for a, b in zip(img_bbox, pdf_bbox):
                assert abs(a - b) <= 2, f"第{index + 1}页 两路径偏差 {abs(a-b)}: {img_bbox} vs {pdf_bbox}"


def test_qa_encrypted_pdf_raises_clear_error_no_output():
    """加密 PDF：渲染与取页图都必须抛出明确异常，且**不产生输出文件**（不静默跳过）。"""
    spec = WatermarkSpec().normalized()
    with tempfile.TemporaryDirectory() as tmp:
        enc = os.path.join(tmp, "enc.pdf")
        doc = fitz.open()
        doc.new_page(width=200, height=200)
        doc.save(enc, encryption=fitz.PDF_ENCRYPT_AES_256, owner_pw="o", user_pw="u")
        doc.close()
        dst = os.path.join(tmp, "enc_wm.pdf")
        raised = False
        try:
            render.render_pdf(enc, dst, spec)
        except Exception:
            raised = True
        assert raised, "加密 PDF 渲染未抛异常（可能静默产出垃圾）"
        assert not os.path.exists(dst), "加密 PDF 渲染留下了输出文件"
        doc2 = media.Document(enc)
        try:
            raised = False
            try:
                doc2.page_image(0)
            except Exception:
                raised = True
            assert raised, "加密 PDF 取页图未抛异常"
        finally:
            doc2.close()


def test_qa_zero_page_pdf_raises_clear_error_no_output():
    """0 页 PDF：渲染抛出明确异常且不留输出文件。"""
    spec = WatermarkSpec().normalized()
    with tempfile.TemporaryDirectory() as tmp:
        zero = os.path.join(tmp, "zero.pdf")
        _write_zero_page_pdf(zero)
        doc = media.Document(zero)
        try:
            assert doc.page_count == 0
            assert doc.kind == media.KIND_PDF
        finally:
            doc.close()
        dst = os.path.join(tmp, "zero_wm.pdf")
        raised = False
        try:
            render.render_pdf(zero, dst, spec)
        except Exception:
            raised = True
        assert raised, "0 页 PDF 渲染未抛异常"
        assert not os.path.exists(dst), "0 页 PDF 渲染留下了输出文件"


def test_qa_damaged_and_fake_inputs_raise():
    """损坏 PDF / 假扩展名 / 0 字节 / 不支持扩展名：Document 构造必须抛明确异常。"""
    with tempfile.TemporaryDirectory() as tmp:
        cases = {}
        p = os.path.join(tmp, "bad.pdf")
        with open(p, "wb") as f:
            f.write(b"%PDF-1.4 garbage not a real pdf")
        cases["损坏PDF"] = p
        p = os.path.join(tmp, "fake.png")
        with open(p, "w", encoding="utf-8") as f:
            f.write("not an image")
        cases["假PNG"] = p
        p = os.path.join(tmp, "zero.png")
        open(p, "wb").close()
        cases["0字节PNG"] = p
        p = os.path.join(tmp, "doc.txt")
        with open(p, "w", encoding="utf-8") as f:
            f.write("hi")
        cases["不支持扩展名"] = p
        for name, path in cases.items():
            raised = False
            try:
                d = media.Document(path)
                d.close()
            except Exception:
                raised = True
            assert raised, f"{name} 未抛异常"


# ===========================================================================
# D. 异常 / 边界输入
# ===========================================================================

def test_qa_select_file_handles_empty_or_encrypted_pdf():
    """选文件（预览）路径：0 页 / 加密 PDF 不得冒出未捕获异常，且应给出错误提示。

    【QA 独立发现 - 源码缺陷】``wm/ui/app.py::App._select_file`` 把
    ``media.Document(...)`` 包在 try 里，但紧随其后的 ``self.doc.page_size(0)``
    **在 try 之外**：0 页 PDF 抛 IndexError、加密 PDF 抛 ValueError，均直达 Tk 回调，
    用户看不到任何错误提示（预览停在原态）。此处断言「不抛 + 显示 error 占位」。
    """
    root = _new_root()
    try:
        from wm.ui import app as ui_app
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            zero = os.path.join(tmp, "zero.pdf")
            _write_zero_page_pdf(zero)
            enc = os.path.join(tmp, "enc.pdf")
            doc = fitz.open()
            doc.new_page(width=200, height=200)
            doc.save(enc, encryption=fitz.PDF_ENCRYPT_AES_256, owner_pw="o", user_pw="u")
            doc.close()

            application = ui_app.App(root)
            root.update_idletasks()
            for path in (zero, enc):
                application.files = [path]
                application._current_index = -1
                raised = False
                detail = ""
                try:
                    application._select_file(0)
                except Exception as exc:  # noqa: BLE001
                    raised = True
                    detail = f"{type(exc).__name__}: {exc}"
                root.update_idletasks()
                if application.doc is not None:  # 失败路径可能留下半开文档，先收尾
                    application.doc.close()
                    application.doc = None
                assert not raised, (
                    f"_select_file 对 {os.path.basename(path)} 冒出未捕获异常（{detail}）；"
                    "应捕获并显示错误占位")
                assert application.preview._placeholder[0] == "error", (
                    f"{os.path.basename(path)} 未显示错误占位，实际={application.preview._placeholder}")
    finally:
        root.destroy()


def test_qa_edge_specs_never_crash():
    """边界参数渲染：不抛未捕获异常、不产出 0 字节、尺寸正确。"""
    base_w, base_h = 600, 400
    before = _canvas(base_w, base_h).convert("RGBA").tobytes()
    cases = [
        ("超长500汉字", "密" * 500, 4.0, 3.0, 30.0, 0.3, "#d32f2f"),
        ("emoji+换行", "机密😀\n文件", 4.0, 3.0, 30.0, 0.3, "#d32f2f"),
        ("单字符", "密", 4.0, 3.0, 30.0, 0.3, "#d32f2f"),
        ("全空格", "   ", 4.0, 3.0, 30.0, 0.3, "#d32f2f"),
        ("margin0", "机密", 4.0, 0.0, 30.0, 0.3, "#d32f2f"),
        ("margin20", "机密", 4.0, 20.0, 30.0, 0.3, "#d32f2f"),
        ("font30>页面", "机密", 30.0, 3.0, 30.0, 0.3, "#d32f2f"),
        ("opacity0.05", "机密", 4.0, 3.0, 30.0, 0.05, "#d32f2f"),
        ("opacity1.0", "机密", 4.0, 3.0, 30.0, 1.0, "#d32f2f"),
        ("angle0", "机密", 4.0, 3.0, 0.0, 0.3, "#d32f2f"),
        ("angle90", "机密", 4.0, 3.0, 90.0, 0.3, "#d32f2f"),
        ("angle180", "机密", 4.0, 3.0, 180.0, 0.3, "#d32f2f"),
        ("angle270", "机密", 4.0, 3.0, 270.0, 0.3, "#d32f2f"),
        ("angle360", "机密", 4.0, 3.0, 360.0, 0.3, "#d32f2f"),
        ("color#FFF", "机密", 4.0, 3.0, 30.0, 0.3, "#FFF"),
        ("color red", "机密", 4.0, 3.0, 30.0, 0.3, "red"),
        ("color#GGGGGG", "机密", 4.0, 3.0, 30.0, 0.3, "#GGGGGG"),
        ("color empty", "机密", 4.0, 3.0, 30.0, 0.3, ""),
    ]
    for name, text, font_pct, margin_pct, angle, opacity, color in cases:
        spec = WatermarkSpec(text=text, font_pct=font_pct, margin_pct=margin_pct,
                             angle=angle, opacity=opacity, color=color).normalized()
        out = render.render_image(_canvas(base_w, base_h), spec)
        assert out.size == (base_w, base_h), f"{name} 尺寸变了：{out.size}"
        assert isinstance(spec.color, str) and spec.color.startswith("#") and len(spec.color) == 7, name
    # 原画布未被就地修改
    assert _canvas(base_w, base_h).convert("RGBA").tobytes() == before


def test_qa_odd_image_formats_render_and_save():
    """16bit 灰度 PNG / CMYK JPEG / 带 alpha PNG / 1×1 / WebP：均可处理且不产 0 字节文件。"""
    from wm.ui.app import App  # 复用真实保存路径（按扩展名压平 alpha）
    with tempfile.TemporaryDirectory() as tmp:
        spec = WatermarkSpec(text="机密", angle=0.0, opacity=0.6).normalized()
        sources = []
        p = os.path.join(tmp, "g16.png")
        Image.new("I;16", (200, 150), 30000).save(p)
        sources.append(p)
        p = os.path.join(tmp, "cmyk.jpg")
        Image.new("CMYK", (200, 150), (0, 0, 0, 0)).save(p)
        sources.append(p)
        p = os.path.join(tmp, "rgba.png")
        Image.new("RGBA", (200, 150), (255, 0, 0, 128)).save(p)
        sources.append(p)
        p = os.path.join(tmp, "one.png")
        Image.new("RGB", (1, 1), WHITE).save(p)
        sources.append(p)
        p = os.path.join(tmp, "pic.webp")
        Image.new("RGB", (200, 150), WHITE).save(p)
        sources.append(p)
        for src in sources:
            doc = media.Document(src)
            try:
                out = render.render_image(doc.page_image(0), spec)
                dst = media.plan_output(src)
                App._save_image(out, dst)
            finally:
                doc.close()
            assert os.path.exists(dst) and os.path.getsize(dst) > 0, f"{src} 产出 0 字节"


def test_qa_repeat_process_increments_and_source_unchanged():
    """重复处理同一文件：输出名自动递增 _watermarked(1)/_watermarked(2)...；原文件哈希不变。"""
    with tempfile.TemporaryDirectory() as tmp:
        src = os.path.join(tmp, "photo.png")
        Image.new("RGB", (100, 80), WHITE).save(src)
        before = hashlib.md5(open(src, "rb").read()).hexdigest()
        spec = WatermarkSpec(text="机密", angle=0.0).normalized()
        names = []
        for _ in range(3):
            doc = media.Document(src)
            try:
                out = render.render_image(doc.page_image(0), spec)
                dst = media.plan_output(src)
                out.save(dst)
                names.append(os.path.basename(dst))
            finally:
                doc.close()
        after = hashlib.md5(open(src, "rb").read()).hexdigest()
        assert names == ["photo_watermarked.png", "photo_watermarked(1).png",
                         "photo_watermarked(2).png"], names
        assert before == after, "原文件被修改"


# ===========================================================================
# E. 批量与取消
# ===========================================================================

def test_qa_batch_isolation_and_monotonic_progress():
    """批量 13 个混合文件（含 1 个坏文件）：进度单调不减、坏文件不中断其余、失败有明确报错。"""
    from wm.ui import app as ui_app
    with tempfile.TemporaryDirectory() as tmp:
        files = []
        for i in range(12):
            if i % 3 == 0:
                p = os.path.join(tmp, f"img{i}.png")
                Image.new("RGB", (200, 150), WHITE).save(p)
            else:
                p = os.path.join(tmp, f"doc{i}.pdf")
                _blank_pdf(p, (300, 400), pages=2)
            files.append(p)
        bad = os.path.join(tmp, "bad.png")
        with open(bad, "w", encoding="utf-8") as f:
            f.write("not an image")
        files.insert(5, bad)

        app = ui_app.App.__new__(ui_app.App)
        app._cancel = False
        app._queue = _queue.Queue()
        spec = WatermarkSpec(text="机密", angle=30.0).normalized()
        app._batch_worker(files, spec, tmp)

        items = []
        while True:
            try:
                items.append(app._queue.get_nowait())
            except _queue.Empty:
                break
        progress = [it["value"] for it in items if it["kind"] == "progress"]
        done = [it for it in items if it["kind"] == "done"]
        logs = [it["text"] for it in items if it["kind"] == "log"]
        assert progress, "无进度回调"
        assert all(progress[i] <= progress[i + 1] + 1e-9 for i in range(len(progress) - 1)), \
            "进度非单调不减"
        assert done and done[0]["succeeded"] == 12, f"成功数不对：{done}"
        assert len(done[0]["failed"]) == 1, f"失败列表不对：{done[0]['failed']}"
        assert any(t.startswith("[FAIL]") and "bad.png" in t for t in logs), "坏文件无明确失败日志"
        produced = [n for n in os.listdir(tmp) if "_watermarked" in n]
        assert len(produced) == 12, f"产出文件数不对：{len(produced)}"


def test_qa_cancel_stops_fast_and_leaves_no_partial_file():
    """处理中途取消：能较快停下、不留半截损坏文件。"""
    spec = WatermarkSpec(text="机密").normalized()
    with tempfile.TemporaryDirectory() as tmp:
        src = os.path.join(tmp, "big.pdf")
        dst = os.path.join(tmp, "big_wm.pdf")
        _blank_pdf(src, (595, 842), pages=200)
        state = {"n": 0}

        def is_cancelled():
            state["n"] += 1
            return state["n"] > 5

        started = time.time()
        cancelled = False
        try:
            render.render_pdf(src, dst, spec, is_cancelled=is_cancelled)
        except render.Cancelled:
            cancelled = True
        elapsed = time.time() - started
        assert cancelled, "取消未被响应"
        assert elapsed < 5.0, f"取消响应过慢：{elapsed:.2f}s"
        assert not os.path.exists(dst), "取消后留下了半截文件"


# ===========================================================================
# F. 界面行为
# ===========================================================================

def test_qa_ui_defaults_and_no_position_entry():
    """默认态符合规格；界面**没有任何**位置调节入口（平铺是唯一模式）。

    覆盖：默认参数值、参数面板无平铺开关 / 无位置区块 / 无重置居中按钮、
    预览无命中框接口、边距文案为平铺语义、平铺交代文案控件（``tile_hint``）
    已彻底移除、打开失败后仍显示错误占位。
    """
    import tkinter as tk
    root = _new_root()
    try:
        from wm.ui import app as ui_app
        application = ui_app.App(root)
        panel = application.panel
        root.update_idletasks()

        # -- 默认态 --
        spec = panel.spec()
        assert spec.font_pct == 4.0 and spec.margin_pct == 3.0 and spec.angle == 30.0
        assert abs(spec.opacity - 0.30) < 1e-9 and spec.color == "#d32f2f"
        assert spec.text == "机密文件"

        # -- 无残留入口 --
        assert not hasattr(panel, "tile_switch"), "平铺开关残留"
        assert not hasattr(panel, "pos_box"), "位置调节区块残留"
        assert not hasattr(panel, "pos_anchor"), "位置区块锚点残留"
        assert not hasattr(panel, "reset_btn"), "重置居中按钮残留"
        assert not hasattr(panel, "set_offset_pct"), "偏移写入接口残留"
        assert not hasattr(application.preview, "set_hit_rect"), "预览命中框接口残留"
        assert not hasattr(application, "_on_drag_delta"), "拖拽回调残留"
        # 面板内不得再出现「位置调节 / 偏移 / 拖动」字样
        for slave in panel._fields.pack_slaves():
            texts = [str(label.cget("text")) for label in _iter_labels(slave)]
            assert not any("位置调节" in t or "偏移" in t or "拖动" in t for t in texts), \
                f"残留位置调节文案：{texts}"

        # -- 文案 --
        assert "留白" in panel.margin_pct.hint_label.cget("text"), "边距文案应为平铺语义"
        assert not hasattr(panel, "tile_hint"), "平铺交代文案控件应已移除"

        # -- 「恢复默认」：一键把**参数**拽回出厂值（与位置无关）--
        # 属性名必须是 ``defaults_btn``：``reset_btn`` 是历史「重置居中」的名字，
        # 被上面的位置调节护栏列为禁止项，两者混同会让护栏失效。
        from wm.spec import WatermarkSpec
        assert hasattr(panel, "defaults_btn"), "缺少「恢复默认」按钮"
        panel.font_pct.set(20.0, notify=True)
        panel.opacity.set(80.0, notify=True)
        assert panel.spec().font_pct != WatermarkSpec.default().font_pct, "前置：参数已被改动"
        panel._on_reset_defaults()
        after = panel.spec()
        assert after == WatermarkSpec.default(), \
            f"恢复默认没有回到出厂参数：{after}"
        # 界面也要跟着回到默认值（不能停在「显示旧值、渲染用新值」的错位上）
        assert panel.font_pct.get() == WatermarkSpec.default().font_pct
        assert panel.opacity.get() == 30.0

        # -- 打开失败仍要显示错误占位 --
        application.files = ["__not_existing__.pdf"]
        application._current_index = -1
        application._select_file(0)  # 打开失败
        root.update_idletasks()
        assert application.preview._placeholder[0] == "error", "失败未显示错误占位"
    finally:
        root.destroy()


def _iter_labels(widget):
    """递归取出 widget 子树里所有带 text 选项的控件（用于扫描残留文案）。"""
    stack = [widget]
    while stack:
        current = stack.pop()
        try:
            if "text" in current.keys():
                yield current
        except tk.TclError:
            continue
        try:
            stack.extend(current.winfo_children())
        except tk.TclError:
            continue


def test_qa_ui_preview_refreshes_after_param_change():
    """参数调节即时生效：改字号后事件循环内能拿到新预览帧（<3s）。"""
    import tkinter as tk
    root = _new_root()
    try:
        from wm.ui import app as ui_app
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "a.png")
            Image.new("RGB", (400, 300), WHITE).save(path)
            application = ui_app.App(root)
            application.add_files([path])
            root.geometry("1120x740")
            root.update_idletasks()
            for _ in range(80):
                root.update()
                time.sleep(0.01)
            before = application._preview_gen
            application.panel.font_pct.set(12.0, notify=True)
            started = time.time()
            refreshed = False
            for _ in range(300):
                root.update()
                time.sleep(0.01)
                if application._preview_gen > before and application.preview._pil is not None:
                    refreshed = True
                    break
            elapsed = time.time() - started
            # 避免 teardown 噪音：停止后续 after 重排
            root.after = lambda *a, **k: "stub"  # type: ignore[assignment]
            assert refreshed, "改参数后未在 3s 内拿到新预览帧"
            assert elapsed < 3.0, f"预览刷新过慢：{elapsed:.2f}s"
    finally:
        root.destroy()


def test_qa_dnd_splitlist_parsing():
    """拖拽路径串解析：带空格的路径用 {} 包裹时能被正确切分（多文件）。"""
    root = _new_root()
    try:
        parsed = list(root.tk.splitlist("{C:/a b/x.png} C:/d/y.pdf"))
        assert parsed == ["C:/a b/x.png", "C:/d/y.pdf"], parsed
        parsed2 = list(root.tk.splitlist("{C:/含 空格/中文.pdf}"))
        assert parsed2 == ["C:/含 空格/中文.pdf"], parsed2
    finally:
        root.destroy()


# ===========================================================================
# G. 性能
# ===========================================================================

def test_qa_performance_reasonable():
    """性能量级：4000×3000 平铺 / 50 页 A4 PDF 平铺 / 极小字号块数保护，均不卡死。"""
    spec = WatermarkSpec(text="机密文件", font_pct=4.0, margin_pct=3.0,
                         angle=30.0, opacity=0.3).normalized()
    t0 = time.time()
    out = render.render_image(_canvas(4000, 3000), spec)
    t_img = time.time() - t0
    assert out.size == (4000, 3000)
    assert t_img < 20.0, f"4000x3000 平铺过慢：{t_img:.2f}s"

    with tempfile.TemporaryDirectory() as tmp:
        src = os.path.join(tmp, "big.pdf")
        dst = os.path.join(tmp, "big_wm.pdf")
        _blank_pdf(src, (595, 842), pages=50)
        t0 = time.time()
        pages = render.render_pdf(src, dst, spec)
        t_pdf = time.time() - t0
        assert pages == 50
        assert t_pdf < 30.0, f"50 页 A4 平铺过慢：{t_pdf:.2f}s"

    spec_small = WatermarkSpec(text="X", font_pct=0.5, margin_pct=0.0,
                               angle=45.0).normalized()
    t0 = time.time()
    render.render_image(_canvas(4000, 4000), spec_small)
    t_guard = time.time() - t0
    assert t_guard < 20.0, f"块数保护未生效/过慢：{t_guard:.2f}s"
    print(f"      [perf] 4000x3000={t_img:.2f}s 50页A4={t_pdf:.2f}s 4000x4000(font0.5%)={t_guard:.2f}s")
