"""图片输出编码（纯函数，无 Tk / 无 App 依赖）。

把 ``App._save_image`` / ``App._write_image`` 的业务逻辑搬到这里，使其可在无显示
环境下单测。逻辑与原文逐字一致：按扩展名选择保存方式、有损格式压平 alpha、EXIF
写回失败降级为「不带 EXIF 再存一次」。

``EXIF_SAVE_EXTS`` 定义为模块级 frozenset，``App._EXIF_SAVE_EXTS`` 直接引用它，
保证单处真源。
"""

from __future__ import annotations

import sys
from typing import Dict, Optional

from PIL import Image

from .. import media, render

#: 输出编码器支持接收 EXIF 的格式。注意 Pillow 当前不会从 TIFF 源提取出本工具可
#: 回写的 ``Document.exif_bytes``，所以 TIFF 源仍按无 EXIF 导出；保留 TIFF 在集合里
#: 仅表示调用方若显式提供 EXIF 字节，编码器可以写入，不改变既有行为。
EXIF_SAVE_EXTS = frozenset({".jpg", ".jpeg", ".png", ".webp", ".tif", ".tiff"})


def save_image(out: Image.Image, dst: str, exif: Optional[bytes] = None) -> None:
    """按输出扩展名选择合适的保存方式（有损格式压平 alpha）。

    ``exif`` 是源文件转正后的 EXIF 字节（``media.Document.exif_bytes``，PDF /
    无 EXIF 时为 None）。输出编码器可给 JPEG / PNG / WebP / TIFF 写 EXIF，但
    Pillow 当前不能从 TIFF 源提供可回写的 ``exif_bytes``，因此 TIFF 源仍按无
    EXIF 导出；GIF / BMP 没有 EXIF 容器，这里显式不传。
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
    write_image(image, dst, params)


def write_image(image: Image.Image, dst: str, params: Dict[str, object]) -> None:
    """落盘；EXIF 写失败时降级成「不带 EXIF 再存一次」。

    元数据写不进去不该让整张图导出失败 —— 水印才是主产物。降级原因打到
    stderr（冻结版进 runtime.log），不会静默丢元数据。
    """
    try:
        image.save(dst, **params)
    except Exception as exc:
        if "exif" not in params:
            raise
        fallback = {key: value for key, value in params.items() if key != "exif"}
        print(f"[WARN] EXIF 写入失败，已按无 EXIF 保存 {dst}：{exc}", file=sys.stderr)
        image.save(dst, **fallback)


__all__ = ["EXIF_SAVE_EXTS", "save_image", "write_image"]
