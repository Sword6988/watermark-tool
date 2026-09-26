"""水印参数数据模型（v4）。

**统一度量：所有尺度参数一律用「页面短边百分比」** ``ref = min(page_w, page_h)``。
好处：预览把页面等比缩小后，全部参数自动等比，视觉完全一致，**不需要任何额外的
预览缩放补偿逻辑**。换算关系：

    font_px   = font_pct   / 100 * ref
    margin_px = margin_pct / 100 * ref

这里集中定义唯一的「参数真源」：取值范围、默认值、合法化 / 夹紧规则、序列化。
界面、引擎、测试全部引用本模块，不得各自写字面量。
"""

from __future__ import annotations

import dataclasses
import math
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Tuple

try:  # Pillow 用于把任意颜色写法归一化成 #rrggbb
    from PIL import ImageColor
except Exception:  # pragma: no cover - Pillow 一定存在，兜底防御
    ImageColor = None  # type: ignore[assignment]

# ---------------------------------------------------------------------------
# 常量（唯一真源）
# ---------------------------------------------------------------------------

DEFAULT_TEXT: str = "机密文件"
#: 默认字体取**微软雅黑**（与 ``wm.fonts.PREFERRED_FAMILIES`` 首位一致）：
#: 鸿蒙黑体只在少数机器上装了，拿它当默认会让大多数用户走回退，且下拉框首选项
#: 在他们那儿没有实际字形。改这里请连 ``wm/fonts.py`` 的优先列表一起改。
DEFAULT_FONT_FAMILY: str = "Microsoft YaHei"
DEFAULT_SUFFIX: str = "_水印版"
#: 多行文本的行距倍数（内部常量，不对外暴露为参数）
LINE_SPACING: float = 1.15

FONT_PCT_RANGE: Tuple[float, float] = (0.5, 30.0)
MARGIN_PCT_RANGE: Tuple[float, float] = (0.0, 20.0)
#: 角度是**循环量**：0° 与 360° 是同一个方向。区间取 ``(-180, 180]``（半开半闭，
#: 180 保持 180 不被翻成 -180），目的只有一个 —— 把「数值不连续的那一点」（循环量
#: 单值表示必然存在的一处接缝）**挪到 ±180**，也就是「文字完全倒置」这个最少用到
#: 的方向上去。旧值域 0–360 把接缝压在 12 点方向 = 0°（水平，最常用的方向），
#: 拖过那里数字会 359→0 横跳。
#: 渲染侧对负角与等价正角完全一致（``PIL.Image.rotate`` 支持负数）。
ANGLE_RANGE: Tuple[float, float] = (-180.0, 180.0)
#: 不透明度的**内部**表示：0–1 的 alpha，渲染直接吃这个值（序列化格式不变）
OPACITY_RANGE: Tuple[float, float] = (0.05, 1.0)
#: 不透明度的**界面显示**区间：百分比 5 % – 100 %（与内部 alpha 相差 100 倍）
OPACITY_PCT_RANGE: Tuple[float, float] = (5.0, 100.0)

DEFAULT_FONT_PCT: float = 4.0
DEFAULT_MARGIN_PCT: float = 3.0
DEFAULT_ANGLE: float = 30.0
DEFAULT_OPACITY: float = 0.30
DEFAULT_OPACITY_PCT: float = 30.0
DEFAULT_COLOR: str = "#d32f2f"

#: 所有数字框 / 关联滑杆的步进统一为 1（整数级调节，不再有 0.1 / 0.01 的细步长）
FONT_PCT_STEP: float = 1.0
MARGIN_PCT_STEP: float = 1.0
ANGLE_STEP: float = 1.0
OPACITY_PCT_STEP: float = 1.0

#: 常用色快捷键（名称 -> #rrggbb，统一小写以便与 normalize_color 的输出一致）
COLOR_PRESETS: Tuple[Tuple[str, str], ...] = (
    ("红", "#d32f2f"),
    ("黑", "#1a1a1f"),
    ("蓝", "#1565c0"),
    ("绿", "#2e7d32"),
    ("橙", "#e65100"),
    ("灰", "#616161"),
)

_HEX_RE = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------

def clamp(value: float, lo: float, hi: float) -> float:
    """把 ``value`` 夹到 ``[lo, hi]`` 区间。"""
    return lo if value < lo else (hi if value > hi else value)


def wrap_deg(value: Any, default: float = DEFAULT_ANGLE) -> float:
    """把任意角度归一到 ``(-180, 180]`` —— **循环量的正确合法化方式**。

    角度不能用 ``clamp``：350° 被夹成 180° 是**把方向改掉了**（前者几乎水平，
    后者完全倒置）。取模才能守住建好的契约「同一个方向 = 同一个数」：

        360 -> 0      350 -> -10     -190 -> 170     180 -> 180

    ``180`` 保持 ``180`` 而不翻成 ``-180``（半开半闭区间），这样 ±180 只有一个端点值。
    """
    ang = math.fmod(to_float(value, default), 360.0)
    if ang > 180.0:
        ang -= 360.0
    elif ang <= -180.0:
        ang += 360.0
    return 0.0 if ang == 0.0 else ang   # 消掉 -0.0（否则界面会显示「-0」）


#: 后缀里**绝不允许**出现的字符：路径分隔符会把输出写到别的目录去（路径穿越），
#: Windows 文件名非法字符会让保存直接失败，通配符与控制字符同理。
_UNSAFE_SUFFIX_CHARS = '\\/:*?"<>|\r\n\t'


def safe_suffix(value: Any, default: str = DEFAULT_SUFFIX) -> str:
    """把用户填的「输出后缀」净化成可直接拼进文件名的中缀。

    输出路径是 ``{原文件名}{后缀}{扩展名}``，后缀一旦含 ``/`` 或 ``\\`` 就会把文件
    写到别的目录（``../`` 更是直接逃出输出目录）—— 这在「绝不覆盖原文件」之外
    另开一个安全口子，必须在**入参处**堵死，不能指望调用方记得自己清理。

    净化规则：去掉所有危险字符与首尾空白；净化后为空则回退默认后缀。长度不限，
    由 UI 输入框的宽度天然约束（超长文件名失败时也有中文报错兜底）。
    """
    raw = value if isinstance(value, str) else ""
    cleaned = "".join(ch for ch in raw.strip() if ch not in _UNSAFE_SUFFIX_CHARS).strip()
    return cleaned or default


def to_float(value: Any, default: float = 0.0) -> float:
    """尽力把任意输入转成 float，失败返回 ``default``（不抛异常）。"""
    if value is None:
        return float(default)
    try:
        result = float(value)
    except (TypeError, ValueError):
        return float(default)
    if result != result:  # NaN
        return float(default)
    return result


def opacity_to_pct(value: Any) -> float:
    """内部 alpha（0.05–1.0）→ 界面百分比（5–100），越界即夹到最近边界。"""
    return clamp(to_float(value, DEFAULT_OPACITY) * 100.0, *OPACITY_PCT_RANGE)


def opacity_from_pct(value: Any) -> float:
    """界面百分比（5–100）→ 内部 alpha（0.05–1.0），越界即夹到最近边界。"""
    return clamp(to_float(value, DEFAULT_OPACITY_PCT) / 100.0, *OPACITY_RANGE)


def normalize_color(value: Any) -> str:
    """把任意颜色写法归一化成 ``#rrggbb``；认不出则回退默认色。"""
    if value is None:
        return DEFAULT_COLOR
    raw = str(value).strip()
    if not raw:
        return DEFAULT_COLOR
    if ImageColor is not None:
        try:
            r, g, b = ImageColor.getrgb(raw)[:3]
            return "#%02x%02x%02x" % (r, g, b)
        except Exception:
            pass
    if _HEX_RE.match(raw):
        if len(raw) == 4:  # #rgb -> #rrggbb
            return "#" + "".join(ch * 2 for ch in raw[1:])
        return raw.lower()
    return DEFAULT_COLOR


# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------

@dataclass
class WatermarkSpec:
    """一份完整的水印参数快照。所有字段都有安全默认值。

    水印**恒以平铺方式铺满整页**：没有「平铺」开关，也没有位置偏移字段，
    位置与排布完全由 ``layout`` 决定，用户不可调节。
    """

    text: str = DEFAULT_TEXT
    font_family: str = DEFAULT_FONT_FAMILY
    font_pct: float = DEFAULT_FONT_PCT
    margin_pct: float = DEFAULT_MARGIN_PCT
    angle: float = DEFAULT_ANGLE
    opacity: float = DEFAULT_OPACITY
    color: str = DEFAULT_COLOR

    # -- 派生 -------------------------------------------------------------

    def lines(self) -> List[str]:
        """按换行拆成多行文本；空文本回退为默认文本。"""
        txt = self.text if isinstance(self.text, str) else ""
        if not txt.strip():
            txt = DEFAULT_TEXT
        return txt.split("\n")

    def copy(self, **changes: Any) -> "WatermarkSpec":
        """返回替换若干字段后的新对象（不修改自身）。"""
        return dataclasses.replace(self, **changes)

    def normalized(self) -> "WatermarkSpec":
        """返回合法化后的副本：类型安全 + 区间夹紧，任何非法值都不抛异常。"""
        txt = self.text if isinstance(self.text, str) else ""
        if not txt.strip():
            txt = DEFAULT_TEXT
        family = self.font_family if isinstance(self.font_family, str) else ""
        family = family.strip() or DEFAULT_FONT_FAMILY
        return self.copy(
            text=txt,
            font_family=family,
            font_pct=clamp(to_float(self.font_pct, DEFAULT_FONT_PCT), *FONT_PCT_RANGE),
            margin_pct=clamp(to_float(self.margin_pct, DEFAULT_MARGIN_PCT), *MARGIN_PCT_RANGE),
            angle=wrap_deg(self.angle),
            opacity=clamp(to_float(self.opacity, DEFAULT_OPACITY), *OPACITY_RANGE),
            color=normalize_color(self.color),
        )

    # -- 缓存指纹 ---------------------------------------------------------

    def block_key(self) -> Tuple[Any, ...]:
        """单个水印**块位图**的参数指纹（缓存 key 的一部分）。

        只列影响块位图的字段：``font_pct`` / ``margin_pct`` 决定的是「块画多大、
        排多密」，块本身的像素**不由它们决定**，故不进 key —— 否则改一次边距就要
        重渲染所有块，缓存命中率白白归零。

        调用方只需再拼上「字号 px」（同一份参数在不同页面尺寸下字号不同）。
        """
        return (
            tuple(self.lines()),
            self.font_family,
            round(float(self.angle), 4),
            round(float(self.opacity), 6),
            self.color,
        )

    def render_key(self) -> Tuple[Any, ...]:
        """整页渲染**结果**的参数指纹（同 dims + 倍率一起构成完整缓存 key）。

        ⚠️ 这是「哪些参数影响像素」的**唯一枚举处**：任何渲染缓存的 key 都必须
        从这里派生，不得在各模块里手写字段列表 —— 漏一个字段表现为「改了参数却
        复用旧图层」（静默的错误输出），且极难排查。
        """
        return (
            self.text,
            self.font_family,
            round(float(self.font_pct), 4),
            round(float(self.margin_pct), 4),
            round(float(self.angle), 4),
            round(float(self.opacity), 6),
            self.color,
        )

    # -- 序列化 -----------------------------------------------------------

    def to_dict(self) -> Dict[str, Any]:
        """导出为可 JSON 化的字典。"""
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "WatermarkSpec":
        """从字典构造并合法化；字段缺失 / 非法不抛异常。"""
        base = cls()
        if not isinstance(data, dict):
            return base
        fields = {f.name for f in dataclasses.fields(cls)}
        kwargs = {k: v for k, v in data.items() if k in fields}
        try:
            return cls(**kwargs).normalized()
        except Exception:
            return base

    @classmethod
    def default(cls) -> "WatermarkSpec":
        """默认参数（已合法化）。"""
        return cls().normalized()


__all__ = [
    "WatermarkSpec",
    "DEFAULT_TEXT",
    "DEFAULT_FONT_FAMILY",
    "DEFAULT_SUFFIX",
    "LINE_SPACING",
    "FONT_PCT_RANGE",
    "MARGIN_PCT_RANGE",
    "ANGLE_RANGE",
    "OPACITY_RANGE",
    "OPACITY_PCT_RANGE",
    "DEFAULT_OPACITY",
    "DEFAULT_OPACITY_PCT",
    "DEFAULT_COLOR",
    "COLOR_PRESETS",
    "FONT_PCT_STEP",
    "MARGIN_PCT_STEP",
    "ANGLE_STEP",
    "OPACITY_PCT_STEP",
    "opacity_to_pct",
    "opacity_from_pct",
    "safe_suffix",
    "clamp",
    "wrap_deg",
    "to_float",
    "normalize_color",
]
