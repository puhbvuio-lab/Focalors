"""
CSV 数据清洗工具，用于去除或替换单元格中的异常换行符，防止破坏 CSV 文件结构。
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

# 匹配常规换行符 (\r, \n) 以及 Unicode 换行/段落分隔符 (\u2028, \u2029)。
# 这些 Unicode 字符在写入 CSV 时会被部分软件解析为物理换行，从而导致数据整行错位。
_LINE_BREAK_RE = re.compile(r"[\r\n\u2028\u2029]+")


class MultilineText(str):
    """
    标记「需要在 Excel 单元格内保留换行」的文本。

    CSV 场景下换行会破坏行结构，因此默认清洗会压平换行；但 Excel 单元格
    支持单元格内换行（配合自动换行显示），图片/视频链接列表这类内容需要
    逐行展示，所以用该类型显式声明，交回调用方决定展示形式。
    """

    __slots__ = ()


def sanitize_csv_cell(value: Any) -> Any:
    """
    清洗单个单元格数据。如果是字符串，则将其中的所有换行符替换为单个空格，并去除前后空白。

    Args:
        value: 单元格值

    Returns:
        Any: 清洗后的值
    """
    if value is None:
        return ""
    if not isinstance(value, str):
        return value
    if isinstance(value, MultilineText):
        # 统一为 \n 并去掉首尾空白，但保留单元格内换行
        return _LINE_BREAK_RE.sub("\n", str(value)).strip()
    return _LINE_BREAK_RE.sub(" ", value).strip()


def sanitize_csv_row(row: Mapping[str, Any]) -> dict[str, Any]:
    """
    清洗一行字典格式的 CSV 数据。
    """
    return {key: sanitize_csv_cell(value) for key, value in row.items()}


def sanitize_csv_rows(rows: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """
    批量清洗多行字典格式的 CSV 数据。
    """
    return [sanitize_csv_row(row) for row in rows]

