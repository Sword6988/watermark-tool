"""版式权威模块（v4）：块几何 + 平铺起止序列。

**水印恒为平铺铺满整页**：本模块不再有「单块定位」分支，也不消费任何位置偏移
参数 —— 排布完全由页面尺寸 + 字号 + 边距 + 旋转角决定，用户不可调节。

**图片（Pillow）与 PDF（PyMuPDF）两条渲染路径必须消费同一份布局结果。**
任何一边单独重算必然产生像素偏差，所以这里集中产出：

    * ``block_geometry``      : 块尺寸 + 墨迹外接框（带缓存）
    * ``axis_starts``         : 单轴「墨迹起点」序列（唯一平铺排布逻辑）
    * ``compute_placements``  : 每个块左上角在「页面坐标系」下的坐标

坐标系：本模块所有量都在**页面坐标系**下（图片 = 像素，PDF = 点）。字号由
``font_px`` 换算得到：``font_pct / 100 * min(page_w, page_h)``。

**关于预览等比缩放**：所有尺度参数都是「页面短边百分比」，因此页面整体等比
缩小后它们自动等比，不需要任何额外的预览补偿系数。渲染时只需把「已算好的
页面坐标布局结果」× scale、以及「块位图字号」× scale 即可，数学上等价于
全分辨率渲染后再整体缩小 —— 预览即所见。
"""

from __future__ import annotations

import math
from typing import Dict, List, Optional, Tuple

from PIL import Image, ImageDraw

from . import fonts
from .spec import LINE_SPACING, WatermarkSpec

#: 平铺块数硬上限，防极端参数下块数爆炸卡死
MAX_TILES = 4000
#: 各类测量缓存上限
_CACHE_CAP = 512

#: 单个水印块的**旋转后包围盒像素**上限。块尺寸随「文本长度 × 字号」增长，细长块
#: 经 ``rotate(expand=True)`` 后可能放大几十至上百倍；只看旋转前尺寸会让长文本绕过
#: 守卫，直到 Pillow 分配数百 MB 甚至近 1GB RGBA 内存。
#:
#: 阈值按 Windows / HarmonyOS Sans SC、30° 实测标定：8000×8000 页面、font_pct=30%、
#: PDF 2× 光栅、文本「机密」的旋转后包围盒约 134.7MP，165MP 留有约 22.5% 余量；
#: 常用「机密文件」30% @178.5px 约 0.45MP、多行「内部资料\n请勿外传」12%
#: @178.5px 约 0.71MP；危险的 ``"A"*200`` 4% @178.5px 约 247.7MP，必须拦截。
#: 判定使用旋转后包围盒的解析估算，并且仍在 ``Image.new`` **之前**执行，保证早失败。
MAX_BLOCK_PIXELS = 165_000_000

#: ``_RENDER_CACHE`` 的**字节**预算。块位图单条可达上百 MB（8000×8000 + 字号 30%
#: 实测 1.2GB），只按**条数**限流（旧做法）会让几十条就吃掉几个 GB —— 改为按字节算。
_RENDER_CACHE_BYTES = 256 * 1024 * 1024

#: (font_family, px, angle, lines) -> ((bw,bh), (ix0,iy0,ix1,iy1))
_BLOCK_CACHE: Dict[tuple, Tuple[Tuple[int, int], Tuple[float, float, float, float]]] = {}
#: 同上 + (color, opacity) -> 实际 RGBA 块位图
_RENDER_CACHE: Dict[tuple, Image.Image] = {}


# ---------------------------------------------------------------------------
# 百分比 -> 像素
# ---------------------------------------------------------------------------

def ref_size(page_w: float, page_h: float) -> float:
    """参考尺度：页面短边。所有百分比参数都以它为基准。"""
    return max(1.0, min(float(page_w), float(page_h)))


def font_px(spec: WatermarkSpec, page_w: float, page_h: float) -> float:
    """字号 -> 页面像素：``font_pct / 100 * ref``。"""
    return max(1.0, float(spec.font_pct) / 100.0 * ref_size(page_w, page_h))


def margin_px(spec: WatermarkSpec, page_w: float, page_h: float) -> float:
    """四周边距 -> 页面像素：``margin_pct / 100 * ref``。"""
    return max(0.0, float(spec.margin_pct) / 100.0 * ref_size(page_w, page_h))


# ---------------------------------------------------------------------------
# 块位图：文字 -> 旋转后的透明 RGBA 块
# ---------------------------------------------------------------------------

def _alpha_rgba(color: str, opacity: float) -> Tuple[int, int, int, int]:
    """把 ``#rrggbb`` + 不透明度转成 RGBA 四元组。"""
    hex_color = color.lstrip("#")
    if len(hex_color) == 3:
        hex_color = "".join(ch * 2 for ch in hex_color)
    try:
        r = int(hex_color[0:2], 16)
        g = int(hex_color[2:4], 16)
        b = int(hex_color[4:6], 16)
    except (ValueError, IndexError):
        r, g, b = 211, 47, 47
    a = int(round(max(0.0, min(1.0, float(opacity))) * 255))
    return (r, g, b, a)


def _rotated_pixel_estimate(width: int, height: int, angle: float) -> float:
    """解析估算 ``rotate(expand=True)`` 后的包围盒像素数。

    Pillow 最终会把边长取整；守卫使用取整前的连续几何值，误差仅为边缘数个像素，
    而阈值留有充足余量。``angle == 0`` 时严格退化为 ``width * height``。
    """
    radians = math.radians(float(angle))
    cosine = abs(math.cos(radians))
    sine = abs(math.sin(radians))
    rotated_width = width * cosine + height * sine
    rotated_height = width * sine + height * cosine
    return rotated_width * rotated_height


def _draw_text_block(
    lines: List[str],
    font,
    rgba: Tuple[int, int, int, int],
    angle: float = 0.0,
) -> Image.Image:
    """把多行文字画到一张透明 RGBA 画布上（未旋转）。

    ``angle`` 只用于在分配画布前估算调用方旋转后的包围盒；实际旋转仍由
    :func:`_render_block_bitmap` 完成。

    块四周的透明内边距压到最小 ``max(2, line_height * 0.08)``：唯一作用是规避
    ``rotate(expand=True)`` 重采样在块边缘的抗锯齿裁切，留白过大会让平铺变稀疏。
    """
    ascent, descent = font.getmetrics()
    line_h = max(1, ascent + descent)
    extra = line_h * max(0.0, LINE_SPACING - 1.0)
    widths = [font.getlength(line) for line in lines] or [0.0]
    text_w = max(widths) if widths else 1.0
    count = max(1, len(lines))
    text_h = line_h * count + extra * (count - 1)

    block_pad = max(2, int(round(line_h * 0.08)))
    width = max(1, int(math.ceil(text_w)) + block_pad * 2)
    height = max(1, int(math.ceil(text_h)) + block_pad * 2)

    # **在分配内存之前**按旋转后包围盒拦下极端组合：细长文本旋转后可能从几百万
    # 像素膨胀到数亿像素，若只检查未旋转画布仍会先分配近 1GB、再卡住数秒。
    pixels = _rotated_pixel_estimate(width, height, angle)
    if pixels > MAX_BLOCK_PIXELS:
        raise ValueError(
            "水印块过大（旋转后需要约 %.0f 百万像素，上限 %d 百万）：文字太长或字号太大，"
            "请减少文字、增加行数换行，或降低字号"
            % (pixels / 1e6, MAX_BLOCK_PIXELS // 1_000_000))

    bitmap = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(bitmap)
    y = float(block_pad)
    for line in lines:
        draw.text((float(block_pad), y), line, font=font, fill=rgba)  # anchor 默认 "la"
        y += line_h + extra
    return bitmap


def _render_block_bitmap(spec: WatermarkSpec, size_px: float) -> Image.Image:
    """取得指定字号下、旋转后的透明 RGBA 水印块（带缓存）。"""
    fs = max(1.0, float(size_px))
    key = (
        spec.font_family or "",
        round(fs, 3),
        round(float(spec.angle), 3),
        tuple(spec.lines()),
        spec.color,
        round(float(spec.opacity), 4),
    )
    cached = _RENDER_CACHE.get(key)
    if cached is not None:
        return cached

    font = fonts.pil_font(spec.font_family, fs)
    angle = float(spec.angle)
    bitmap = _draw_text_block(
        spec.lines(), font, _alpha_rgba(spec.color, spec.opacity), angle=angle)
    if abs(angle) > 1e-6:
        bitmap = bitmap.rotate(angle, expand=True, resample=Image.BICUBIC)

    # 超大块**不进缓存**（否则单条就把预算撑爆、触发反复清仓），普通块按字节预算淘汰
    if _bitmap_cost(bitmap) <= _RENDER_CACHE_BYTES:
        _RENDER_CACHE[key] = bitmap
        _trim_render_cache()
    return bitmap


def _bitmap_cost(bitmap: Image.Image) -> int:
    """块位图的内存量级（RGBA = 4 字节/像素）。"""
    return bitmap.size[0] * bitmap.size[1] * 4


def _trim_render_cache() -> None:
    """按插入顺序淘汰最旧的块位图，直到总字节落回预算。

    dict 保持插入顺序，``next(iter(...))`` 即最旧的一条 —— 够用的近似 LRU。
    """
    total = sum(_bitmap_cost(v) for v in _RENDER_CACHE.values())
    while total > _RENDER_CACHE_BYTES and _RENDER_CACHE:
        key, oldest = next(iter(_RENDER_CACHE.items()))
        total -= _bitmap_cost(oldest)
        del _RENDER_CACHE[key]


# ---------------------------------------------------------------------------
# 块几何（带缓存）
# ---------------------------------------------------------------------------

def block_geometry(
    spec: WatermarkSpec, size_px: float
) -> Tuple[Tuple[int, int], Tuple[float, float, float, float]]:
    """返回 ``((块宽, 块高), (墨迹 x0, y0, x1, y1))``，图片与 PDF 共用。

    测墨迹 bbox 必须用 ``spec.copy(opacity=1.0)`` 的**探针**渲染：低不透明度会
    抹掉边缘抗锯齿像素，测出的墨迹比实际小。
    """
    fs = max(1.0, float(size_px))
    key = (
        "geom",
        spec.font_family or "",
        round(fs, 3),
        round(float(spec.angle), 3),
        tuple(spec.lines()),
    )
    cached = _BLOCK_CACHE.get(key)
    if cached is not None:
        return cached

    probe = spec.copy(opacity=1.0)
    bitmap = _render_block_bitmap(probe, fs)
    bbox = bitmap.getchannel("A").getbbox()
    if not bbox or bbox[2] <= bbox[0] or bbox[3] <= bbox[1]:
        bbox = (0, 0, bitmap.size[0], bitmap.size[1])
    result = (bitmap.size, (float(bbox[0]), float(bbox[1]), float(bbox[2]), float(bbox[3])))

    if len(_BLOCK_CACHE) >= _CACHE_CAP:
        _BLOCK_CACHE.clear()
    _BLOCK_CACHE[key] = result
    return result


# ---------------------------------------------------------------------------
# 平铺起止序列：完整水印 + 贴齐铺满
# ---------------------------------------------------------------------------

def axis_starts(page: float, ink: float, margin: float, extra_gap: float = 0.0) -> List[float]:
    """某一轴上各块「墨迹起点」的坐标：**首尾贴齐边距线 + 整轴均分**。

    设计（v3）：
        * 首块墨迹起点 = ``margin``、末块墨迹终点 = ``page - margin``（两端贴齐）；
        * 相邻间距 ``g = span / k``，其中 ``span = page - 2*margin - ink``（墨迹可用
          跨度）、``k = floor(span / step)`` —— 取「满足最小间距前提下的最大块数」，
          于是 ``g >= step = ink + 2*margin``；可见空隙 ``g - ink >= 2*margin``，
          既不粘连更不重叠，且整轴**完全等距**；
        * 放不下两块（``k == 0``）或步长退化 -> 只放一块并居中，不抛异常。

    ⚠️ **为什么不用「等距 + 末列补齐」**（v2 的错误做法，已废弃）：那条路先按
    ``floor`` 排满，再把最后一块「补」到边距线上，而 ``floor`` 之后的余量恒
    ``< step``，所以补出来的末块**起点差**必然 ``< step``；若守卫条件误写成
    「起点差 >= 2*margin」（正确的判据应是「可见空隙 = 起点差 - ink >= 2*margin」），
    末块可见空隙就会小于设定值，极端参数下甚至变成**负值（墨迹重叠）**。
    实测 A4 595×842、font=4%、margin=1%：起点差 18.1 < 墨迹高 63，末两行重叠 44.9 px。
    均分法把余量摊到每一段，代价是实际间距比设定值略大（最多 ``(k+1)/k`` 倍），
    换取「零重叠 + 零裁切 + 间距完全均匀」。**不要再改回补齐式。**

    ``extra_gap`` 仅用于极端参数下的块数回退：``step`` 增大 -> ``k`` 减小，仍收敛。
    """
    page = float(page)
    ink = float(ink)
    margin = float(margin)
    step = ink + 2.0 * margin + float(extra_gap)
    span = page - 2.0 * margin - ink
    if span <= 0 or step <= 1.0:
        return [(page - ink) / 2.0]
    count = int(span // step)
    if count < 1:  # 只放得下一块 -> 居中（此时居中落点必然距边 >= margin）
        return [(page - ink) / 2.0]
    gap = span / count
    return [margin + i * gap for i in range(count + 1)]


def compute_placements(
    page_w: float,
    page_h: float,
    block_w: float,
    block_h: float,
    spec: WatermarkSpec,
    ink: Optional[Tuple[float, float, float, float]] = None,
) -> List[Tuple[float, float]]:
    """返回所有块左上角的坐标列表（页面坐标系）。

    恒为平铺：两轴各自走 ``axis_starts``（首尾贴齐 + 均分），再做笛卡尔积。
    起点序列以**墨迹尺寸**计算间距，不用含旋转留白的块尺寸，否则间距会被放大。
    """
    if page_w <= 0 or page_h <= 0:
        return [(0.0, 0.0)]
    ix0, iy0 = (0.0, 0.0) if ink is None else (float(ink[0]), float(ink[1]))

    # 步长以**墨迹尺寸**计算，不用含旋转留白的块尺寸，否则间距会被无形放大
    if ink is None:
        iw, ih = max(1.0, float(block_w)), max(1.0, float(block_h))
    else:
        iw = max(1.0, float(ink[2]) - float(ink[0]))
        ih = max(1.0, float(ink[3]) - float(ink[1]))

    margin = margin_px(spec, page_w, page_h)
    extra = 0.0
    xs: List[float] = [0.0]
    ys: List[float] = [0.0]
    for _ in range(16):  # 块数保护：步长倍增回退，防止卡死（审计实测最差 2 轮收敛）
        xs = axis_starts(page_w, iw, margin, extra)
        ys = axis_starts(page_h, ih, margin, extra)
        if len(xs) * len(ys) <= MAX_TILES:
            break
        extra = (extra if extra > 0 else iw + 2.0 * margin) * 1.35
    return [(x - ix0, y - iy0) for y in ys for x in xs]


def clear_caches() -> None:
    """清空测量/渲染缓存（测试用）。"""
    _BLOCK_CACHE.clear()
    _RENDER_CACHE.clear()


__all__ = [
    "MAX_TILES",
    "MAX_BLOCK_PIXELS",
    "ref_size",
    "font_px",
    "margin_px",
    "block_geometry",
    "axis_starts",
    "compute_placements",
    "clear_caches",
]
