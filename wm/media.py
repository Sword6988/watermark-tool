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


class PasswordRequiredError(ValueError):
    """PDF 设有**打开口令**（用户口令），读不到任何页面内容。

    故意继承 :class:`ValueError` 而不是 :class:`Exception`：既有的调用方
    （``ui/app.py`` 的 ``_abort_open``、``ui/batch.py`` 的失败记录）都按
    ``except Exception`` 兜住，继承 ``ValueError`` 还能让将来「按 ValueError
    收窄」的写法不漏掉它。

    为什么单独开一个类型：这类文件**能打开**（``fitz.open`` 不报错、``page_count``
    甚至返回正常页数），要到取页面尺寸 / 栅格化时才炸，pymupdf 给的是
    ``document closed or encrypted`` —— 用户看到这句只会以为文件坏了，不知道是
    要输密码。单独的类型让调用方能给出**可行动**的提示。
    """


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

    def __init__(self, path: str, read_path: Optional[str] = None) -> None:
        self.path = str(path)
        #: 实际读取路径。加密文件解密后指向临时明文；默认与 ``path`` 相同。
        #: **输出命名一律用 ``path``**（原文件旁出图），只有"读"走 ``read_path``。
        self.read_path = str(read_path) if read_path else self.path
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
            source_image = Image.open(self.read_path)
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
            # ``tobytes()`` 必须在 try 里：畸形 EXIF 会让它抛异常，而那时
            # ``source_image`` 还没关闭 —— Windows 下句柄会一直锁住源文件，
            # 后续删除 / 覆盖全部失败（表现为 WinError 32 且无日志）。
            try:
                self.exif_bytes = (normalized_exif.tobytes()
                                   if normalized_exif is not None and len(normalized_exif)
                                   else None)
            except Exception:
                try:
                    source_image.close()
                finally:
                    if normalized_image is not source_image:
                        normalized_image.close()
                raise
            self._image_size = tuple(normalized_image.size)
            if normalized_image is source_image:
                # 常态路径不保留打开的磁盘句柄（Windows 下会锁源文件）。取像素时
                # page_image() 再短暂打开并在返回前完整解码，尺寸查询仍保持零解码。
                source_image.close()
            else:
                source_image.close()
                self._image = normalized_image
        else:
            self._pdf = fitz.open(self.read_path)
            self._reject_if_password_protected()
            self.page_count = self._pdf.page_count
            self.frame_count = 1

    def _reject_if_password_protected(self) -> None:
        """带**打开口令**的 PDF 就地抛出 :class:`PasswordRequiredError`。

        必须在**打开时就判**，不能等取像素：这类文件 ``fitz.open`` 不报错、
        ``page_count`` 也正常，要到 ``page_size()`` / ``page_image()`` 才炸，
        届时错误已经到了预览 / 导出阶段，用户只会看到「渲染失败」。

        ``needs_pass`` 为真时先试一次空口令：只设了**所有者口令**（限制打印 /
        编辑但允许直接打开）的 PDF 也可能被标成需要口令，那类文件是能读的，
        不能误杀。
        """
        if self._pdf is None:
            return
        try:
            needs = bool(self._pdf.needs_pass)
        except Exception:
            return  # 拿不到这个属性就按「不需要口令」处理，不因此拒绝文件
        if not needs:
            return
        try:
            if self._pdf.authenticate(""):
                return
        except Exception:
            pass  # 认证本身出错：按「需要口令」处理（下面会抛，且信息可行动）
        self._pdf.close()
        self._pdf = None
        raise PasswordRequiredError(
            "该 PDF 设有打开口令，需要密码才能读取 —— "
            "请先用其它工具解除口令保护，再添加此文件")

    # -- 元信息 -----------------------------------------------------------

    def page_size(self, index: int = 0) -> Tuple[float, float]:
        """返回页面坐标系下的 ``(宽, 高)``。"""
        if self.kind == KIND_IMAGE:
            width, height = self._image.size if self._image is not None else self._image_size
            return float(width), float(height)
        if self._pdf is None:
            # 不用 assert：冻结版以 -O 构建，assert 会被剔除，同样的错误在源码版
            # 是清晰的 AssertionError、在 exe 里却退化成 'NoneType' 不可下标，
            # 排查成本天差地别。
            raise ValueError("PDF 文档未打开（或已关闭）")
        rect = self._pdf[index].rect
        return float(rect.width), float(rect.height)

    def page_image(self, index: int = 0, dpi: int = 150) -> Image.Image:
        """返回该页的位图（RGB）。图片返回原图，PDF 按 dpi 栅格化。"""
        if self.kind == KIND_IMAGE:
            if self._image is not None:
                return self._image.convert("RGB")
            # 无需 EXIF 转正的常态路径只在真正取像素时解码；上下文退出前 convert()
            # 已生成独立图像，因此磁盘句柄不会随返回值泄漏到调用方。
            with Image.open(self.read_path) as source_image:
                source_image.seek(0)
                return source_image.convert("RGB")
        if self._pdf is None:
            raise ValueError("PDF 文档未打开（或已关闭）")
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
    """规划输出路径：默认同目录 + ``_水印版`` 后缀，绝不覆盖原文件。"""
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
    "PasswordRequiredError",
    "Document",
    "unique_path",
    "plan_output",
]
