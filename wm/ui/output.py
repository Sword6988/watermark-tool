"""图片输出编码（纯函数，无 Tk / 无 App 依赖）。

把 ``App._save_image`` / ``App._write_image`` 的业务逻辑搬到这里，使其可在无显示
环境下单测：按扩展名选择保存方式、有损格式压平 alpha、**把源图的 EXIF / DPI / ICC
带回输出**，任一元数据写不进去则逐个剥离后重试（水印优先于元数据）。

``EXIF_SAVE_EXTS`` 定义为模块级 frozenset，``App._EXIF_SAVE_EXTS`` 直接引用它，
保证单处真源。
"""

from __future__ import annotations

import os
import sys
from typing import Dict, Optional, Tuple

from PIL import Image

from .. import media, render

#: 输出编码器支持接收 EXIF 的格式。注意 Pillow 当前不会从 TIFF 源提取出本工具可
#: 回写的 ``Document.exif_bytes``，所以 TIFF 源仍按无 EXIF 导出；保留 TIFF 在集合里
#: 仅表示调用方若显式提供 EXIF 字节，编码器可以写入，不改变既有行为。
EXIF_SAVE_EXTS = frozenset({".jpg", ".jpeg", ".png", ".webp", ".tif", ".tiff"})
#: 分辨率 / ICC 的可写格式集合：真源在 ``media``（与格式识别同处一处）。
DPI_SAVE_EXTS = media.DPI_SAVE_EXTS
ICC_SAVE_EXTS = media.ICC_SAVE_EXTS
#: 保存参数里属于「元数据」的键：它们写不进去**不该**让整张图导出失败（水印才
#: 是主产物）。降级顺序即剥离顺序 —— 先怀疑 EXIF，再 ICC，最后 DPI。
META_KEYS = ("exif", "icc_profile", "dpi")


def save_image(
    out: Image.Image,
    dst: str,
    exif: Optional[bytes] = None,
    dpi: Optional[Tuple[int, int]] = None,
    icc_profile: Optional[bytes] = None,
) -> None:
    """按输出扩展名选择合适的保存方式（有损格式压平 alpha）。

    ``exif`` / ``dpi`` / ``icc_profile`` 都是**源文件带过来的元数据**
    （``media.Document`` 的 ``exif_bytes`` / ``dpi`` / ``icc_profile``，缺失时为
    None），输出时原样带回 —— 丢掉它们会让 300dpi 的扫描件输出后打印尺寸放大
    4 倍、带 sRGB 配置的图输出后偏色。

    三类元数据都只在**编码器支持时才传**：GIF / BMP 既没有 EXIF 容器也没有
    分辨率与 ICC 容器，硬传会让 Pillow 抛参数错误。
    """
    ext = media.ext_of(dst)
    if ext in (".jpg", ".jpeg"):
        image: Image.Image = render.flatten(out)
        params: Dict[str, object] = {"quality": 95}
    elif ext == ".bmp":
        image = render.flatten(out)
        params = {}
    elif ext == ".webp":
        image = out
        params = {"quality": 95, "method": 4}
    elif ext == ".gif":
        image = render.flatten(out).convert("P", palette=Image.ADAPTIVE)
        params = {}
    else:
        image = out
        params = {}
    if exif and ext in EXIF_SAVE_EXTS:
        params["exif"] = exif
    if dpi and ext in DPI_SAVE_EXTS:
        params["dpi"] = dpi
    if icc_profile and ext in ICC_SAVE_EXTS:
        params["icc_profile"] = icc_profile
    write_image(image, dst, params)


def write_image(image: Image.Image, dst: str, params: Dict[str, object]) -> None:
    """落盘；元数据写失败时**逐个剥离**再存（水印优先于元数据）。

    保存失败的第一嫌疑是元数据（EXIF / ICC / DPI 的字节串可能不被该编码器接受），
    第二嫌疑才是图片本身。所以先逐个剥掉元数据重试，全部剥完仍失败才抛出 ——
    且抛出的是**说人话**的异常（见 :func:`_friendly_save_error`）：原始
    Pillow 报错（``DecompressionBombError``、``exceeds limit`` 之类）出现在批处理
    失败清单里，用户根本不知道该怎么办。

    降级原因打到 stderr（冻结版进 runtime.log），不会静默丢元数据。
    """
    try:
        image.save(dst, **params)
        return
    except Exception as exc:
        first = exc
    remaining = dict(params)
    for key in META_KEYS:
        if key not in remaining:
            continue
        trial = {k: v for k, v in remaining.items() if k != key}
        try:
            image.save(dst, **trial)
        except Exception:
            remaining = trial  # 不是它的问题，继续剥下一个
            continue
        print(f"[WARN] {key} 写入失败，已按不带 {key} 保存 {dst}：{first}",
              file=sys.stderr)
        return
    raise _friendly_save_error(dst, first) from first


def _friendly_save_error(dst: str, exc: Exception) -> ValueError:
    """把 Pillow 的保存失败翻译成用户看得懂的中文异常。

    批处理把 ``str(exc)`` 直接列进失败清单；英文的 ``DecompressionBombError`` 之类
    对终端用户毫无意义，这里按常见原因给出**可执行**的建议。
    """
    text = str(exc)
    low = text.lower()
    if "decompressionbomb" in low or "decompression bomb" in low:
        reason = "图片超过 Pillow 的安全像素上限（请缩小图片或分批处理）"
    elif "exceed" in low or "too large" in low:
        reason = "图片尺寸超出该格式上限（宽 / 高不得超过 65500 像素）"
    elif isinstance(exc, OSError) or isinstance(exc, PermissionError):
        reason = "目标路径不可写（权限不足 / 磁盘已满 / 文件被占用）"
    else:
        reason = "该格式无法保存当前像素模式"
    return ValueError(
        f"保存失败（{os.path.basename(dst)}）：{reason}｜原始错误："
        f"{type(exc).__name__}: {text}")


__all__ = [
    "EXIF_SAVE_EXTS",
    "DPI_SAVE_EXTS",
    "ICC_SAVE_EXTS",
    "META_KEYS",
    "save_image",
    "write_image",
]
