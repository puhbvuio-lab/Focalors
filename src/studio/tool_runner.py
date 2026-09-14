"""
子工具进程执行引导程序。
供主窗口 QProcess 子进程调用启动，实现爬虫沙箱式隔离独立运行，支持直接入参反射载入子界面。
"""

from __future__ import annotations

import argparse
import inspect
import json
import logging
import sys
import traceback
from pathlib import Path
from typing import Any

from src.studio.base import load_object
from src.studio.discovery import discover_tools
from src.studio.registry import TOOLS

logger = logging.getLogger(__name__)


def _setup_console_logging() -> None:
    """
    仅为工具运行器配置最小日志输出，避免导入检查阶段被 GUI 依赖链阻塞。
    """
    try:
        from src.core.app_logging import setup_console_logging as real_setup_console_logging
    except Exception:
        if not logging.getLogger().handlers:
            logging.basicConfig(
                level=logging.INFO,
                format="[%(asctime)s] [%(levelname)s] %(name)s: %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
    else:
        real_setup_console_logging()


def find_tool(tool_id: str):
    """
    根据 ID 检索对应的工具元数据。
    第一阶段搜寻硬编码静态注册表，如果未命中则在第二阶段执行目录扫描搜寻动态清单配置文件。
    """
    for tool in TOOLS:
        if tool.tool_id == tool_id:
            return tool
    discovered, _ = discover_tools()
    for tool in discovered:
        if tool.tool_id == tool_id:
            return tool
    raise ValueError(f"Unknown tool_id: {tool_id}")


def check_tool(tool_id: str) -> dict[str, str | bool]:
    """
    诊断工具完整性，验证对应的实现脚本路径是否有效存在。
    """
    tool = find_tool(tool_id)
    script_path = Path(__file__).resolve().parents[1] / tool.implementation_path
    return {
        "tool_id": tool.tool_id,
        "name": tool.name,
        "entrypoint": tool.entrypoint,
        "implementation_path": tool.implementation_path,
        "script_exists": script_path.exists(),
    }


def _dedupe_preserve_order(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        normalized = value.strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        result.append(normalized)
    return result


def _missing_dependency_names(exc: BaseException) -> list[str]:
    names: list[str] = []
    module_name = getattr(exc, "name", None)
    if module_name:
        names.append(str(module_name).split(".", 1)[0])

    message = str(exc)
    marker = "No module named "
    if marker in message:
        parts = message.split("'")
        if len(parts) >= 2:
            names.append(parts[1].split(".", 1)[0])

    return _dedupe_preserve_order(names)


def check_tool_import(tool_id: str) -> dict[str, Any]:
    """
    校验工具入口能否被安全导入；若失败，返回结构化的依赖缺失或导入错误信息。
    """
    info: dict[str, Any] = dict(check_tool(tool_id))
    tool = find_tool(tool_id)
    info["available"] = False
    info["missing_dependencies"] = []
    info["error_type"] = None
    info["error_message"] = None

    try:
        load_object(tool.entrypoint)
    except ModuleNotFoundError as exc:
        info["error_type"] = "dependency_missing"
        info["missing_dependencies"] = _missing_dependency_names(exc)
        info["error_message"] = str(exc)
    except ImportError as exc:
        missing_dependencies = _missing_dependency_names(exc)
        if missing_dependencies:
            info["error_type"] = "dependency_missing"
            info["missing_dependencies"] = missing_dependencies
        else:
            info["error_type"] = "import_error"
        info["error_message"] = str(exc)
    except Exception as exc:
        info["error_type"] = type(exc).__name__
        info["error_message"] = str(exc)
    else:
        info["available"] = True

    return info


def run_tool(tool_id: str) -> int:
    """
    沙箱式启动指定的子工具界面窗口。

    Args:
        tool_id: 工具标识

    Returns:
        int: 进程退出状态码（0 代表完美退出，其他代表异常）
    """
    from PyQt5.QtWidgets import QApplication, QMessageBox, QWidget

    _setup_console_logging()
    tool = find_tool(tool_id)
    logger.info("Starting tool: %s (%s)", tool.name, tool.tool_id)
    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName(tool.name)
    try:
        # 反射载入清单定义中指定的入口界面类或方法
        target = load_object(tool.entrypoint)
        if inspect.isclass(target):
            window = target()
        elif callable(target):
            window = target()
        else:
            raise TypeError(f"Entrypoint is not callable: {tool.entrypoint}")
        if not isinstance(window, QWidget):
            raise TypeError(f"Entrypoint must return a QWidget: {tool.entrypoint}")
        window.setWindowTitle(tool.name)
        window.show()
        exit_code = app.exec_()
        logger.info("Tool closed: %s (%s) exit_code=%s", tool.name, tool.tool_id, exit_code)
        return exit_code
    except Exception as exc:
        logger.error("Tool startup failed: %s (%s)\n%s", tool.name, tool.tool_id, traceback.format_exc())
        try:
            # 异常兜底：启动崩溃时，尝试弹出 QMessageBox 警告窗方便非命令行用户察觉，如 GUI 未能就绪则退避输出到 stderr
            QMessageBox.critical(None, "启动失败", f"{tool.name}\n\n{exc}")
        except Exception:
            print(f"启动失败：{tool.name}\n{exc}", file=sys.stderr)
        return 3


def main(argv=None) -> int:
    """主程序命令行解析。"""
    parser = argparse.ArgumentParser(description="Run a registered desktop tool by id.")
    parser.add_argument("--tool-id", required=True, help="Tool id from src.studio.registry")
    parser.add_argument("--check", action="store_true", help="Only validate and print tool metadata")
    parser.add_argument("--check-import", action="store_true", help="Validate whether the tool entrypoint is importable")
    args = parser.parse_args(argv)

    if args.check_import:
        result = check_tool_import(args.tool_id)
        print(json.dumps(result, ensure_ascii=True))
        if result["available"]:
            return 0
        return 2 if result["error_type"] == "dependency_missing" else 3

    if args.check:
        print(json.dumps(check_tool(args.tool_id), ensure_ascii=True))
        return 0

    return run_tool(args.tool_id)


if __name__ == "__main__":
    raise SystemExit(main())
