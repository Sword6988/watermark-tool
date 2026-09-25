"""P1-4 拆分后的无头单测：覆盖 ``wm.ui.output`` / ``wm.ui.preview_job`` / ``wm.ui.batch``。

这些模块不依赖 Tk，所以本文件**不** import ``wm.ui.app``，可在无显示环境下运行
（``tests/run_all.py`` 会自动发现并收集）。
"""

from __future__ import annotations

import os
import tempfile
from typing import List

from PIL import Image

from wm.ui import output, preview_job, batch
from wm.spec import WatermarkSpec

WHITE = (255, 255, 255)


def test_output_save_image_writes_file() -> None:
    """save_image 能把一张带水印的 RGBA 图落盘成合法图片。"""
    with tempfile.TemporaryDirectory() as tmp:
        dst = os.path.join(tmp, "out.png")
        img = Image.new("RGBA", (60, 40), (255, 0, 0, 128))
        output.save_image(img, dst)
        assert os.path.exists(dst), "输出文件未生成"
        reopened = Image.open(dst)
        reopened.load()
        assert reopened.size == (60, 40), f"尺寸不符：{reopened.size}"


def test_output_save_image_writes_exif_for_jpg() -> None:
    """复用 test_all.py 的 Orientation=6 模式：EXIF 写回、方向标签清除。"""
    from wm import media

    with tempfile.TemporaryDirectory() as tmp:
        source = os.path.join(tmp, "src.jpg")
        probe = Image.new("RGB", (40, 20), WHITE)
        exif = probe.getexif()
        exif[274] = 6               # Orientation = 竖拍（顺时针 90°）
        exif[315] = "wm-exif-probe"  # Artist
        probe.save(source, exif=exif)

        doc = media.Document(source)
        try:
            assert doc.exif_bytes, "转正后 EXIF 字节不应为空"
            out = os.path.join(tmp, "out.jpg")
            output.save_image(Image.new("RGBA", (20, 40)), out, exif=doc.exif_bytes)
            saved = Image.open(out).getexif()
            assert saved.get(315) == "wm-exif-probe", \
                f"其它 EXIF 条目必须保留，实际 {dict(saved)}"
            assert saved.get(274) in (None, 1), \
                f"方向标签必须清除（否则会二次旋转），实际 {saved.get(274)}"
        finally:
            doc.close()


def test_preview_job_renders_without_tk() -> None:
    """preview_job.render_preview 在无 Tk 下也能渲染出预览图。"""
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "page.png")
        Image.new("RGB", (100, 100), WHITE).save(path)

        result = preview_job.render_preview(path, 0, WatermarkSpec().normalized(), 300, 200)
        out, page_w, page_h = result
        assert out is not None, "预览图不应为 None"
        assert isinstance(out, Image.Image)
        # 显示尺寸应正且落在画布之内（avail = canvas - 2*12）
        assert out.size[0] >= 1 and out.size[1] >= 1
        assert out.size[0] <= 300 and out.size[1] <= 200, \
            f"预览超出画布：{out.size}"
        # 返回的页面尺寸应与源图一致（100x100）
        assert (page_w, page_h) == (100.0, 100.0), f"页面尺寸错误：{(page_w, page_h)}"


class _BatchResult:
    """run_batch 的 on_done 回调产物（与 on_done(succeeded, failed, cancelled) 对齐）。"""

    def __init__(self, succeeded: int, failed: List, cancelled: bool) -> None:
        self.succeeded = succeeded
        self.failed = failed
        self.cancelled = cancelled


def test_batch_runs_and_cancels() -> None:
    """run_batch：正常跑完 2 个文件；以及一开始就取消要立即停。"""
    with tempfile.TemporaryDirectory() as tmp:
        files = []
        for i in range(2):
            p = os.path.join(tmp, f"img{i}.png")
            Image.new("RGB", (80, 60), WHITE).save(p)
            files.append(p)

        # -- 正常运行 --
        logs: List[str] = []
        done: List[_BatchResult] = []
        batch.run_batch(
            files, WatermarkSpec().normalized(), tmp,
            is_cancelled=lambda: False,
            on_progress=lambda *a, **k: None,
            on_log=logs.append,
            on_done=lambda s, f, c: done.append(_BatchResult(s, f, c)),
        )
        assert done, "on_done 未回调"
        assert done[0].succeeded == 2, f"成功数应为 2，实际 {done[0].succeeded}"
        assert done[0].cancelled is False
        produced = [n for n in os.listdir(tmp) if "_watermarked" in n]
        assert len(produced) == 2, f"产出文件数不对：{len(produced)}"

        # -- 一开始就取消：应立即停，不产生任何输出 --
        logs2: List[str] = []
        done2: List[_BatchResult] = []
        batch.run_batch(
            files, WatermarkSpec().normalized(), tmp,
            is_cancelled=lambda: True,
            on_progress=lambda *a, **k: None,
            on_log=logs2.append,
            on_done=lambda s, f, c: done2.append(_BatchResult(s, f, c)),
        )
        assert done2, "on_done 未回调"
        assert done2[0].succeeded == 0, f"取消时成功数应为 0，实际 {done2[0].succeeded}"
        assert done2[0].cancelled is True
        # 不应新增任何 _watermarked 文件（首轮已生成 2 个；这里仍是 2 个）
        produced2 = [n for n in os.listdir(tmp) if "_watermarked" in n]
        assert len(produced2) == 2, f"取消后仍产生了输出：{len(produced2)}"


__all__ = [
    "test_output_save_image_writes_file",
    "test_output_save_image_writes_exif_for_jpg",
    "test_preview_job_renders_without_tk",
    "test_batch_runs_and_cancels",
]
