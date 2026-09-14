"""
应用快捷入口模块，包装重导出 GUI 主应用程序及主函数入口。
"""

from __future__ import annotations

from src.studio.qt_app import ThreePlatformCrawlerQtApp, main


__all__ = ["ThreePlatformCrawlerQtApp", "main"]
