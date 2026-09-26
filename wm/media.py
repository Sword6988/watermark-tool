"""文件类型识别、页面尺寸读取、输出路径规划。

「绝不覆盖原文件」是硬要求：输出路径一律加后缀，若同名已存在则自动加序号。
"""

from __future__ import annotations

import os
from typing import Optional, Tuple

import pymupdf as fitz
from PIL import Image, ImageOps

from .spec import DEFAULT_SUFFIX

#: 支持的图片扩展名
IMAGE_EXTS = frozenset({".png", ".jpg", ".jpeg", ".bmp", ".webp", ".tif", ".tiff", ".gif"})
#: 支持的文档扩展名
PDF_EXTS = frozenset({".pdf"})
SUPPORTED_EXTS = IMAGE_EXTS | PDF_EXTS

KIND_IMAGE = "image"
KIND_PDF = "pdf"


# ---------------------------------------------------------------------------
# 类型识别
# ---------------------------------------------------------------------------

def ext_of(path: str) -> str:
    """返回小写扩展名（含点）。"""
    return os.path.splitext(str(path))[1].lower()


def kind_of(path: str) -> Optional[str]:
    """返回 'image' / 'pdf' / None。"""
    ext = ext_of(path)
    if ext in IMAGE_EXTS:
        return KIND_IMAGE
    if ext in PDF_EXTS:
        return KIND_PDF
    return None


def is_supported(path: str) -> bool:
    """是否为支持的文件类型。"""
    return kind_of(path) is not None


# ---------------------------------------------------------------------------
# 文档封装
# ---------------------------------------------------------------------------

#: 输出编码器支持接收 ``dpi`` 参数的格式。BMP / GIF 没有分辨率容器，显式不传，
#: 否则 Pillow 会抛 ``ValueError: unknown file extension`` 之外的参数错误。
DPI_SAVE_EXTS = frozenset({".png", ".jpg", ".jpeg", ".webp", ".tif", ".tiff"})
#: 输出编码器支持内嵌 ICC 色彩配置文件的格式（同 ``ui.output.ICC_SAVE_EXTS`` 的真源）。
ICC_SAVE_EXTS = frozenset({".png", ".jpg", ".jpeg", ".webp", ".tif", ".tiff"})
#: 源图的哪些色彩模式可以**原样**把 ICC 带到 RGB 输出上。CMYK / YCbCr 的 ICC 描述
#: 的是四通道色彩空间，把它的 profile 塞进已被转成 RGB 的图里是**错误标注**，
#: 宁可不写也不能写错。
ICC_SAFE_MODES = frozenset({"RGB", "RGBA", "L", "LA", "P"})


def _read_dpi(raw: object) -> Optional[Tuple[int, int]]:
    """把 ``info["dpi"]`` 规范化成 ``(x, y)`` 整数对，不可用返回 ``None``。

    Pillow 给的是浮点（JPEG 的 JFIF 密度、PNG 的 pHYs 都可能是 ``(300.0, 300.0)``
    之类）。**必须**过滤两类值：非正数（``(0, 0)``）与荒谬小值（``(1, 1)``）——
    后者写进输出会让打印尺寸夸张到几十米，比不写更糟。下界取 8 dpi：正常素材的
    密度都在 72 以上，低于这个数的不是真实分辨率。
    """
    if not isinstance(raw, (tuple, list)) or len(raw) != 2:
        return None
    try:
        x, y = int(round(float(raw[0]))), int(round(float(raw[1])))
    except (TypeError, ValueError):
        return None
    if min(x, y) < 8 or max(x, y) > 100_000:
        return None
    return (x, y)


def _read_icc(raw: object) -> Optional[bytes]:
    """把 ``info["icc_profile"]`` 规范化成 ``bytes``，为空 / 类型不对返回 ``None``。"""
    if isinstance(raw, bytes) and raw:
        return raw
    if isinstance(raw, (bytearray, memoryview)) and len(raw):
        return bytes(raw)
    return None


class Document:
    """一个待处理的文件（图片或 PDF）。

    图片的「页面坐标系」= 像素；PDF 的「页面坐标系」= 点（pt）。

    除像素外还保留三类**应带回输出文件**的元数据（PDF / 缺失时为 ``None``）：

        * ``exif_bytes``  —— 转正后的 EXIF；
        * ``dpi``         —— 源图分辨率（打印尺寸靠它，丢了会让 300dpi 的扫描件
                             输出后变成 72dpi，打印尺寸放大 4 倍）；
        * ``icc_profile`` —— 源图 ICC 色彩配置（丢了会偏色）。

    三者都只在**头部解析阶段**读取（``Image.open`` 后 ``info`` 字典已有，不会
    解码整张图），因此不影响「打开大图不额外驻留像素」的性能约束。
    """

    def __init__(self, path: str) -> None:
        self.path = str(path)
        self.kind = kind_of(self.path)
        if self.kind is None:
            raise ValueError(f"不支持的文件类型：{self.path}")
        self._image: Optional[Image.Image] = None
        self._image_size: Tuple[int, int] = (0, 0)
        self._pdf: Optional[fitz.Document] = None
        # 输出侧可直接写回的、与已转正像素一致的 EXIF；PDF/无 EXIF 图片为 None。
        self.exif_bytes: Optional[bytes] = None
        #: 源图分辨率 ``(x, y)``；无分辨率信息 / PDF 为 None
        self.dpi: Optional[Tuple[int, int]] = None
        #: 源图 ICC 色彩配置；PDF / 无 ICC / 色彩空间不可直传时为 None
        self.icc_profile: Optional[bytes] = None
        if self.kind == KIND_IMAGE:
            source_image = Image.open(self.path)
            try:
                # 只读头部元数据不会解码整张图片。绝大多数图片没有方向标签，或
                # Orientation=1（已正向）；这些常态路径必须保留 Image.open 的惰性，
                # 否则仅打开一张 20MP 图片就会额外驻留约一份全尺寸像素缓冲。
                #
                # Pillow 的 exif_transpose() 总会返回副本，而且多帧图的副本只含当前
                # 帧；所以须先记录原始帧数，并且仅在方向 2–8 真需要转正时才解码、
                # 复制。Orientation 缺失 / 0 / 1 均不解码：只记录尺寸并关闭文件，
                # 等 page_image() 真正需要像素时再短暂打开。
                self.frame_count = int(getattr(source_image, "n_frames", 1) or 1)
                source_image.seek(0)
                self.dpi = _read_dpi(source_image.info.get("dpi"))
                self.icc_profile = (_read_icc(source_image.info.get("icc_profile"))
                                    if source_image.mode in ICC_SAFE_MODES else None)
                # 不调用 source_image.getexif()：Pillow 的 PNG 实现会为此 load() 整张
                # 图片。JPEG / PNG / WebP 在 open 阶段已把原始 EXIF 块放进 info，直接
                # 解析这段小字节串即可读取 Orientation，而无需创建像素缓冲。
                raw_exif = source_image.info.get("exif")
                source_exif: Optional[Image.Exif] = None
                if isinstance(raw_exif, bytes) and raw_exif:
                    source_exif = Image.Exif()
                    source_exif.load(raw_exif)
                orientation = source_exif.get(274) if source_exif is not None else None
                if orientation in range(2, 9):
                    source_image.load()
                    normalized_image = ImageOps.exif_transpose(source_image)
                    normalized_exif = normalized_image.getexif()
                else:
                    normalized_image = source_image
                    normalized_exif = source_exif
            except Exception:
                source_image.close()
                raise
            self.page_count = 1
            self.exif_bytes = (normalized_exif.tobytes()
                               if normalized_exif is not None and len(normalized_exif)
                               else None)
            self._image_size = tuple(normalized_image.size)
            if normalized_image is source_image:
                # 常态路径不保留打开的磁盘句柄（Windows 下会锁源文件）。取像素时
                # page_image() 再短暂打开并在返回前完整解码，尺寸查询仍保持零解码。
                source_image.close()
            else:
                source_image.close()
                self._image = normalized_image
        else:
            self._pdf = fitz.open(self.path)
            self.page_count = self._pdf.page_count
            self.frame_count = 1

    # -- 元信息 -----------------------------------------------------------

    def page_size(self, index: int = 0) -> Tuple[float, float]:
        """返回页面坐标系下的 ``(宽, 高)``。"""
        if self.kind == KIND_IMAGE:
            width, height = self._image.size if self._image is not None else self._image_size
            return float(width), float(height)
        assert self._pdf is not None
        rect = self._pdf[index].rect
        return float(rect.width), float(rect.height)

    def page_image(self, index: int = 0, dpi: int = 150) -> Image.Image:
        """返回该页的位图（RGB）。图片返回原图，PDF 按 dpi 栅格化。"""
        if self.kind == KIND_IMAGE:
            if self._image is not None:
                return self._image.convert("RGB")
            # 无需 EXIF 转正的常态路径只在真正取像素时解码；上下文退出前 convert()
            # 已生成独立图像，因此磁盘句柄不会随返回值泄漏到调用方。
            with Image.open(self.path) as source_image:
                source_image.seek(0)
                return source_image.convert("RGB")
        assert self._pdf is not None
        page = self._pdf[index]
        pixmap = page.get_pixmap(dpi=dpi)
        if pixmap.alpha:
            return Image.frombytes("RGBA", (pixmap.width, pixmap.height), pixmap.samples).convert("RGB")
        if pixmap.n == 4:
            return Image.frombytes("RGBA", (pixmap.width, pixmap.height), pixmap.samples).convert("RGB")
        return Image.frombytes("RGB", (pixmap.width, pixmap.height), pixmap.samples)

    def close(self) -> None:
        """释放底层资源。"""
        if self._pdf is not None:
            try:
                self._pdf.close()
            finally:
                self._pdf = None
        if self._image is not None:
            try:
                self._image.close()
            finally:
                self._image = None


# ---------------------------------------------------------------------------
# 输出路径规划（绝不覆盖原文件）
# ---------------------------------------------------------------------------

def unique_path(path: str) -> str:
    """若 ``path`` 已存在，追加 ``(1)`` / ``(2)`` … 直到不冲突。"""
    if not os.path.exists(path):
        return path
    root, ext = os.path.splitext(path)
    index = 1
    while True:
        candidate = f"{root}({index}){ext}"
        if not os.path.exists(candidate):
            return candidate
        index += 1


def plan_output(src: str, out_dir: Optional[str] = None, suffix: str = DEFAULT_SUFFIX) -> str:
    """规划输出路径：默认同目录 + ``_watermarked`` 后缀，绝不覆盖原文件。"""
    directory = out_dir if out_dir else os.path.dirname(os.path.abspath(src))
    os.makedirs(directory, exist_ok=True)
    root, ext = os.path.splitext(os.path.basename(src))
    return unique_path(os.path.join(directory, f"{root}{suffix}{ext}"))


__all__ = [
    "IMAGE_EXTS",
    "PDF_EXTS",
    "SUPPORTED_EXTS",
    "KIND_IMAGE",
    "KIND_PDF",
    "DPI_SAVE_EXTS",
    "ICC_SAVE_EXTS",
    "ICC_SAFE_MODES",
    "ext_of",
    "kind_of",
    "is_supported",
    "Document",
    "unique_path",
    "plan_output",
]
