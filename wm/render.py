"""渲染路径：图片（Pillow）与 PDF（PyMuPDF）。

两条路径共用 ``layout`` 的同一份版式结果，区别只在最终「合成」：

    * 图片：把透明水印层 alpha 合成到原图。
    * PDF ：把透明水印层作为**一张** overlay 图片插入页面的 ``page.rect``。

统一走「透明全页 RGBA 水印层」的好处：
    * 版式与图片路径逐像素同构；
    * 避免几千次矢量文字绘制，性能与 PDF 体积都更好；
    * 不同尺寸页面分别缓存水印层，同尺寸多页复用同一 Pixmap
      （缓存按**字节预算**限流，见 :data:`PDF_LAYER_CACHE_BYTES`）。

⚠️ PDF 图层缓存里存的是 ``fitz.Pixmap`` 而不是 PIL Image，按尺寸缓存时**每差 1pt
就是一条**（扫描件常见），旧实现永不释放 —— 实测 60 页各差 1pt：峰值工作集增量
1043.8MB。故照 :mod:`wm.layout` 的字节预算做法限流。

所有尺度参数都是「页面短边百分比」。**版式恒在原生的 1:1 页面坐标系里计算**
（块数、间距、贴边都不随 ``scale`` 变），``scale`` 只影响块位图的光栅倍率：

    * :func:`_render_crisp_layer` -> 块位图按 ``scale`` 光栅化（清晰），并按其自身
      墨迹 bbox 对齐 -> 四边贴边在任意 ``scale`` 下精确不漂移（bug 1 修复）。
    * :func:`render_output_layer` 用它做**输出层**：图片 = 1、PDF = 2。
    * :func:`render_overlay_layer` 是**预览 / 通用**入口：整层按 1:1 渲染后整体缩放到
      ``page*scale`` -> 字形**密度**与全分辨率输出一致，预览即所见（WYSIWYG，bug 2）。

⚠️ 为什么版式不能用「缩放后的坐标系」重算：``axis_starts`` 的 ``k = floor(span/step)``
不连续，重算会让块数跨整数边界变化（实测 900×600 / font4% / m3% / 45°：1× 六列、
2× 七列），使图片路径与 PDF 路径的平铺网格不一致。

⚠️ 最高风险点：RGBA → ``fitz.Pixmap`` 必须保留 alpha 通道，否则会表现为
「整页被不透明层盖住」。已实测 PyMuPDF 1.28 ``fitz.Pixmap(png_bytes)`` 得到
``n=4, alpha=1``，插入后半透明红叠白底呈粉色而非纯红（见 tests），**必须**
保留这条回归用例。
"""

from __future__ import annotations

import io
import math
import os
import time
from typing import Callable, Optional, Tuple

import pymupdf as fitz  # PyMuPDF（``import fitz`` 自 1.28 起已弃用，将来会 ImportError）
from PIL import Image

from . import layout
from .lru import SizedLRU
from .spec import WatermarkSpec

#: **图片**路径的**小图**光栅倍率（超采样后降采样回原尺寸，边缘更平滑）。
#:
#: 实测超采样的**收益很小**：1× 与「2× 再降回 1×」的逐像素差仅 0.357/255（0.14%）、
#: 覆盖率差 0.075pp。但它的**代价很大**：8000×8000 上「2×→1× 的 LANCZOS 降采样」
#: 单独就要 5.13s（占总耗时 77%），峰值内存 3.3GB。
#:
#: 所以改成**按像素预算自适应**：小图（≤ :data:`IMAGE_SS_MAX_PIXELS`）保留 2× 保住
#: 与 PDF 路径一致的边缘精度，大图降到 1× 换取 10 倍提速与数 GB 内存。
#:
#: 能这么改的前提：**版式恒在原生 1:1 坐标系计算**，倍率只影响光栅，不动块数与
#: 间距，因此不会破坏「图片 = PDF」「预览 = 输出」两条契约。
IMAGE_RENDER_SCALE = 2.0
#: 图片超采样的像素预算：源图超过这个像素数就直接用 1×（8MP ≈ 4000×2000，
#: 2× 层 = 32M 像素 ≈ 128MB RGBA，是能接受的上限）
IMAGE_SS_MAX_PIXELS = 8_000_000
#: 图片路径超过此像素数（目标尺寸）就按「水平带」合成，避免整层 RGBA 驻留。
IMAGE_BAND_PIXELS = 16_000_000
#: 每条带的行数；峰值内存 ≈ 带宽 × 宽 × 4 字节。
IMAGE_BAND_ROWS = 1024
#: **PDF** 路径的光栅倍率（72 dpi * 2 = 144 dpi）。打印需要更高分辨率，保留 2×。
#:
#: ⚠️ 这个 2× 是**上限**，不是固定值：大页面会按 :data:`PDF_SS_MAX_PIXELS` 自动
#: 降倍率（见 :func:`pdf_render_scale`）—— 实测 A0 单页固定 2× 要 2.44s、PDF 体积
#: +873KB，而 A4 只要 +47KB，把 A0 的倍率降到 1× 后两者都回到 A4 量级。
PDF_RENDER_SCALE = 2.0
#: PDF 图层的**像素预算**：单个 2× 图层超过它就降倍率。取 8MP 与图片路径的
#: :data:`IMAGE_SS_MAX_PIXELS` 对齐（A4 2× 约 2MP 不受影响；A0 2× 约 32MP 降到 1×）。
PDF_SS_MAX_PIXELS = 8_000_000
#: PDF 光栅倍率下限。低于 1× 就是「比 72dpi 还粗」，打印会明显发虚，不再往下探。
PDF_MIN_RENDER_SCALE = 1.0
#: 倍率量化步长：相邻尺寸（扫描件常见的 1pt 抖动）必须落到**同一个**倍率上，
#: 否则缓存每条尺寸一套、命中率归零。0.5 一档足够，也更稳。
PDF_SCALE_QUANTUM = 0.5

#: PDF 图层缓存（``fitz.Pixmap``）的**字节**预算。
#:
#: 与 :data:`wm.layout._RENDER_CACHE_BYTES` 同一套做法：只按**条数**限流没有意义 ——
#: 单条 A4 图层在 2× 光栅下就约 7.6MB（1190×1684×4 ≈ 8015840 B），A0 更是约 122MB。
#: 缓存键含页面视觉尺寸，**每差 1pt 就是一条**（扫描件极常见），旧实现永不释放：
#:
#:     * 60 页同尺寸 A4：只 1 条，+29MB（没问题）；
#:     * 60 页**各差 1pt**：旧实现 60 条全留 —— 累计创建 498.6MB、峰值工作集增量
#:       1043.8MB（审计基线 +1035MB）；加预算后驻留 251.2MB（29 条）、峰值增量
#:       787.9MB（余下的是 MuPDF 自己为 60 张不同图层图保留的文档侧数据），
#:       耗时 10.24s -> 9.88s；
#:     * 10 页 A0 各差 1pt：旧实现 +2623MB / 22.2s（审计基线）。
PDF_LAYER_CACHE_BYTES = 256 * 1024 * 1024

ProgressFn = Callable[[int, int, str], None]
CancelFn = Callable[[], bool]


def pdf_render_scale(page_w: float, page_h: float, scale: float = PDF_RENDER_SCALE) -> float:
    """按页面尺寸把 PDF 光栅倍率压到像素预算内（**版式不变，只降光栅密度**）。

    能这么做的前提与图片路径那条一样：版式恒在原生 1:1 页面坐标系里算（块数 /
    间距 / 贴边都不随倍率变），倍率只影响块位图的光栅密度 —— 所以降倍率不会破坏
    「图片 = PDF」「预览 = 输出」两条契约，代价只是水印字的边缘略柔。

    倍率**向下量化**到 :data:`PDF_SCALE_QUANTUM`：相邻尺寸（扫描件常差 1pt）落到
    同一档，缓存才不会每页一条。

        A4 595×842（0.5MP）-> 2.0（不变，打印精度优先）
        A1 1684×2384（4.0MP）-> 1.0
        A0 3370×2384（8.0MP）-> 1.0（旧行为固定 2.0，实测 2.44s / +873KB）
    """
    base = max(PDF_MIN_RENDER_SCALE, float(scale))
    area = float(page_w) * float(page_h)
    if area <= 0:
        return base
    affordable = math.sqrt(PDF_SS_MAX_PIXELS / area)
    if affordable >= base:
        return base
    steps = int(math.floor(affordable / PDF_SCALE_QUANTUM))
    return max(PDF_MIN_RENDER_SCALE, steps * PDF_SCALE_QUANTUM)


class Cancelled(Exception):
    """用户取消批处理时抛出。"""


def _cancel_guard(is_cancelled: Optional[CancelFn]) -> Callable[[], None]:
    """把「可选的取消回调」包成**必存**的零参检查函数。

    没传回调时返回一个空操作，这样调用点可以无条件写 ``check()``，不必到处写
    ``if is_cancelled is not None and is_cancelled()``。
    """
    if is_cancelled is None:
        return lambda: None

    def check() -> None:
        if is_cancelled():
            raise Cancelled()

    return check


# ---------------------------------------------------------------------------
# 合成原语
# ---------------------------------------------------------------------------

def _blit(layer: Image.Image, block: Image.Image, x: float, y: float) -> None:
    """把 ``block`` 以 alpha 合成到 ``layer`` 上（自动裁剪到层内区域）。

    直接给 ``alpha_composite`` 传越界坐标在部分 Pillow 版本下不受支持，这里先
    把块裁到层内区域再就地合成，稳妥且不丢 alpha。
    """
    lw, lh = layer.size
    bw, bh = block.size
    dx0, dy0 = int(round(x)), int(round(y))
    ix0, iy0 = max(0, dx0), max(0, dy0)
    ix1, iy1 = min(lw, dx0 + bw), min(lh, dy0 + bh)
    if ix1 <= ix0 or iy1 <= iy0:
        return
    src = block.crop((ix0 - dx0, iy0 - dy0, ix1 - dx0, iy1 - dy0))
    layer.alpha_composite(src, dest=(ix0, iy0))


def _prepare_layer(
    native_w: float,
    native_h: float,
    spec: WatermarkSpec,
    scale: float,
    is_cancelled: Optional[CancelFn] = None,
):
    """计算版式 + 块位图（与 :func:`_render_crisp_layer` 同一份），返回供合成复用。

    把「版式 + 块位图准备」从整层渲染里抽出来，让**整层路径**与**按带路径**共用
    完全相同的 ``placements`` / ``ink_block`` —— 二者像素级一致的前提。返回
    ``None`` 表示零尺寸（调用方应回退为空白层）。

    返回元组：``(placements, ink_block, ix0, iy0, native_w, native_h)``。
    """
    check = _cancel_guard(is_cancelled)
    check()
    if native_w <= 0 or native_h <= 0:
        return None
    # 版式（块数 / 间距 / 各块墨迹起点）恒在原生坐标系计算 —— 两路径共享同一网格
    size = layout.font_px(spec, native_w, native_h)
    (block_w, block_h), ink = layout.block_geometry(spec, size)
    placements = layout.compute_placements(native_w, native_h, block_w, block_h, spec, ink)
    ix0, iy0 = float(ink[0]), float(ink[1])
    ink_w = max(1.0, float(ink[2]) - float(ink[0]))
    ink_h = max(1.0, float(ink[3]) - float(ink[1]))

    # 块位图按缩放后字号光栅化（清晰）；再**裁到墨迹 bbox 并精确拉伸到 scale×原生墨迹**。
    # 必须裁+拉伸而不能只按「缩放块自身 bbox」对齐：FreeType 的字形外接框并非严格线性
    # 缩放（实测 A4 font4%：原生 ink 宽 89 → 2× 实际 174 ≠ 178），只对齐一条边会让对面
    # 边漂移约 2pt，破坏「贴边 ≤2px」。裁到 ink 后拉伸到 round(ink*scale)，两条边都精确。
    block = layout._render_block_bitmap(spec, size * scale)
    check()  # 块位图渲染可能是**最贵**的一步（实测单块可达 4.73s）
    bbox = block.getchannel("A").getbbox()
    ink_block = block.crop(bbox) if bbox else block
    target_wh = (max(1, int(round(ink_w * scale))), max(1, int(round(ink_h * scale))))
    if ink_block.size != target_wh:
        ink_block = ink_block.resize(target_wh, Image.LANCZOS)
    return (placements, ink_block, ix0, iy0, native_w, native_h)


def _render_crisp_layer(
    page_w: float,
    page_h: float,
    spec: WatermarkSpec,
    scale: float,
    is_cancelled: Optional[CancelFn] = None,
) -> Image.Image:
    """渲染透明水印层：**版式恒按原生（1:1）页面坐标系计算**，只有块位图按
    ``scale`` 光栅化并按其**自身墨迹 bbox** 对齐。

    为什么版式必须用原生坐标系（关键）：``axis_starts`` 里 ``k = floor(span/step)``
    是**不连续**的；若在「缩放后的坐标系」里重算，块尺寸/边距的取整误差会让
    ``span/step`` 跨过整数边界 -> 块数变化（实测 900×600 / font4% / m3% / 45°：
    1× 得 6 列、2× 得 7 列）。于是「图片路径」与「PDF 路径」的平铺网格会不一致。
    改为「版式恒用原生、仅块位图缩放」后，两路径块数与间距**逐块一致**。

    块的对齐：``blit = 墨迹起点 × scale − 缩放块的墨迹偏移``，于是缩放块渲染出的
    墨迹边 = ``原生墨迹边 × scale``，四边贴边在任意 ``scale`` 下都精确不漂移 ——
    这正是 bug 1（原按未缩放墨迹摆放却按缩放字号光栅化，导致贴边漂移/触边）的修复。

    ``is_cancelled``：可选的取消回调。实测各步骤耗时（Windows / 默认字体）：
    平铺循环本身很便宜（8000×8000：24 块 0.22s、156 块 0.05s），真正的大头是
    **平铺之前**的块位图渲染（8000×8000 + font 30% 单块 4.73s）与整层画布分配。
    故检查点设在「块位图渲染后 / 平铺循环里每 64 块一次」—— 后者在
    ``layout.MAX_TILES``（4000 块）上限下最多 63 次调用，开销可忽略。
    """
    layer_w = max(1, int(round(page_w * scale)))
    layer_h = max(1, int(round(page_h * scale)))
    layer = Image.new("RGBA", (layer_w, layer_h), (0, 0, 0, 0))
    if page_w <= 0 or page_h <= 0:
        return layer

    prepared = _prepare_layer(page_w, page_h, spec, scale, is_cancelled)
    if prepared is None:
        return layer
    placements, ink_block, ix0, iy0, _w, _h = prepared
    check = _cancel_guard(is_cancelled)
    for count, (bx, by) in enumerate(placements):
        if count % 64 == 0:  # 每 64 块查一次：循环本身很便宜，别每块都调
            check()
        _blit(layer, ink_block, (bx + ix0) * scale, (by + iy0) * scale)
    return layer


def _composite_banded(
    base: Image.Image,
    native_w: float,
    native_h: float,
    spec: WatermarkSpec,
    scale: float,
    is_cancelled: Optional[CancelFn] = None,
) -> Image.Image:
    """把水印按水平带直接合成到 ``base``（RGBA，已是目标尺寸），与
    ``Image.alpha_composite(base, 整层)`` 逐像素一致，但峰值只与单条带有关。

    仅在 ``factor == 1`` 的大图路径调用；带之间互不重叠，故结果与整层合成完全相同。
    """
    prepared = _prepare_layer(native_w, native_h, spec, scale, is_cancelled)
    if prepared is None:
        return base
    placements, ink_block, ix0, iy0, _w, _h = prepared
    bw, bh = ink_block.size
    target_w, target_h = base.size
    check = _cancel_guard(is_cancelled)
    for y0 in range(0, target_h, IMAGE_BAND_ROWS):
        check()
        y1 = min(y0 + IMAGE_BAND_ROWS, target_h)
        band_layer = Image.new("RGBA", (target_w, y1 - y0), (0, 0, 0, 0))
        for (bx, by) in placements:
            ty = (by + iy0) * scale
            if ty + bh < y0 or ty > y1:
                continue
            _blit(band_layer, ink_block, (bx + ix0) * scale, ty - y0)
        base_band = base.crop((0, y0, target_w, y1))
        # 注意：paste 必须带 (0, y0) 这个 box，否则默认贴到左上角 (0,0)，
        # 后续各带会覆盖第 0 带、导致整图结果与整层合成不一致（像素级不等价）。
        base.paste(Image.alpha_composite(base_band, band_layer), (0, y0))
    return base


def render_overlay_layer(
    page_w: float, page_h: float, spec: WatermarkSpec, scale: float = 1.0,
    is_cancelled: Optional[CancelFn] = None,
) -> Image.Image:
    """预览 / 通用入口：**整层按 1:1 渲染后整体缩放到 ``page*scale``**。

    这样字形**密度**与「全分辨率输出再缩放到同一尺寸」完全一致 —— 预览即所见
    （WYSIWYG）。若改为按缩放后字号直接光栅化块，短边小 / 缩放比极端时字形密度
    会偏移（墨迹覆盖率偏差可达约 4–6pp）。输出路径用 :func:`render_output_layer`
    以保持清晰。``scale == 1`` 时与输出层逐像素一致。

    ``is_cancelled``：可选的取消回调（预览用）。与输出路径同源、同一套语义 ——
    一旦返回 True 立刻抛 :class:`Cancelled`，调用方负责把它翻译成「这一帧作废」。
    """
    page_w = float(page_w)
    page_h = float(page_h)
    scale = max(1e-3, float(scale))
    layer_w = max(1, int(round(page_w * scale)))
    layer_h = max(1, int(round(page_h * scale)))
    if page_w <= 0 or page_h <= 0:
        return Image.new("RGBA", (layer_w, layer_h), (0, 0, 0, 0))

    native = _render_crisp_layer(page_w, page_h, spec, 1.0, is_cancelled=is_cancelled)
    if native.size == (layer_w, layer_h):
        return native
    return native.resize((layer_w, layer_h), Image.LANCZOS)


def render_output_layer(
    page_w: float, page_h: float, spec: WatermarkSpec, scale: float = 1.0
) -> Image.Image:
    """输出入口用的透明水印层：**清晰优先**（版式原生、块位图按 ``scale`` 光栅化）。

    图片输出 ``scale == 1``，与 :func:`render_overlay_layer` 逐像素一致；
    PDF 输出 ``scale = PDF_RENDER_SCALE``，块位图按放大字号光栅化以保持锐利，
    块数与间距仍与图片路径**逐块一致**（版式恒按原生坐标系计算）。
    """
    return _render_crisp_layer(float(page_w), float(page_h), spec,
                               max(1e-3, float(scale)))


# ---------------------------------------------------------------------------
# 图片路径
# ---------------------------------------------------------------------------

def render_image(
    img: Image.Image,
    spec: WatermarkSpec,
    scale: float = 1.0,
    is_cancelled: Optional[CancelFn] = None,
) -> Image.Image:
    """给 ``img`` 加水印并返回新的 RGBA 图（输出路径）。

    光栅倍率**按源图像素数自适应**（见 :data:`IMAGE_SS_MAX_PIXELS`）：小图 2× 超采样
    保边缘精度，大图 1× 换性能与内存。

    「图片 = PDF」的一致性**不依赖光栅倍率**：两条路径的版式都恒在原生 1:1 页面
    坐标系计算，倍率只影响块位图的清晰度。PDF 因打印需要始终用 2×。

    ``is_cancelled``
        可选的取消回调（:data:`CancelFn`）。在**真正耗时的步骤之间**检查，一旦
        回调返回 True 立即抛 :class:`Cancelled`。默认 ``None`` 表示不检查，
        既有调用方（预览 / 单张导出）行为完全不变。

        检查点的位置由实测决定（Windows / 默认字体 / 文本「机密文件」）：

        * 2000×2000（4MP，走 2× 超采样）：``layer.resize``（LANCZOS 降回 1×）
          0.29s / 总 0.51s —— **单一最贵的一步**；
        * 8000×8000（64MP，1×）：``convert`` 0.09s、``_render_crisp_layer`` 0.24s、
          ``alpha_composite`` 0.09s；
        * 平铺循环本身只有 24–156 块、0.05–0.22s，不值得每块都查（改在
          :func:`_render_crisp_layer` 里每 64 块查一次）。

        故检查点设在：进入时 / convert 后 / resize 后 / 水印层渲染后 / 合成前。
        取消一律在**返回之前**抛出，本函数不写任何文件，因此不会留下半成品输出。
    """
    check = _cancel_guard(is_cancelled)
    check()
    base = img.convert("RGBA")
    check()
    target = (max(1, int(round(img.size[0] * scale))),
              max(1, int(round(img.size[1] * scale))))
    if base.size != target:
        base = base.resize(target, Image.LANCZOS)
        check()
    pixels = img.size[0] * img.size[1]
    factor = IMAGE_RENDER_SCALE if pixels <= IMAGE_SS_MAX_PIXELS else 1.0
    # 大图（factor==1）且超过阈值：按带合成，避免整层 RGBA 驻留。
    # factor==1 时带内合成与「整层 alpha_composite」逐像素一致（带之间互不重叠），
    # 因此只在 factor==1 时走带路径，保证与旧实现像素级等价。
    if factor == 1.0 and (target[0] * target[1]) > IMAGE_BAND_PIXELS:
        return _composite_banded(base, img.size[0], img.size[1], spec,
                                 max(1e-3, float(scale)), is_cancelled)
    layer = _render_crisp_layer(img.size[0], img.size[1], spec,
                                max(1e-3, factor) * max(1e-3, float(scale)),
                                is_cancelled=is_cancelled)
    check()
    if layer.size != target:
        layer = layer.resize(target, Image.LANCZOS)  # 实测占小图总耗时的 57%
        check()
    return Image.alpha_composite(base, layer)


def flatten(img: Image.Image, background: Tuple[int, int, int] = (255, 255, 255)) -> Image.Image:
    """把带 alpha 的结果压到不透明底色上（输出 JPEG / BMP 等无 alpha 格式时用）。"""
    if img.mode in ("RGBA", "LA"):
        rgba = img.convert("RGBA")
        bg = Image.new("RGB", rgba.size, background)
        bg.paste(rgba, mask=rgba.getchannel("A"))
        return bg
    return img.convert("RGB")


# ---------------------------------------------------------------------------
# PDF 路径
# ---------------------------------------------------------------------------

#: (参数指纹, 光栅倍率, 视觉宽, 视觉高, /Rotate) -> 对应的透明水印层 ``fitz.Pixmap``
#:
#: 键里**必须**带参数指纹与光栅倍率：缓存是模块级的（跨 ``render_pdf`` 调用复用，
#: 批处理上百个同版式 PDF 时能省下大量重渲染），若只按尺寸做键，「同尺寸 + 不同参数」
#: 的两份 PDF 会互相命中对方的图层；漏掉 ``scale`` 则 1× 与 2× 会复用不同分辨率图层。
#:
#: 页面尺寸仍取整到 pt：500.0 与 500.4 会共键，可能有最高约 23/255 的亚像素灰度差；
#: 这是用肉眼不可见的误差换取扫描件尺寸抖动时缓存命中率的已知取舍。
#:
#: 容器用 :class:`wm.lru.SizedLRU`：预览线程与批处理线程会**同时**读写它，无锁的
#: trim 会随机 ``KeyError``（审计 S2）。预算同样走 lambda 惰性求值，方便测试压小。
_PDF_LAYER_CACHE: SizedLRU = SizedLRU(
    lambda value: _pixmap_cost(value), lambda: PDF_LAYER_CACHE_BYTES)


def _spec_cache_key(spec: WatermarkSpec) -> tuple:
    """图层缓存用的「参数指纹」—— 真源是 :meth:`WatermarkSpec.render_key`。

    留这个薄封装只为兼容既有调用点：**不要**在这里补字段，改 spec 去。
    """
    return spec.render_key()


def _pixmap_cost(pix: "fitz.Pixmap") -> int:
    """一个图层 Pixmap 的驻留字节数。

    实测 PyMuPDF 1.28.2（1190×1684、``n=4``）：``pix.size == 8015936``，而
    ``pix.n * pix.width * pix.height == 8015840``、``len(pix.samples) == 8015840``
    —— 即 ``.size`` = 采样区字节数 + 96 字节的固定结构开销，**语义正确且略保守**。
    优先用它；万一将来版本没有这个属性，退回 ``n * width * height``。
    """
    size = getattr(pix, "size", None)
    if isinstance(size, int) and size > 0:
        return int(size)
    return int(getattr(pix, "n", 4) or 4) * int(pix.width) * int(pix.height)


def clear_pdf_layer_cache() -> None:
    """清空 PDF 图层缓存（测试 / 诊断用）。"""
    _PDF_LAYER_CACHE.clear()


def trim_pdf_layer_cache() -> None:
    """把图层缓存压回字节预算（诊断用；正常路径由 ``SizedLRU.set`` 自动裁剪）。

    ⚠️ **最新一条永不淘汰**：``render_pdf`` 是「先入缓存、再 ``insert_image``」，
    正在用的那张就是最新一条。``SizedLRU`` 的 ``min_keep=1`` 兜住这条底线。
    """
    _PDF_LAYER_CACHE.trim()


def _discard_part(part_path: str) -> None:
    """尽最大努力删掉 ``.part`` 半成品 —— 绝不在用户目录里留垃圾。

    删除失败（被别的进程占用等）**不能**掩盖真正的原因，故一律吞掉 OSError。
    """
    try:
        if os.path.exists(part_path):
            os.remove(part_path)
    except OSError:
        pass


def render_pdf(
    src_path: str,
    dst_path: str,
    spec: WatermarkSpec,
    scale: float = PDF_RENDER_SCALE,
    progress: Optional[ProgressFn] = None,
    is_cancelled: Optional[CancelFn] = None,
) -> int:
    """给 PDF 逐页加水印并写出新文件，返回页数。

    每页插入同一张（按页面尺寸缓存）的透明水印层；循环里 ``time.sleep(0.001)``
    主动让出 GIL —— Windows 上 ``sleep(0)`` 只让出微秒级，主线程 UI 会假死。

    ``scale`` 是**上限**：每页再经 :func:`pdf_render_scale` 按像素预算下调
    （A4 仍是 2×，A0 降到 1×），见 :data:`PDF_SS_MAX_PIXELS`。

    输出走「先写 ``dst + ".part"``、再 ``os.replace``」：同目录内 ``os.replace``
    是原子的，因此既保留「绝不留半成品」的语义，又不必像旧实现 ``doc.tobytes()``
    那样把整份输出 PDF 驻留内存 —— 实测 30 页 / 输出 86MB 的样本：峰值工作集增量
    **194.7MB -> 33.6MB**（-83%），耗时 9.14s -> 9.07s。任何异常路径都会清掉
    ``.part``，绝不在用户目录里留垃圾。
    """
    doc = fitz.open(src_path)
    spec_key = _spec_cache_key(spec)
    part_path = dst_path + ".part"
    try:
        total = doc.page_count
        for index in range(total):
            if is_cancelled is not None and is_cancelled():
                raise Cancelled()
            page = doc[index]
            rect = page.rect               # 视觉尺寸（**已应用** /Rotate）
            rot = int(page.rotation) % 360
            # 大页面自动降倍率（A0 2× -> 1×，实测 2.44s -> 约 1/4），版式不受影响
            eff_scale = pdf_render_scale(rect.width, rect.height, scale)
            key = (spec_key, float(eff_scale), int(round(rect.width)),
                   int(round(rect.height)), rot)
            pix = _PDF_LAYER_CACHE.get(key)
            if pix is None:
                # 输出用清晰层（缩放坐标系内渲染），块位图按放大字号光栅化
                layer = render_output_layer(rect.width, rect.height, spec, scale=eff_scale)
                if rot:
                    # ⚠️ **/Rotate 页必须补偿**：``page.rect`` 是已旋转的**视觉**
                    # 尺寸（595×842 的页 + /Rotate 90 => 842×595），而 ``insert_image``
                    # 落在**未旋转**坐标系（MediaBox 仍是 595×842）。不补偿的话，
                    # 宽 842 的图层被塞进宽 595 的页 —— 一侧整条无水印、另一侧越界
                    # （实测缺约 25% 页宽，贴边偏差 247px），且不报错、预览也看不出来。
                    #
                    # **方向是 +rot，不是 -rot**（实测踩过的坑）：
                    #   * /Rotate 90 = 显示时把页面**顺时针**转 90°（PDF 规范）；
                    #   * PIL 的 rotate(θ) 是**逆时针** θ 度；
                    #   * 于是「显示 = U.rotate(-90)」，要它等于视觉层 L，则
                    #     U = L.rotate(+90) —— 即 ``rotate(rot)``。
                    # 用 -rot 会让内容**整体转 180°**（文字上下颠倒）。这个错误用
                    # 「四段覆盖率」测不出来：平铺图案转 180° 照样铺满整页。判方向必须
                    # 用非对称标记（见 ``_smoke/verify_rotate_marker.py``）。
                    layer = layer.rotate(rot, expand=True)
                buffer = io.BytesIO()
                layer.save(buffer, format="PNG")
                pix = fitz.Pixmap(buffer.getvalue())  # n=4, alpha=1（保留 alpha）
                # 单条超预算时 SizedLRU 自动拒收（否则一进去就把缓存清光，
                # 反复重渲染）；普通过则由它按字节预算淘汰最旧的一条。
                _PDF_LAYER_CACHE.set(key, pix)
            if rot:
                page.set_rotation(0)       # 插到未旋转坐标系；插完立刻还原
            try:
                page.insert_image(page.rect, pixmap=pix, overlay=True)
            finally:
                if rot:
                    page.set_rotation(rot)
            if progress is not None:
                progress(index + 1, total, f"第 {index + 1}/{total} 页")
            time.sleep(0.001)  # Windows 上必须给足 1ms 才能让主线程拿到 GIL
        # 直接流式写盘，不再 tobytes() 整份驻留内存；写坏/中断只影响 .part
        doc.save(part_path, garbage=4, deflate=True)
    except BaseException:
        _discard_part(part_path)
        raise
    finally:
        doc.close()
    try:
        os.replace(part_path, dst_path)  # 同目录内原子替换：要么旧文件、要么新文件
    except BaseException:
        _discard_part(part_path)
        raise
    return total


__all__ = [
    "IMAGE_RENDER_SCALE",
    "IMAGE_SS_MAX_PIXELS",
    "IMAGE_BAND_PIXELS",
    "IMAGE_BAND_ROWS",
    "PDF_RENDER_SCALE",
    "PDF_SS_MAX_PIXELS",
    "PDF_MIN_RENDER_SCALE",
    "PDF_SCALE_QUANTUM",
    "pdf_render_scale",
    "PDF_LAYER_CACHE_BYTES",
    "clear_pdf_layer_cache",
    "Cancelled",
    "render_overlay_layer",
    "render_output_layer",
    "render_image",
    "render_pdf",
    "flatten",
]
