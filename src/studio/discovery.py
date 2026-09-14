"""
工具自动发现管理模块，负责扫描并动态载入各平台子目录下的插件配置文件 (*.manifest.json)。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Sequence

from src.studio.base import ToolSpec

logger = logging.getLogger(__name__)

# 默认工具清单扫描检索的子路径
SCAN_DIRS = [
    "src/platforms",
    "src/processing",
    "src/tools",
]


def discover_tools(scan_dirs: Sequence[str] | None = None) -> tuple[list[ToolSpec], list[str]]:
    """
    自动扫描发现所有的爬虫子工具。
    在工作根目录下搜索匹配所有以 .manifest.json 结尾的文件并反序列化。

    Args:
        scan_dirs: 选填扫描目录，默认为平台平台和处理中心目录

    Returns:
        tuple[list[ToolSpec], list[str]]: 成功发现的工具规范描述符列表，以及在解析过程中遇到的异常报错信息列表。
    """
    if scan_dirs is None:
        scan_dirs = SCAN_DIRS

    # 定位项目根目录
    import sys
    if getattr(sys, 'frozen', False):
        project_root = Path(sys._MEIPASS)
    else:
        project_root = Path(__file__).resolve().parents[2]
    tools: list[ToolSpec] = []
    errors: list[str] = []
    seen_ids: set[str] = set()

    for scan_dir in scan_dirs:
        base = project_root / scan_dir
        if not base.is_dir():
            logger.warning("Scan directory not found: %s", base)
            continue

        # 深度递归匹配所有的 manifest 声明清单
        for manifest_path in sorted(base.rglob("*.manifest.json")):
            try:
                tool, err = _load_manifest(manifest_path)
                if err:
                    errors.append(err)
                if tool:
                    # 校验工具 ID 全局唯一性，防止注册冲突导致反射覆盖
                    if tool.tool_id in seen_ids:
                        err_msg = f"工具 ID 冲突: '{tool.tool_id}' ({manifest_path})"
                        logger.error(err_msg)
                        errors.append(err_msg)
                    else:
                        seen_ids.add(tool.tool_id)
                        tools.append(tool)
            except Exception as e:
                err_msg = f"加载 {manifest_path} 时发生未捕获异常: {e}"
                logger.exception(err_msg)
                errors.append(err_msg)

    return tools, errors


def _load_manifest(path: Path) -> tuple[ToolSpec | None, str | None]:
    """
    加载并解析单个 json 清单文件，校验其字段合法性。
    """
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        err = f"文件 {path.name} 包含无效的 JSON: {e}"
        logger.error(err)
        return None, err
    except Exception as e:
        err = f"无法读取文件 {path.name}: {e}"
        logger.error(err)
        return None, err

    # 声明清单必需具备的 5 个基础元数据字段
    required = {"tool_id", "name", "category", "summary", "entrypoint"}
    missing = required - set(data)
    if missing:
        err = f"文件 {path.name} 缺少必需字段: {missing}"
        logger.error(err)
        return None, err

    tags = tuple(data.get("tags", []))

    tool = ToolSpec(
        tool_id=data["tool_id"],
        name=data["name"],
        category=data["category"],
        summary=data["summary"],
        entrypoint=data["entrypoint"],
        implementation_path=data.get("implementation_path", ""),
        tags=tags,
    )
    return tool, None

