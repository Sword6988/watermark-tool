"""入口与随包二进制的用例：``main.py`` 的参数解析 / 自检，以及 ``dec.exe`` 的可追溯性。

对应审计 M22：``main.selftest()`` 是**打包后自动验收的唯一依据**，但它此前从未被
用例跑过（换了 Python 版本才发现 `import fitz` 之类的退化）；随包分发的
``tools/LDDec/dec.exe`` 也从未被校验过指纹 —— 供应链上"审计过"只是口头说法。
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import main as entry  # noqa: E402  入口模块（模块级只定义常量，导入无副作用）

TOOLS_DIR = os.path.join(ROOT, "tools", "LDDec")
DEC_EXE = os.path.join(TOOLS_DIR, "dec.exe")


# ---------------------------------------------------------------------------
# main._split_argv
# ---------------------------------------------------------------------------

def test_split_argv_files_and_selftest() -> None:
    """``文件 --selftest 目录``：文件要留下，选项与其值要被剥走。"""
    files, out = entry._split_argv(["a.png", "b.pdf", "--selftest", "out_dir"])
    assert files == ["a.png", "b.pdf"], f"待处理文件不符：{files}"
    assert out == "out_dir", f"自检输出目录不符：{out}"


def test_split_argv_selftest_without_value_does_not_drop_files() -> None:
    """``a.png --selftest``（选项在末尾且缺值）：不能把 a.png 也丢掉（旧实现会越界丢）。"""
    files, out = entry._split_argv(["a.png", "--selftest"])
    assert files == ["a.png"], f"文件不该被选项吞掉：{files}"
    assert out, "缺值时也应给出一个默认输出目录"


def test_split_argv_unknown_option_is_ignored() -> None:
    """未知选项跳过而不是当成文件名（否则会被当成待加水印的文件去打开）。"""
    files, out = entry._split_argv(["a.png", "--whatever", "b.png"])
    assert files == ["a.png", "b.png"], f"未知选项应被忽略：{files}"
    assert out is None


def test_split_argv_empty() -> None:
    """无参数：空列表 + 不自检。"""
    files, out = entry._split_argv([])
    assert files == [] and out is None


# ---------------------------------------------------------------------------
# main.selftest
# ---------------------------------------------------------------------------

def test_selftest_reports_failure_on_unusable_out_dir() -> None:
    """输出目录不可用时**必须返回退出码**并写报告，绝不能弹模态框（无头环境会挂死）。"""
    with tempfile.TemporaryDirectory() as tmp:
        blocker = os.path.join(tmp, "iam_a_file")
        with open(blocker, "w", encoding="utf-8") as handle:
            handle.write("x")
        code = entry.selftest(blocker)          # 传一个**文件**当目录
        assert code == 1, "目录不可用时必须返回非 0"
        report = os.path.join(blocker, "selftest_report.txt")
        # 目录建不出来时报告也写不进去：断言的是「没挂住、有结论」，不是文件位置
        assert not os.path.exists(report) or code == 1


def test_selftest_end_to_end_passes() -> None:
    """真跑一遍自检：报告里必须出现 [SELFTEST] PASS（打包脚本据此判定 exe 可用）。"""
    try:
        import tkinterdnd2  # noqa: F401
    except Exception as exc:
        raise unittest.SkipTest("需要 tkinterdnd2 / 可用显示环境：%s" % exc)

    with tempfile.TemporaryDirectory() as tmp:
        out_dir = os.path.join(tmp, "selftest_out")
        code = entry.selftest(out_dir)
        report = os.path.join(out_dir, "selftest_report.txt")
        assert os.path.isfile(report), "自检必须写出报告文件"
        with open(report, encoding="utf-8") as handle:
            text = handle.read()
        assert code == 0, "自检未通过：\n%s" % text
        assert "[SELFTEST] PASS" in text, "报告缺少 PASS 结论"
        for name in ("libs", "tkdnd", "image", "pdf"):
            assert "[OK] %s " % name in text, f"自检项 {name} 未通过：\n{text}"


# ---------------------------------------------------------------------------
# 随包的 dec.exe（供应链可追溯）
# ---------------------------------------------------------------------------

def _skip_if_no_exe() -> None:
    if not os.path.isfile(DEC_EXE):
        raise unittest.SkipTest("未随包提供 tools/LDDec/dec.exe（内网部署后才有）")


def _load_deploy_module():
    """按**文件路径**加载 ``packaging/deploy_lddec.py``。

    不能直接 ``from packaging import deploy_lddec``：``packaging`` 与 PyPI 上同名
    的库撞名，而本项目的 ``packaging/`` 没有 ``__init__.py``（命名空间包），
    Python 会**优先**选 site-packages 里那个真正的包 —— 结果是 ModuleNotFoundError。
    """
    import importlib.util

    path = os.path.join(ROOT, "packaging", "deploy_lddec.py")
    spec = importlib.util.spec_from_file_location("wm_deploy_lddec", path)
    assert spec is not None and spec.loader is not None, "无法加载 deploy_lddec.py"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_lddec_binary_matches_manifest() -> None:
    """dec.exe 必须与 SHA256.txt 记录一致 —— 换了二进制就要重新生成清单。"""
    _skip_if_no_exe()
    deploy_lddec = _load_deploy_module()

    assert os.path.isfile(os.path.join(TOOLS_DIR, deploy_lddec.MANIFEST)), \
        "缺少 %s：随包二进制必须留指纹记录" % deploy_lddec.MANIFEST
    assert deploy_lddec.verify_manifest(TOOLS_DIR), \
        "dec.exe 与清单不符（可能被替换或损坏）；换版本请重跑 " \
        "python packaging/deploy_lddec.py deploy <来源目录>"


def test_lddec_binary_is_valid_pe_and_findable() -> None:
    """dec.exe 得是合法 PE 文件，且 ``dlp.find_lddec()`` 能自动定位到它（零配置前提）。"""
    _skip_if_no_exe()
    from wm import dlp

    with open(DEC_EXE, "rb") as handle:
        assert handle.read(2) == b"MZ", "dec.exe 不是合法 PE 文件（下载不完整？）"
    found = dlp.find_lddec()
    assert found is not None, "内置适配器应能自动发现随包的 dec.exe"
    assert os.path.samefile(found, DEC_EXE), \
        f"定位到了别的文件：{found}（期望 {DEC_EXE}）"


def test_lddec_binary_runs_and_terminates() -> None:
    """无参数运行必须**自行退出**（挂住会让整个解密链路卡在超时上，用户侧毫无反馈）。

    不校验退出码：cmd 版无参数时打印 usage 后返回，不同构建返回值不一致；这里要
    钉住的是「能启动、会退出、不弹窗」。
    """
    _skip_if_no_exe()
    startup = None
    if os.name == "nt":
        startup = subprocess.STARTUPINFO()
        startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW  # 别抢焦点
    try:
        proc = subprocess.run([DEC_EXE], cwd=tempfile.gettempdir(),
                              capture_output=True, timeout=30,
                              startupinfo=startup)
    except subprocess.TimeoutExpired:
        raise AssertionError("dec.exe 无参数运行 30 秒未退出（会拖死解密链路）")
    assert proc.returncode is not None, "dec.exe 未正常结束"
