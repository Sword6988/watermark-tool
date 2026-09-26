"""加密文件（DLP / 透明加密）自动解密链路的回归用例。

覆盖的是「用户添加文件即自动解密」这条链路，重点是四条行为契约：

1. 两个入口（「选择文件」按钮 / 拖拽投放）走**同一条**解密链路；
2. 先探测再解密 —— 未加密文件零额外开销、行为不变；
3. 批量时单个文件解密失败只跳过它自己，不影响其它文件与工具；
4. 原文件**始终不被改写**，明文只落在受控临时目录且会被清理。

用例里的解密器是**假提供者**（不碰任何真实 DLP），因此不依赖内网环境即可回归。
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from typing import Dict, List, Optional

from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from wm import dlp, media
from wm.spec import WatermarkSpec
from wm.ui import batch

WHITE = (255, 255, 255)
MAGIC = dlp.DEFAULT_MAGIC


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------

def _make_root():
    """创建根窗口；缺 tkinterdnd2 / 无显示环境时抛 SkipTest（**不**算通过）。"""
    try:
        import tkinterdnd2
        return tkinterdnd2.TkinterDnD.Tk()
    except Exception as exc:
        raise unittest.SkipTest("需要 tkinterdnd2 / 可用显示环境：%s" % exc)


def _png_bytes(size=(40, 30)) -> bytes:
    """生成一张 PNG 的原始字节（用作「明文」）。"""
    import io
    buffer = io.BytesIO()
    Image.new("RGB", size, WHITE).save(buffer, format="PNG")
    return buffer.getvalue()


def _write_encrypted(path: str, payload: bytes) -> str:
    """写一个「加密文件」：加密魔数 + 明文内容（本用例里解密 = 剥掉前 3 字节）。"""
    with open(path, "wb") as handle:
        handle.write(MAGIC + payload)
    return path


class _FakeProvider(dlp.DecryptProvider):
    """可控的假解密器。

    ``payloads`` 按**文件基名**给出解密后的明文；``mode`` 控制失败形态。
    """

    name = "fake"

    def __init__(self, payloads: Optional[Dict[str, bytes]] = None,
                 mode: str = "ok", error: str = "假解密器报错") -> None:
        self.payloads = payloads or {}
        self.mode = mode
        self.error = error
        self.calls: List[tuple] = []

    def available(self) -> bool:
        return self.mode != "unavailable"

    def decrypt(self, src: str, dst: str) -> None:
        self.calls.append((src, dst))
        if self.mode == "raise":
            raise dlp.DecryptError(self.error)
        payload = self.payloads.get(os.path.basename(src))
        if payload is None:
            raise dlp.DecryptError("解密器不认识这个文件")
        if self.mode == "garbage":
            payload = b"\x01\x02\x03" * 32  # 长度够，但文件头不是任何已知格式
        if self.mode == "empty":
            payload = b""
        with open(dst, "wb") as handle:
            handle.write(payload)


def _use_provider(provider) -> None:
    dlp.set_provider(provider)


# ---------------------------------------------------------------------------
# 用例
# ---------------------------------------------------------------------------

def test_dlp_magic_detection_only_looks_at_the_head():
    """加密探测：只看文件头 3 字节；非加密文件一律判为「未加密」。"""
    with tempfile.TemporaryDirectory() as tmp:
        plain = os.path.join(tmp, "plain.png")
        with open(plain, "wb") as handle:
            handle.write(_png_bytes())
        secret = _write_encrypted(os.path.join(tmp, "secret.png"), _png_bytes())
        assert dlp.is_encrypted(plain) is False, "正常 PNG 不得被判为加密"
        assert dlp.is_encrypted(secret) is True, "魔数文件必须被判为加密"
        assert dlp.is_encrypted(os.path.join(tmp, "missing.png")) is False, \
            "文件不存在时按「未加密」处理（交给后续打开阶段报错）"


def test_dlp_plain_file_keeps_the_original_path():
    """未加密文件：解析结果仍是原路径，不产生任何临时文件。"""
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "plain.png")
        with open(path, "wb") as handle:
            handle.write(_png_bytes())
        result = dlp.resolve(path)
        assert result.state == dlp.STATE_PLAIN
        assert result.path == path and result.read_path == path


def test_dlp_add_files_decrypts_automatically():
    """添加文件即自动解密：进列表的是原路径，读取的是临时明文。"""
    root = _make_root()
    previous = dlp.get_provider()
    try:
        from wm.ui import app as ui_app
        with tempfile.TemporaryDirectory() as tmp:
            payload = _png_bytes()
            secret = _write_encrypted(os.path.join(tmp, "secret.png"), payload)
            _use_provider(_FakeProvider({"secret.png": payload}))
            application = ui_app.App(root)
            application.add_files([secret])
            root.update_idletasks()

            assert application.files == [secret], "原路径进列表（输出命名靠它）"
            plain = application._read_path(secret)
            assert plain != secret and os.path.isfile(plain), "应有临时明文"
            with open(plain, "rb") as handle:
                assert handle.read() == payload, "明文内容必须正确"
            with open(secret, "rb") as handle:
                assert handle.read() == MAGIC + payload, "原文件绝不能被改写"

            document = media.Document(secret, read_path=plain)
            try:
                assert document.page_size(0) == (40.0, 30.0), "明文必须能被正常解析"
            finally:
                document.close()
    finally:
        dlp.set_provider(previous)
        try:
            if root.winfo_exists():
                root.destroy()
        except Exception:
            pass


def test_dlp_button_and_drop_share_one_decrypt_path():
    """两个入口行为一致：「选择文件」与拖拽投放都经过 _add_paths 的解密。"""
    root = _make_root()
    previous = dlp.get_provider()
    try:
        from wm.ui import app as ui_app
        with tempfile.TemporaryDirectory() as tmp:
            payload = _png_bytes((24, 18))
            dragged = _write_encrypted(os.path.join(tmp, "dragged.png"), payload)
            picked = _write_encrypted(os.path.join(tmp, "picked.png"), payload)
            provider = _FakeProvider({"dragged.png": payload, "picked.png": payload})
            _use_provider(provider)

            application = ui_app.App(root)
            calls = {"add_paths": 0}
            original_add_paths = application._add_paths

            def _counting_add_paths(paths):
                calls["add_paths"] += 1
                return original_add_paths(paths)

            application._add_paths = _counting_add_paths  # 实例属性优先于类方法

            # 入口一：拖拽投放
            application._on_drop(type("E", (), {"data": "{%s}" % dragged})())
            # 入口二：「选择文件」按钮（替换文件对话框的返回值）
            original_dialog = ui_app.filedialog.askopenfilenames
            ui_app.filedialog.askopenfilenames = lambda **_kw: (picked,)
            try:
                application._choose_files()
            finally:
                ui_app.filedialog.askopenfilenames = original_dialog

            assert calls["add_paths"] == 2, "两个入口都必须经过 _add_paths"
            assert application.files == [dragged, picked]
            assert len(provider.calls) == 2, "两个入口的文件都应被解密"
            for path in (dragged, picked):
                assert application._read_path(path) != path, f"{path} 未走明文路径"
    finally:
        dlp.set_provider(previous)
        try:
            if root.winfo_exists():
                root.destroy()
        except Exception:
            pass


def test_dlp_failed_file_is_skipped_but_others_continue():
    """批量中某个文件解密失败：只跳过它，其它文件照常进入列表。"""
    root = _make_root()
    previous = dlp.get_provider()
    try:
        from wm.ui import app as ui_app
        with tempfile.TemporaryDirectory() as tmp:
            good = _write_encrypted(os.path.join(tmp, "good.png"), _png_bytes())
            bad = _write_encrypted(os.path.join(tmp, "bad.png"), _png_bytes())
            provider = _FakeProvider({"good.png": _png_bytes()})
            _use_provider(provider)

            application = ui_app.App(root)
            warnings: List[str] = []
            original_warning = ui_app.messagebox.showwarning
            ui_app.messagebox.showwarning = lambda _t, msg, **_kw: warnings.append(msg)
            try:
                application.add_files([good, bad])
            finally:
                ui_app.messagebox.showwarning = original_warning

            assert application.files == [good], "解密失败的文件不得进列表"
            assert application._read_path(good) != good, "成功的文件仍要走明文"
            assert len(warnings) == 1, "失败必须明确提示（且只弹一次汇总）"
            assert "bad.png" in warnings[0], "提示里要有文件名"
            assert "解密器不认识这个文件" in warnings[0], "提示里要有失败原因"
    finally:
        dlp.set_provider(previous)
        try:
            if root.winfo_exists():
                root.destroy()
        except Exception:
            pass


def test_dlp_corrupted_or_empty_plaintext_is_rejected():
    """解密产物损坏 / 为空：按失败处理并跳过，绝不把坏文件交给后续流程。"""
    root = _make_root()
    previous = dlp.get_provider()
    try:
        from wm.ui import app as ui_app
        with tempfile.TemporaryDirectory() as tmp:
            broken = _write_encrypted(os.path.join(tmp, "broken.png"), _png_bytes())
            empty = _write_encrypted(os.path.join(tmp, "empty.png"), _png_bytes())
            _use_provider(_FakeProvider({"broken.png": b"x", "empty.png": b"y"},
                                        mode="garbage"))
            warnings: List[str] = []
            application = ui_app.App(root)
            original_warning = ui_app.messagebox.showwarning
            ui_app.messagebox.showwarning = lambda _t, msg, **_kw: warnings.append(msg)
            try:
                application.add_files([broken])
            finally:
                ui_app.messagebox.showwarning = original_warning
            assert application.files == [], "损坏的解密产物不得进列表"
            assert "文件头损坏" in warnings[0]

            _use_provider(_FakeProvider({"empty.png": b"y"}, mode="empty"))
            result = dlp.resolve(empty)
            assert result.path is None and "为空" in result.message
    finally:
        dlp.set_provider(previous)
        try:
            if root.winfo_exists():
                root.destroy()
        except Exception:
            pass


def test_dlp_without_provider_encrypted_file_is_skipped_with_reason():
    """未配置解密器且文件确实读不开：明确提示「当前环境无法解密」并跳过。"""
    root = _make_root()
    previous = dlp.get_provider()
    try:
        from wm.ui import app as ui_app
        with tempfile.TemporaryDirectory() as tmp:
            secret = _write_encrypted(os.path.join(tmp, "secret.png"), _png_bytes())
            _use_provider(None)
            result = dlp.resolve(secret)
            assert result.path is None, "解不了就不能进列表"
            assert "未配置解密器" in result.message, "原因要可行动（提示找谁授权）"

            warnings: List[str] = []
            application = ui_app.App(root)
            original_warning = ui_app.messagebox.showwarning
            ui_app.messagebox.showwarning = lambda _t, msg, **_kw: warnings.append(msg)
            try:
                application.add_files([secret])
            finally:
                ui_app.messagebox.showwarning = original_warning
            assert application.files == []
            assert "secret.png" in warnings[0]
    finally:
        dlp.set_provider(previous)
        try:
            if root.winfo_exists():
                root.destroy()
        except Exception:
            pass


def test_dlp_readable_file_flagged_as_encrypted_still_uses_original():
    """命中魔数但读得开（透明解密已生效 / 魔数误判）：按原样走，不动临时文件。"""
    previous = dlp.get_provider()
    original_probe = dlp.probe_readable
    try:
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_encrypted(os.path.join(tmp, "direct.png"), _png_bytes())
            dlp.probe_readable = lambda _p: None  # 模拟「读得开」（白名单生效 / 误判）
            result = dlp.resolve(path)
            assert result.state == dlp.STATE_DIRECT
            assert result.path == path and result.read_path == path
    finally:
        dlp.probe_readable = original_probe
        dlp.set_provider(previous)


def test_dlp_plaintext_is_released_on_clear_and_close():
    """从列表移除 / 清空 / 关窗时，临时明文必须被删除（不留明文在磁盘上）。"""
    root = _make_root()
    previous = dlp.get_provider()
    try:
        from wm.ui import app as ui_app
        with tempfile.TemporaryDirectory() as tmp:
            payload = _png_bytes()
            first = _write_encrypted(os.path.join(tmp, "first.png"), payload)
            second = _write_encrypted(os.path.join(tmp, "second.png"), payload)
            _use_provider(_FakeProvider({"first.png": payload, "second.png": payload}))
            application = ui_app.App(root)
            application.add_files([first, second])
            kept = application._read_path(first)
            assert os.path.isfile(kept)

            application.files_list.selection_clear(0, "end")
            application.files_list.selection_set(0)
            application._remove_selected()
            assert not os.path.isfile(kept), "移出列表后明文必须删除"
            assert first not in application._plain

            remaining = application._read_path(second)
            assert os.path.isfile(remaining)
            application._release_resources()
            assert not os.path.isfile(remaining), "关窗后明文必须删除"
            assert application._plain == {}
    finally:
        dlp.set_provider(previous)
        try:
            if root.winfo_exists():
                root.destroy()
        except Exception:
            pass


def test_dlp_command_provider_never_touches_the_source():
    """真实形态的解密器（外部命令）也绝不改写原文件：交给它的只是临时副本。

    假命令模拟 LDDec 的行为：把明文写到 ``<输入>.dec_``（不写 ``{dst}``），
    验证适配层仍能接管产物，且原文件保持密文不变。
    """
    with tempfile.TemporaryDirectory() as tmp:
        payload = _png_bytes((36, 24))
        secret = _write_encrypted(os.path.join(tmp, "secret.png"), payload)
        script = os.path.join(tmp, "fake dec.py")
        with open(script, "w", encoding="utf-8") as handle:
            handle.write(
                "import sys\n"
                "src, dst = sys.argv[1], sys.argv[2]\n"
                "data = open(src, 'rb').read()\n"
                "open(src + '.dec_', 'wb').write(data[3:])\n")
        template = '"%s" "%s" "{src}" "{dst}"' % (sys.executable, script)
        provider = dlp.CommandProvider(template, timeout=30)
        assert provider.available(), "带空格路径的命令模板应能正确切分 argv"

        dst = dlp.new_plaintext_path(secret)
        try:
            provider.decrypt(secret, dst)
            with open(dst, "rb") as handle:
                assert handle.read() == payload, "应接管 <输入>.dec_ 形式的产物"
            with open(secret, "rb") as handle:
                assert handle.read() == MAGIC + payload, "原文件必须保持密文"
            assert not os.path.exists(secret + ".dec_"), "不得在原文件旁留产物"
        finally:
            dlp.release(dst)


def test_dlp_batch_reads_plaintext_but_names_output_from_source():
    """批处理：读明文，但输出仍按**原文件**命名（绝不会写进临时目录）。"""
    with tempfile.TemporaryDirectory() as tmp:
        payload = _png_bytes((48, 32))
        secret = _write_encrypted(os.path.join(tmp, "secret.png"), payload)
        plain = os.path.join(tmp, "plain.png")
        with open(plain, "wb") as handle:
            handle.write(payload)

        logs: List[str] = []
        done = {}
        batch.run_batch(
            [secret], WatermarkSpec(text="密"), None,
            is_cancelled=lambda: False,
            on_progress=lambda *_a: None,
            on_log=logs.append,
            on_done=lambda s, f, c: done.update(succeeded=s, failed=f, cancelled=c),
            read_path=lambda _p: plain)
        assert done.get("succeeded") == 1 and not done.get("failed"), logs
        expected = os.path.join(tmp, "secret_水印版.png")
        assert os.path.isfile(expected), "输出必须落在原文件旁、沿用原文件名"
