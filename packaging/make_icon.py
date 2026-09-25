"""把「图标原图」转成 Windows 可用的多尺寸 ``.ico`` + 预览 PNG。

用法：
    python packaging/make_icon.py [原图路径]

缺省会取 ``assets/`` 下最新的 PNG。产出：
    assets/app.ico       多尺寸图标（16/20/24/32/40/48/64/128/256）
    assets/app_256.png   256×256 预览（给人工过目用）

要点：
    * 图像模型即使声明 transparent，实际常给出**不透明白底**；而文档页本身也是
      白的，所以不能用「全局近白即透明」，必须从四角做**连通域泛洪**，只吃掉
      页面**外面**的背景，保留文档内部的白。
    * 去背后按内容包围盒居中并留边距，再生成各尺寸 —— 保证 16px 下主体不被裁到。
"""

from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageDraw

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ASSETS = PROJECT_ROOT / "assets"
OUT_ICO = ASSETS / "app.ico"
OUT_PNG = ASSETS / "app_256.png"

#: Windows 图标常用尺寸（含任务栏/资源管理器所需的多个档位）
SIZES = [(16, 16), (20, 20), (24, 24), (32, 32), (40, 40),
         (48, 48), (64, 64), (128, 128), (256, 256)]

#: 泛洪容差：太小啃不干净白底，太大可能穿透文档轮廓
FLOOD_THRESH = 32
#: 内容与图标边缘的留白比例
MARGIN_RATIO = 0.045


def pick_source() -> Path:
    if len(sys.argv) > 1:
        return Path(sys.argv[1])
    pngs = sorted(ASSETS.glob("*.png"), key=lambda p: p.stat().st_mtime)
    # 排除自己产出的预览图
    pngs = [p for p in pngs if p.name != OUT_PNG.name]
    if not pngs:
        raise SystemExit("assets/ 下没有可用的 PNG 原图")
    return pngs[-1]


def opaque_ratio(im: Image.Image) -> float:
    a = im.getchannel("A").point(lambda v: 255 if v > 8 else 0)
    data = list(a.getdata())
    return sum(1 for v in data if v) / max(1, len(data))


def strip_background(im: Image.Image) -> Image.Image:
    """去白底：从四角泛洪，只清除与页面外背景连通的白色区域。"""
    im = im.convert("RGBA")
    if im.getchannel("A").getextrema()[0] < 255:
        return im  # 原图自带透明通道，直接沿用

    w, h = im.size
    sentinel = (255, 0, 255, 0)  # 品红 + 全透明；不可见，仅作标记
    for seed in ((0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1)):
        try:
            ImageDraw.floodfill(im, seed, sentinel, thresh=FLOOD_THRESH)
        except Exception:
            pass
    return im


def center_square(im: Image.Image) -> Image.Image:
    """按内容包围盒居中成正方形，并留边距。"""
    mask = im.getchannel("A").point(lambda v: 255 if v > 8 else 0)
    bb = mask.getbbox()
    if bb is None:
        raise SystemExit("去背后图像为空，请检查原图或调大泛洪容差")
    im = im.crop(bb)

    w, h = im.size
    side = max(w, h)
    margin = round(side * MARGIN_RATIO)
    inner = max(1, side - 2 * margin)
    scale = inner / side
    new_w, new_h = max(1, round(w * scale)), max(1, round(h * scale))
    im = im.resize((new_w, new_h), Image.LANCZOS)

    canvas = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    canvas.paste(im, ((side - new_w) // 2, (side - new_h) // 2))
    return canvas


def main() -> int:
    src = pick_source()
    im = Image.open(src)
    print("原图:", src.name, im.mode, im.size)

    before = opaque_ratio(im.convert("RGBA"))
    im = strip_background(im)
    after = opaque_ratio(im)
    print("不透明像素占比: %.1f%% -> %.1f%%（去背 %s）"
          % (before * 100, after * 100, "生效" if after < before - 0.01 else "未生效"))

    im = center_square(im)
    print("居中后:", im.size, "| 内容包围盒:", im.getchannel('A').point(lambda v: 255 if v > 8 else 0).getbbox())

    ASSETS.mkdir(parents=True, exist_ok=True)
    im.save(OUT_ICO, format="ICO", sizes=SIZES)
    im.resize((256, 256), Image.LANCZOS).save(OUT_PNG)
    print("产出:", OUT_ICO, "|", OUT_PNG)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
