"""核心基础设施单测：``wm/lru.py``、``wm/fonts.py``、``wm/ui/panel.py``。

这三个模块此前**没有任何直接用例**（审计 M22）：缓存语义只在渲染路径里被间接
覆盖，字体回退只在真机上"看着对"，滚动容器的销毁顺序更是完全没测。它们一旦退化，
表现都是**偶发且难复现**的问题（缓存抖动、中文变豆腐块、销毁后 Tcl 报
``invalid command name``），所以在这里按行为契约直接钉住。

无 Tk 依赖的部分可在无显示环境运行；``panel`` 相关用例按现有约定抛
``SkipTest``（**不**算通过）。
"""

from __future__ import annotations

import os
import sys
import threading
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from wm.lru import SizedLRU


# ---------------------------------------------------------------------------
# wm/lru.py
# ---------------------------------------------------------------------------

def _cache(budget, **kw) -> SizedLRU:
    """建一个「按字节长度计费」的缓存。"""
    return SizedLRU(len, budget, **kw)


def test_lru_is_true_lru_not_fifo() -> None:
    """命中必须刷新次序：被复用的旧条目不能先被挤掉（旧实现是 FIFO，会误淘汰）。"""
    cache = _cache(3)
    cache.set("a", "x")   # 1 字节
    cache.set("b", "x")
    cache.set("c", "x")
    assert cache.get("a") == "x", "刚放进去的条目应能取到"
    cache.set("d", "x")   # 超限 → 淘汰最旧的
    assert "a" in cache, "a 刚被命中，应视为最新的，不能被淘汰（FIFO 会错杀它）"
    assert "b" not in cache, "b 是最旧的，应被淘汰"
    assert len(cache) == 3, f"容量应回到 3，实际 {len(cache)}"


def test_lru_oversized_entry_is_not_cached() -> None:
    """单条就超预算时不入缓存（否则它会先把别人全清光、再被自己挤掉，反复重渲染）。"""
    cache = _cache(5)
    assert cache.set("big", "0123456789") is False, "超限条目不应入缓存"
    assert len(cache) == 0, "未入缓存则条目数为 0"
    assert cache.bytes == 0, f"字节账目也应保持 0，实际 {cache.bytes}"


def test_lru_budget_can_be_callable_and_is_evaluated_lazily() -> None:
    """预算可以是无参函数：测试把常量压小时才生效（构造时固化就会失效）。"""
    state = {"budget": 10}
    cache = _cache(lambda: state["budget"])
    cache.set("k", "12345")
    assert len(cache) == 1
    state["budget"] = 2          # 运行时收紧预算
    cache.set("k2", "1")
    assert len(cache) <= 1, f"预算收紧后应触发裁剪，实际 {len(cache)}"


def test_lru_overwrite_same_key_keeps_byte_account_exact() -> None:
    """覆盖同键必须扣掉旧计量，否则字节账目会单调膨胀、误判超限。"""
    cache = _cache(100)
    cache.set("k", "aa")     # 2
    cache.set("k", "bbbb")   # 覆盖为 4
    assert cache.bytes == 4, f"覆盖后应为 4 字节，实际 {cache.bytes}"
    assert len(cache) == 1, "同键只应有一条"


def test_lru_min_keep_protects_newest_when_over_budget() -> None:
    """``min_keep`` 保留最新那条 —— 渲染路径「先入缓存、再用」，裁它会拿到已释放对象。"""
    cache = _cache(1, min_keep=1)
    cache.set("old", "x")
    cache.set("new", "x")
    assert "new" in cache, "最新一条必须留住"
    assert "old" not in cache, "最旧的应被裁掉"
    assert cache.bytes <= 1, f"应压回预算内，实际 {cache.bytes}"


def test_lru_concurrent_set_get_is_safe() -> None:
    """并发读写不得抛异常（旧实现 ``next(iter(d.items()))`` 会随机 KeyError）。"""
    cache = _cache(200)
    errors: list = []

    def worker(tag: int) -> None:
        try:
            for i in range(200):
                key = ("k", i % 50)
                cache.set(key, "x" * (i % 7 + 1))
                cache.get(key)
                cache.bytes
        except Exception as exc:  # pragma: no cover - 只在出错时到这
            errors.append("%d: %r" % (tag, exc))

    threads = [threading.Thread(target=worker, args=(t,)) for t in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=20)
    assert not errors, "并发访问抛异常：%s" % errors[:3]
    assert cache.bytes >= 0, f"字节账目不能为负：{cache.bytes}"
    assert cache.bytes <= 200, f"必须落在预算内：{cache.bytes}"


def test_lru_clear_resets_bytes_and_length() -> None:
    """clear 必须把字节账目一起归零（切换文档时用，漏归零会让后续全部判超限）。"""
    cache = _cache(100)
    cache.set("a", "xx")
    cache.set("b", "yyy")
    assert cache.bytes == 5
    cache.clear()
    assert len(cache) == 0 and cache.bytes == 0, "clear 后条目与字节都应归零"


# ---------------------------------------------------------------------------
# wm/fonts.py
# ---------------------------------------------------------------------------

def test_fonts_resolve_returns_usable_font_file() -> None:
    """``resolve`` 必须给到**存在**的字体文件；否则渲染层会静默排不出字。"""
    from wm import fonts

    info = fonts.resolve(None)
    assert info is not None, "本机至少应有一个可用字体"
    path, index = info
    assert os.path.isfile(path), f"解析出的字体文件不存在：{path}"
    assert isinstance(index, int) and index >= 0, f"ttc 索引非法：{index}"


def test_fonts_pil_font_cache_is_bounded_and_reused() -> None:
    """字体对象缓存必须有**条目上限**且按 (家族, 尺寸) 复用（旧实现到 128 条整表清空）。"""
    from wm import fonts

    fonts.clear_cache()
    first = fonts.pil_font(None, 24)
    again = fonts.pil_font(None, 24)
    assert first is again, "相同 (家族, 尺寸) 应命中缓存返回同一对象"

    for size in range(1000, 1000 + fonts._FONT_CACHE_CAP + 40):
        fonts.pil_font(None, size)
    assert len(fonts._font_cache) <= fonts._FONT_CACHE_CAP, \
        f"缓存必须受上限约束，实际 {len(fonts._font_cache)} > {fonts._FONT_CACHE_CAP}"


def test_fonts_can_render_cjk_returns_bool_and_caches() -> None:
    """中文覆盖判定返回布尔并写缓存；未知家族不得抛异常。"""
    from wm import fonts

    fonts.clear_cache()
    family = fonts.default_family()
    assert isinstance(fonts.can_render_cjk(family), bool), "判定结果必须是布尔"
    assert family in fonts._cjk_map, "判定结果应进缓存（否则每次都重新加载字体）"
    # 未知家族不抛异常、且仍返回布尔。注意它**不是** False：``can_render_cjk``
    # 内部走 ``resolve``，未知家族会回退到优先字体，于是结果是「回退后那个字体
    # 能不能排中文」。本用例钉住的是「不抛异常 + 类型是布尔」，不是具体取值。
    unknown = fonts.can_render_cjk("__NoSuchFont__")
    assert isinstance(unknown, bool), f"未知家族也须返回布尔，实际 {unknown!r}"
    assert fonts.can_render_cjk("") is False, "空家族名应判 False"


def test_fonts_clear_cache_resets_all_caches() -> None:
    """clear_cache 要同时清家族映射 / 字体对象 / 中文判定三张表。"""
    from wm import fonts

    fonts.pil_font(None, 20)
    fonts.can_render_cjk(fonts.default_family())
    assert len(fonts._font_cache) > 0
    fonts.clear_cache()
    assert len(fonts._font_cache) == 0, "字体对象缓存未清空"
    assert len(fonts._cjk_map) == 0, "中文判定缓存未清空"


# ---------------------------------------------------------------------------
# wm/ui/panel.py
# ---------------------------------------------------------------------------

def test_panel_scrolled_frame_cancels_wheel_job_on_destroy() -> None:
    """销毁时必须先撤掉滚轮合并定时器 —— 否则 16ms 后回调会碰已销毁的 canvas。

    表现为 Tcl ``invalid command name ".!...canvas"``：窗口已经关了，控制台还在
    刷错误，用户侧看就是"关窗后偶发报错"。
    """
    try:
        import tkinter as tk
        root = tk.Tk()
    except Exception as exc:
        raise unittest.SkipTest("需要可用显示环境：%s" % exc)

    try:
        from wm.ui.panel import ScrolledFrame

        root.geometry("320x240")
        frame = ScrolledFrame(root)
        frame.pack(fill="both", expand=True)
        root.update_idletasks()
        root.update()

        frame.accumulate(120.0)          # 挂一个 16ms 后的合并回调
        assert frame._wheel_job is not None, "累积滚动量后应挂上待执行的定时器"
        frame.destroy()
        assert frame._wheel_job is None, "destroy 必须撤掉定时器并清空句柄"

        # 定时器真被撤掉的话，等它本该触发的时刻过后也不该有任何 Tcl 报错
        root.update()
        assert ScrolledFrame._LIVE_COUNT >= 0, "实例计数不应为负"
    finally:
        try:
            root.destroy()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# 非 ASCII 路径（模拟中文用户名机器）
# ---------------------------------------------------------------------------

def test_ascii_temp_root_returns_pure_ascii_dir() -> None:
    """临时根必须是纯 ASCII：这是"中文用户名机器"上唯一能主动消除的隐患。"""
    from wm import dlp

    root = dlp._ascii_temp_root()
    if root is None:
        raise unittest.SkipTest("本机找不到任何纯 ASCII 临时目录（罕见）")
    assert root.isascii(), root
    assert os.path.isdir(root), root


def test_cmdtype_reads_non_ascii_source_path_byte_exact() -> None:
    """中文（含 emoji）**原文件**路径要能字节保真地读出来。

    用户文件叫「PLM验收单.pdf」是常态；工具内部只读 ASCII 副本，所以这条测的是
    "从中文路径复制到 ASCII 副本"这一段。
    """
    import tempfile

    from wm import dlp

    provider = dlp.CmdTypeProvider()
    if not provider.available():
        raise unittest.SkipTest("当前系统没有 cmd.exe")
    with tempfile.TemporaryDirectory() as tmp:
        cn_dir = os.path.join(tmp, "中文 目录\U0001f512")
        os.makedirs(cn_dir, exist_ok=True)
        src = os.path.join(cn_dir, "PLM验收单\U0001f512.pdf")
        blob = dlp.magic() + b"%PDF-1.6\n" + os.urandom(2048)
        with open(src, "wb") as handle:
            handle.write(blob)

        dst = dlp.new_plaintext_path(src)
        try:
            provider.decrypt(src, dst)
            assert os.path.isfile(dst), "没有产出文件"
            assert os.path.getsize(dst) == len(blob), (
                "字节数不一致：%d != %d" % (os.path.getsize(dst), len(blob)))
            with open(dst, "rb") as handle:
                assert handle.read() == blob, "内容不是字节保真"
        finally:
            dlp.release(dst)


# ---------------------------------------------------------------------------
# 带口令的 PDF（审计 §四 第 5 项）
# ---------------------------------------------------------------------------

def _make_pdf(path: str, password: str = "") -> None:
    """写一个一页的 PDF；给了 ``password`` 就用 AES-256 加打开口令。"""
    import pymupdf

    doc = pymupdf.open()
    try:
        page = doc.new_page()
        page.insert_text((72, 72), "WATERMARK TEST", fontsize=24)
        if password:
            doc.save(path, encryption=pymupdf.PDF_ENCRYPT_AES_256,
                     owner_pw=password, user_pw=password)
        else:
            doc.save(path)
    finally:
        doc.close()


def test_pdf_with_open_password_raises_actionable_error() -> None:
    """带**打开口令**的 PDF 必须在打开时就给出「需要密码」的可行动提示。

    这类文件 ``fitz.open`` 不报错、``page_count`` 也正常，要到取页面尺寸才炸，
    pymupdf 给的是 ``document closed or encrypted`` —— 用户只会以为文件坏了。
    """
    import tempfile

    from wm import media

    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "secret.pdf")
        _make_pdf(path, password="123456")
        try:
            media.Document(path)
        except media.PasswordRequiredError as exc:
            message = str(exc)
            assert "口令" in message, f"提示要点明是口令问题，实际：{message}"
            assert "密码" in message, f"提示要让用户在别处输密码，实际：{message}"
        else:
            raise AssertionError("带口令的 PDF 应当被拒绝，不能静默打开")


def test_pdf_password_error_is_a_valueerror() -> None:
    """``PasswordRequiredError`` 必须是 ``ValueError`` 的子类。

    调用方现在都用 ``except Exception`` 兜住，但继承 ``ValueError`` 能让将来
    「按 ValueError 收窄」的写法不漏掉它。
    """
    from wm import media

    assert issubclass(media.PasswordRequiredError, ValueError)


def test_plain_pdf_is_not_mistaken_for_password_protected() -> None:
    """普通 PDF 必须照常打开 —— 不能有「凡 PDF 都要密码」的误杀。"""
    import tempfile

    from wm import media

    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "plain.pdf")
        _make_pdf(path)
        doc = media.Document(path)
        try:
            assert doc.page_count == 1, doc.page_count
            width, height = doc.page_size(0)
            assert width > 0 and height > 0, (width, height)
        finally:
            doc.close()


# ---------------------------------------------------------------------------
# 序列化版本号（审计 P2：from_dict 加版本号）
# ---------------------------------------------------------------------------


def test_to_dict_writes_current_schema_version() -> None:
    """``to_dict`` 必须带上当前格式版本，且不把「来源版本」原样写回去。"""
    from wm.spec import SCHEMA_VERSION, WatermarkSpec

    data = WatermarkSpec(text="机密").normalized().to_dict()
    assert data.get("version") == SCHEMA_VERSION, data.get("version")
    assert "schema_version" not in data, data  # 来源信息不是参数，不该回写


def test_from_dict_roundtrip_and_schema_version_recorded() -> None:
    """往返后参数一致，且来源版本被记录（能被判断「是否来自更新版本」）。"""
    from wm.spec import SCHEMA_VERSION, WatermarkSpec

    spec = WatermarkSpec(text="机密", font_pct=4.0).normalized()
    restored = WatermarkSpec.from_dict(spec.to_dict())
    assert restored == spec, (restored, spec)
    assert restored.schema_version == SCHEMA_VERSION, restored.schema_version


def test_from_dict_tolerates_legacy_and_future_data() -> None:
    """旧数据（无版本号）照常加载；未来版本保留已知字段、来源可被识别。

    这两条都是**向后兼容**的底线：老配置不能因为多了个 version 就加载失败，
    新配置也不能因为本地认不出字段就把整份参数丢掉。
    """
    from wm.spec import SCHEMA_VERSION, WatermarkSpec

    spec = WatermarkSpec(text="机密", font_pct=4.0).normalized()
    data = spec.to_dict()

    legacy = dict(data)
    legacy.pop("version")
    old = WatermarkSpec.from_dict(legacy)
    assert old == spec and old.schema_version is None, (old, old.schema_version)

    future = dict(data, version=SCHEMA_VERSION + 1, brand_new_field=123)
    new = WatermarkSpec.from_dict(future)
    assert new == spec, (new, spec)                    # 未知字段丢弃，已知字段保留
    assert new.schema_version == SCHEMA_VERSION + 1    # 来源版本留痕，不再静默
    # 版本号绝不能参与缓存指纹：否则同一份参数会因"来自哪个版本"而重复渲染
    assert new.render_key() == spec.render_key()
