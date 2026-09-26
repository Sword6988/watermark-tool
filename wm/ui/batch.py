"""批处理编排（纯逻辑，无 Tk / 无 App 依赖）。

把 ``App._batch_worker`` 的循环体搬到这里：逐文件打开、规划输出、PDF 走
``render.render_pdf``、图片走 ``render.render_image`` + ``output.save_image``，捕获
``render.Cancelled``（取消）与其它异常（记失败），并在每步通过回调把进度 / 日志 /
完成事件推给调用方。

取消与所有 UI 副作用都通过回调参数注入（``is_cancelled`` / ``on_progress`` /
``on_log`` / ``on_done``），本模块自身完全不碰 ``queue``、不碰 Tk，可无头单测。
"""

from __future__ import annotations

import os
from typing import Callable, List, Optional, Tuple

from .. import media, render
from ..spec import DEFAULT_SUFFIX, WatermarkSpec
from . import output

ProgressFn = Callable[[int, int, str, int], None]
LogFn = Callable[[str], None]
DoneFn = Callable[[int, "list", bool], None]
CancelFn = Callable[[], bool]


def run_batch(
    files: List[str],
    spec: WatermarkSpec,
    out_dir: Optional[str],
    *,
    is_cancelled: CancelFn,
    on_progress: ProgressFn,
    on_log: LogFn,
    on_done: DoneFn,
    suffix: str = DEFAULT_SUFFIX,
) -> None:
    """逐文件加水印并写出；通过回调上报进度 / 日志 / 完成。

    进度回调 ``on_progress(done, count, label, index)`` 同时服务于两种来源：
      * PDF 内部逐页进度（``done``=已完成页数，``count``=总页数，``label``=页码提示，
        ``index``=当前文件下标）；
      * 每个文件处理完后的整体进度（``done=count=1``、``label=""``、``index``=文件下标），
        由调用方据此重建 ``(index + 1) / total`` 的整条进度。

    ``suffix`` 是输出文件名的中缀（默认 ``_水印版``）。UI 不再提供后缀输入框，
    传非默认值前请用 :func:`wm.spec.safe_suffix` 清理 —— 本函数不做净化，
    但它也不会改变任何目录：后缀只参与文件名拼接。
    """
    total = len(files)
    succeeded = 0
    failed: "list" = []
    for index, path in enumerate(files):
        if is_cancelled():
            break
        try:
            doc = media.Document(path)
            try:
                dst = media.plan_output(path, out_dir, suffix=suffix)
                if doc.kind == media.KIND_PDF:
                    pages = render.render_pdf(
                        path, dst, spec,
                        progress=lambda done, count, label, i=index: on_progress(
                            done, count, label, i),
                        is_cancelled=is_cancelled)
                    note = f"{pages} 页"
                else:
                    out = render.render_image(doc.page_image(0), spec)
                    # 元数据（EXIF / DPI / ICC）由 media.Document 提供，缺失为 None；
                    # 用 getattr 兼容对方尚未落地的版本，属性缺失就按无该项存。
                    output.save_image(
                        out, dst,
                        exif=getattr(doc, "exif_bytes", None),
                        dpi=getattr(doc, "dpi", None),
                        icc_profile=getattr(doc, "icc_profile", None))
                    note = (f"仅首帧（共 {doc.frame_count} 帧）" if doc.frame_count > 1
                            else "图片")
            finally:
                doc.close()
            succeeded += 1
            on_log(f"[OK] {os.path.basename(path)} -> {os.path.basename(dst)}（{note}）")
        except render.Cancelled:
            on_log("[FAIL] 已取消")
            break
        except Exception as exc:
            failed.append((path, str(exc)))
            on_log(f"[FAIL] {os.path.basename(path)}：{exc}")
        on_progress(1, 1, "", index)
    on_done(succeeded, failed, is_cancelled())


__all__ = ["run_batch"]
