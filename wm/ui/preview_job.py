"""预览渲染（纯函数，无 Tk / 无 App 依赖）。

把 ``App._preview_worker`` 里的「渲染」部分搬到这里：打开文档、按画布尺寸 fit-to-
canvas 计算缩放、渲染底图与水印层并合成。返回 ``(Image, page_w, page_h)``，由调用方
（``App._preview_worker``）负责把它塞进队列 / 决定如何上报错误。

此处**不**碰队列、不碰 Tk：打开失败只返回 ``(None, page_w, page_h)``，让调用方决定
怎么显示（调用方据此推一个 error 消息），绝不向外抛异常。
"""

from __future__ import annotations

from typing import Callable, Optional, Tuple

from PIL import Image

from .. import media, render
from ..spec import WatermarkSpec


#: 哨兵返回值：表示「这一帧被取消了」。
#:
#: 与 ``None``（**渲染失败**）必须区分开：取消是正常流程（用户又改了参数），既不该
#: 贴图也不该报「预览失败」；失败则是要让用户看见的。两者混同会让快速拖滑杆时
#: 预览区反复闪红。
CANCELLED = object()


def render_preview(
    path: str, page: int, spec: WatermarkSpec, canvas_w: int, canvas_h: int,
    is_cancelled: "Optional[Callable[[], bool]]" = None,
) -> Tuple[Optional[Image.Image], int, int]:
    """渲染单页预览图（RGB）。

    公式与 ``App._preview_worker`` 完全一致：``avail = max(40, canvas - 2*12)``，
    ``scale = min(avail_w/page_w, avail_h/page_h)`` 夹到 ``[0.02, 4.0]``，底图按
    ``scale`` 缩到显示尺寸，水印层用 ``render.render_overlay_layer`` 渲染后缩到同一
    尺寸，alpha 合成后转 RGB。

    ``is_cancelled``：把取消语义一路传到渲染**内部** —— 否则大预览只能「跑完再
    丢」，而最该早点停下来的恰恰是那种一帧就几百 MB 的超大图。取消时立刻返回
    ``(None, 0, 0)``，与「渲染失败」同样处理（调用方本就靠 ``gen`` 区分过期帧）。

    返回 ``(out, page_w, page_h)``；打开 / 渲染失败返回 ``(None, page_w, page_h)``
    （失败时 ``page_w`` / ``page_h`` 取不到，记 0）。
    """
    cancelled = False

    def _check() -> bool:
        nonlocal cancelled
        if is_cancelled is not None and is_cancelled():
            cancelled = True
        return cancelled

    try:
        doc = media.Document(path)
        try:
            page_w, page_h = doc.page_size(page)
            pad = 12
            avail_w = max(40, canvas_w - 2 * pad)
            avail_h = max(40, canvas_h - 2 * pad)
            scale = min(avail_w / page_w, avail_h / page_h)
            scale = max(0.02, min(scale, 4.0))
            disp_w = max(1, int(round(page_w * scale)))
            disp_h = max(1, int(round(page_h * scale)))
            if _check():
                return CANCELLED, 0, 0
            if doc.kind == media.KIND_PDF:
                dpi = max(20, int(round(72 * scale)))
                base = doc.page_image(page, dpi=dpi).convert("RGBA")
            else:
                base = doc.page_image(page).convert("RGBA")
            if base.size != (disp_w, disp_h):
                base = base.resize((disp_w, disp_h), Image.LANCZOS)
            if _check():
                return CANCELLED, 0, 0
            layer = render.render_overlay_layer(page_w, page_h, spec, scale=scale,
                                                is_cancelled=_check)
            if layer.size != base.size:
                layer = layer.resize(base.size, Image.LANCZOS)
            out = Image.alpha_composite(base, layer).convert("RGB")
        finally:
            doc.close()
        return out, page_w, page_h
    except render.Cancelled:
        # 渲染内部（图层）检查到取消：这就是「这一帧不要了」，不是失败
        return CANCELLED, 0, 0
    except Exception:
        # 打开 / 渲染失败：不抛，交给调用方决定如何上报（原始 _preview_worker 推
        # 的是一个 error 消息）。page_w / page_h 取不到就记 0。
        return None, 0, 0


__all__ = ["CANCELLED", "render_preview"]
