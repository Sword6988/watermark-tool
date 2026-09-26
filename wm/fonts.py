"""系统字体发现与缓存。

策略：
    * 扫描 ``C:\\Windows\\Fonts`` 与用户字体目录 ``%LOCALAPPDATA%\\Microsoft\\Windows\\Fonts``。
    * 用 ``PIL.ImageFont.truetype(path).getname()`` 读字体家族名，建立
      ``家族名 -> (文件路径, ttc 索引)`` 的映射。
    * 优先 微软雅黑，回退 鸿蒙黑体 / 宋体 / 黑体。

所有函数带缓存。**实测**首次扫描（195 个字体文件 / 97 个家族）耗时 **0.19s**：
瓶颈是逐个读 TTC 头的 I/O，不是 CPU，所以没有必要为它做线程外异步 —— 界面上
几乎感知不到。之后的查询是 O(1)。
"""

from __future__ import annotations

import os
import threading
from typing import Dict, List, Optional, Tuple

from PIL import ImageFont

#: 界面与渲染的字体优先列表。
#:
#: **微软雅黑排在最前**：它是 Windows 自带字体，绝大多数机器都有；鸿蒙黑体
#: （``HarmonyOS Sans SC``）只在少数机器上装了 —— 让它占位首位，只会让绝大多数
#: 用户落到后位回退，而且下拉框的第一项（最显眼的那一项）在他们那儿根本选不出
#: 实际字形，等于白占一个位置。故按「装机量」排序，鸿蒙退到微软系之后。
#:
#: 另一个实测细节：``msyh.ttc`` 的 index 0 是 ``Microsoft YaHei``、index 1 才是
#: ``Microsoft YaHei UI``，而本模块每个文件只读 index 0，所以 UI 变体实际上扫不到
#: —— 它保留在列表第二位只是表达「有就用」的次序，不影响默认结果。
PREFERRED_FAMILIES: Tuple[str, ...] = (
    "Microsoft YaHei",
    "Microsoft YaHei UI",
    "HarmonyOS Sans SC",
    "SimSun",
    "SimHei",
    "Noto Sans SC",
    "Source Han Sans CN",
    "Segoe UI",
)

#: 英文字体家族名 -> 界面中文显示名。
#:
#: **只做展示层翻译**：渲染、序列化、``WatermarkSpec.font_family`` 一律仍用英文
#: 原名（PIL 只认英文名），这里的映射仅供下拉框显示。
#: 没有收录的家族（多数西文字体没有通用中文名）原样显示英文。
FAMILY_LABELS: Dict[str, str] = {
    # -- HarmonyOS / 鸿蒙 -------------------------------------------------
    "HarmonyOS Sans SC": "鸿蒙黑体",
    "HarmonyOS Sans TC": "鸿蒙黑体（繁）",
    "HarmonyOS Serif SC": "鸿蒙宋体",
    # -- 微软中文 ---------------------------------------------------------
    "Microsoft YaHei": "微软雅黑",
    "Microsoft YaHei UI": "微软雅黑 UI",
    "Microsoft YaHei Light": "微软雅黑 Light",
    "Microsoft JhengHei": "微软正黑体",
    "Microsoft JhengHei UI": "微软正黑体 UI",
    "DengXian": "等线",
    "DengXian Light": "等线 Light",
    "DengXian Bold": "等线 Bold",
    "SimSun": "宋体",
    "SimSun-ExtB": "宋体-ExtB",
    "NSimSun": "新宋体",
    "SimHei": "黑体",
    "KaiTi": "楷体",
    "KaiTiG": "楷体 GB",
    "FangSong": "仿宋",
    "FangSong_GB2312": "仿宋 GB2312",
    "LiSu": "隶书",
    "YouYuan": "幼圆",
    "MingLiU": "细明体",
    "PMingLiU": "新细明体",
    # -- 华文（ST）系列 ----------------------------------------------------
    "STSong": "华文宋体",
    "STZhongsong": "华文中宋",
    "STHeiti": "华文黑体",
    "STXihei": "华文细黑",
    "STKaiti": "华文楷体",
    "STXingkai": "华文行楷",
    "STFangsong": "华文仿宋",
    "STLiti": "华文隶书",
    "STHupo": "华文琥珀",
    "STCaiyun": "华文彩云",
    "STXinwei": "华文新魏",
    # -- 思源 / Noto 系列 --------------------------------------------------
    "Noto Sans SC": "思源黑体",
    "Noto Serif SC": "思源宋体",
    "Noto Sans CJK SC": "思源黑体 CJK",
    "Noto Serif CJK SC": "思源宋体 CJK",
    "Noto Sans TC": "思源黑体（繁）",
    "Noto Serif TC": "思源宋体（繁）",
    "Source Han Sans CN": "思源黑体 CN",
    "Source Han Serif CN": "思源宋体 CN",
    "Source Han Sans SC": "思源黑体 SC",
    "Source Han Serif SC": "思源宋体 SC",
    # -- 方正 -------------------------------------------------------------
    "FZXiaoBiaoSong-B05S": "方正小标宋",
    "FZHei-B01S": "方正黑体",
    "FZKai-Z03S": "方正楷体",
    "FZSong-Z13S": "方正书宋",
    "FZFangSong-Z02S": "方正仿宋",
    "FZShuTi": "方正舒体",
    "FZYaoTi": "方正姚体",
    # -- 日文 / 韩文（能排汉字，按中文组对待；标注语种避免误当简体中文） --------
    "MS Gothic": "MS 哥特体（日文）",
    "MS PGothic": "MS P哥特体（日文）",
    "MS Mincho": "MS 明朝（日文）",
    "MS PMincho": "MS P明朝（日文）",
    "Yu Gothic": "Yu 哥特体（日文）",
    "Meiryo": "Meiryo（日文）",
    "Malgun Gothic": "Malgun 哥特体（韩文）",
    # -- 其他常见中文字体 --------------------------------------------------
    "WenQuanYi Zen Hei": "文泉驿正黑",
    "WenQuanYi Micro Hei": "文泉驿微米黑",
    "PingFang SC": "苹方",
    "PingFang TC": "苹方（繁）",
    "Heiti SC": "黑体-简",
    "Songti SC": "宋体-简",
    "Hiragino Sans GB": "冬青黑体",
    "Alibaba PuHuiTi": "阿里巴巴普惠体",
    "Alibaba PuHuiTi 3.0": "阿里巴巴普惠体 3.0",
}

_FONT_DIRS: Tuple[str, ...] = (
    r"C:\Windows\Fonts",
    os.path.join(os.environ.get("LOCALAPPDATA", ""), "Microsoft", "Windows", "Fonts"),
    os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts"),
)
_FONT_EXTS: Tuple[str, ...] = (".ttf", ".ttc", ".otf")

#: 家族名 -> (路径, 索引)
_family_map: Optional[Dict[str, Tuple[str, int]]] = None
_scan_lock = threading.Lock()

#: 家族名 -> 是否真有汉字字形（``can_render_cjk`` 的缓存）
_cjk_map: Dict[str, bool] = {}

#: 「能不能排中文」的探针字，以及用来逼出 ``.notdef`` 的对照字。
#: U+FFFF 是**永久非字符**，任何字体都没有它的字形，必然落到 .notdef。
_CJK_SAMPLE = "中"
_NODEF_SAMPLE = "\uFFFF"

#: PIL 字体对象缓存 (family, px) -> FreeTypeFont
_font_cache: Dict[Tuple[str, int], ImageFont.FreeTypeFont] = {}
_FONT_CACHE_CAP = 128


def _scan_fonts() -> Dict[str, Tuple[str, int]]:
    """扫描字体目录并建立家族名映射（内部使用，带一次性缓存）。"""
    global _family_map
    if _family_map is not None:
        return _family_map
    with _scan_lock:
        if _family_map is not None:
            return _family_map
        mapping: Dict[str, Tuple[str, int]] = {}
        styles: Dict[str, str] = {}
        for directory in _FONT_DIRS:
            if not directory or not os.path.isdir(directory):
                continue
            try:
                names = sorted(os.listdir(directory))
            except OSError:
                continue
            for name in names:
                if not name.lower().endswith(_FONT_EXTS):
                    continue
                path = os.path.join(directory, name)
                try:
                    probe = ImageFont.truetype(path, 16)
                    family, style = probe.getname()
                except Exception:
                    continue
                if not family:
                    continue
                prev = styles.get(family)
                # 优先保留 Regular 字重作为该家族的代表字面
                if prev is None or (prev.lower() != "regular" and str(style).lower() == "regular"):
                    mapping[family] = (path, 0)
                    styles[family] = str(style)
        _family_map = mapping
        return mapping


def _has_cjk(text: str) -> bool:
    """显示名里是否含 CJK 字符（判名字，不判能力）。"""
    return any(
        (0x2E80 <= ord(ch) <= 0x9FFF)     # CJK 部首补充 … 汉字
        or (0xF900 <= ord(ch) <= 0xFAFF)  # CJK 兼容汉字
        or (0xFF00 <= ord(ch) <= 0xFFEF)  # 全角 / 半角形式
        for ch in text
    )


def can_render_cjk(family: Optional[str]) -> bool:
    """该字体**是否真能排出汉字**（不只是名字像中文）。

    判据是**字形覆盖**而不是名字：把「中」的位图与 ``.notdef`` 的位图（用 U+FFFF
    逼出来）对比 —— 不同说明真有汉字字形；相同说明这个字根本排不出来，是西文字体。

    为什么不看名字：``FAMILY_LABELS`` 不可能收全，像 ``FZShuTi``（方正舒体）这种
    没收录的中文字体会被误分到西文组；反过来名字带中文却排不出字的更罕见。
    名字里确有 CJK 字符时直接判真，省掉一次字体加载。

    全量探测 97 个字体约 0.06 秒，结果按家族名缓存。
    """
    if not family:
        return False
    cached = _cjk_map.get(family)
    if cached is not None:
        return cached
    result = False
    try:
        if _has_cjk(display_name(family)):
            result = True
        else:
            info = resolve(family)
            if info is not None:
                path, index = info
                # 不走 pil_font：那会占用渲染用的字体对象缓存（容量 128）导致频繁清仓
                probe = ImageFont.truetype(path, 32, index=index)
                result = (bytes(probe.getmask(_CJK_SAMPLE))
                          != bytes(probe.getmask(_NODEF_SAMPLE)))
    except Exception:
        result = False
    _cjk_map[family] = result
    return result


def families() -> List[str]:
    """返回可用字体家族名列表：**能排中文的在前，西文在后**。

    分组依据是 ``can_render_cjk``（真有汉字字形），与"用户在下拉框里想先看到中文
    字体"的诉求一致；组内仍沿用既有次序：优先字体靠前，其余按字母序。
    """
    mapping = _scan_fonts()
    rank = {fam: i for i, fam in enumerate(PREFERRED_FAMILIES)}
    others = len(rank)  # 非优先字体的排名（一律排在优先字体之后）

    def sort_key(family: str) -> Tuple[int, int, str]:
        """排序键：**能渲染中文的家族整体排在前面**，其次按优先列表次序。

        为什么要分组而不是纯按次序排：下拉框里中文字体与纯西文字体混排时，用户
        往下翻半天才找得到能显示「机密文件」的字体；把不能渲染汉字的放到后半段，
        视觉上自然形成「能用的在前面」。
        """
        return (0 if can_render_cjk(family) else 1,  # 中文组 / 西文组
                rank.get(family, others),            # 组内：优先字体靠前
                family.lower())                      # 组内：其余按字母序

    return sorted(mapping.keys(), key=sort_key)


def display_name(family: Optional[str]) -> str:
    """家族名的界面显示名；没收录的家族原样返回英文原名。"""
    if not family:
        return ""
    return FAMILY_LABELS.get(family, family)


def display_names(family_list: List[str]) -> List[str]:
    """把家族名列表翻成显示名列表，与入参**逐项对齐**。

    **重名消歧**：多个家族映射到同一个中文名时（如「思源黑体」可能同时来自
    ``Noto Sans SC`` 与 ``Source Han Sans CN``），给这些项补上 ``（英文原名）``
    后缀 —— ttk 下拉框是只读单行文本，做不了真正的悬浮提示，用次要文本兜底。
    """
    labels = [display_name(f) for f in family_list]
    counts: Dict[str, int] = {}
    for label in labels:
        counts[label] = counts.get(label, 0) + 1
    return [f"{label}（{family}）" if counts[label] > 1 else label
            for family, label in zip(family_list, labels)]


def default_family() -> str:
    """返回本机可用的默认字体家族（微软雅黑优先，鸿蒙只在没装雅黑时才轮到）。

    与 ``wm.spec.DEFAULT_FONT_FAMILY`` 取同一个值（``PREFERRED_FAMILIES[0]``），
    两边口径必须一致 —— 否则「界面默认」和「渲染默认」会落到不同字体上。
    """
    mapping = _scan_fonts()
    for fam in PREFERRED_FAMILIES:
        if fam in mapping:
            return fam
    return PREFERRED_FAMILIES[0]


def resolve(family: Optional[str]) -> Optional[Tuple[str, int]]:
    """把家族名解析为 ``(字体文件路径, ttc 索引)``；找不到返回 ``None``。

    找不到时会沿优先列表回退，尽量给出一个能渲染中文的字面。
    """
    mapping = _scan_fonts()
    if family:
        hit = mapping.get(family)
        if hit is not None:
            return hit
    for fam in PREFERRED_FAMILIES:
        hit = mapping.get(fam)
        if hit is not None:
            return hit
    if mapping:
        return next(iter(mapping.values()))
    return None


def has_family(family: Optional[str]) -> bool:
    """判断某家族名是否可用。"""
    return bool(family) and family in _scan_fonts()


def font_path(family: Optional[str]) -> Optional[str]:
    """返回字体文件路径（找不到返回 ``None``）。"""
    hit = resolve(family)
    return hit[0] if hit else None


def pil_font(family: Optional[str], size: float) -> ImageFont.FreeTypeFont:
    """取得指定家族的 PIL 字体对象（按家族 + 像素尺寸缓存）。"""
    px = max(1, int(round(float(size))))
    key = (family or "", px)
    cached = _font_cache.get(key)
    if cached is not None:
        return cached

    info = resolve(family)
    font: Optional[ImageFont.FreeTypeFont] = None
    if info is not None:
        path, index = info
        try:
            font = ImageFont.truetype(path, px, index=index)
        except Exception:
            font = None
    if font is None:
        try:
            font = ImageFont.load_default(size=px)
        except Exception:
            font = ImageFont.load_default()

    if len(_font_cache) >= _FONT_CACHE_CAP:
        _font_cache.clear()
    _font_cache[key] = font
    return font


def clear_cache() -> None:
    """清空家族映射、字体对象缓存与中文覆盖判定缓存（测试用）。"""
    global _family_map
    with _scan_lock:
        _family_map = None
    _font_cache.clear()
    _cjk_map.clear()


__all__ = [
    "PREFERRED_FAMILIES",
    "FAMILY_LABELS",
    "families",
    "can_render_cjk",
    "display_name",
    "display_names",
    "default_family",
    "resolve",
    "has_family",
    "font_path",
    "pil_font",
    "clear_cache",
]
