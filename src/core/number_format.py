"""
数值转换处理工具，专门用于将各种社交平台上的紧凑型计数字符串（如 "1.2M", "5.8万", "9,870" 等）展开为完整的整数。
"""

from __future__ import annotations

import re
from decimal import Decimal, ROUND_HALF_UP

# 匹配带单位的缩写数值。
# (?P<number>...) 匹配整型、带千分位逗号的数字或浮点数。
# \s* 匹配可选空格。
# (?P<unit>...) 匹配社交媒体常用的数量级缩写单位（支持中英文、繁体字）。
_NUMBER_WITH_UNIT_RE = re.compile(
    r"(?P<number>\d+(?:,\d{3})*(?:\.\d+)?|\d+(?:\.\d+)?)\s*(?P<unit>K|M|B|万|萬|亿|億|千|百)?",
    re.IGNORECASE,
)

# 各个缩写单位对应的 Decimal 乘数因子
_UNIT_MULTIPLIERS = {
    "k": Decimal("1000"),
    "m": Decimal("1000000"),
    "b": Decimal("1000000000"),
    "千": Decimal("1000"),
    "万": Decimal("10000"),
    "萬": Decimal("10000"),
    "亿": Decimal("100000000"),
    "億": Decimal("100000000"),
    "百": Decimal("100"),
}


def expand_compact_number(value, default: str = "") -> str:
    """
    将缩写形式的数字转换为完整的整数字符串。例如 "1.5K" -> "1500"，"3.2万" -> "32000"。

    Args:
        value: 待转换的值，可以是 str, int, float 等。
        default: 转换失败时返回的默认字符串。

    Returns:
        str: 展开后的纯数字整数字符串，若无法解析则返回原始字符串或默认值。
    """
    text = "" if value is None else str(value).strip()
    if not text:
        return default

    # 替换中文全角逗号为半角逗号，保证千分位分隔符能够正确被正则清洗掉
    match = _NUMBER_WITH_UNIT_RE.search(text.replace("，", ","))
    if not match:
        return text

    # 清洗掉数字中的千分位逗号以构造 Decimal 实例
    number_text = match.group("number").replace(",", "")
    unit = (match.group("unit") or "").lower()
    try:
        number = Decimal(number_text)
    except (ValueError, ArithmeticError):
        return text

    multiplier = _UNIT_MULTIPLIERS.get(unit, Decimal("1"))
    # 使用 ROUND_HALF_UP (四舍五入) 规避 Python 默认的银行家舍入法（ROUND_HALF_EVEN）带来的偏差问题
    expanded = (number * multiplier).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return str(int(expanded))

