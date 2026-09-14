"""
系统日志配置模块，提供统一的分级日志管理。
支持 WARN / ERROR 二级分类，自动持久化到文件。
文件日志使用普通 FileHandler 追加写入，rotation 由外部管理。
"""

from __future__ import annotations

import logging
import sys
import threading
from pathlib import Path


# 系统日志根目录名称，用于在多模块间归集日志
LOGGER_ROOT = "crawler_tool"
_console_lock = threading.Lock()
_file_lock = threading.Lock()
_CONFIGURED = False
_FILE_HANDLER_CONFIGURED = False


def _get_log_file_path() -> Path:
    """获取日志文件路径，自动创建 output 目录。"""
    root = Path(__file__).resolve().parents[2]
    log_dir = root / "output"
    log_dir.mkdir(parents=True, exist_ok=True)
    return log_dir / "crawler.log"


def setup_console_logging(level: int = logging.INFO) -> None:
    """
    初始化控制台日志配置。
    设置统一的日志格式，避免重复配置 handler。

    Args:
        level: 日志级别，默认为 logging.INFO
    """
    global _CONFIGURED
    with _console_lock:
        if _CONFIGURED:
            return

        # 在非交互式或重定向环境下，sys.stdout 可能为 None，
        # 此时退避使用 sys.stderr 确保日志能够正常输出
        stream = sys.stdout or sys.stderr
        handler = logging.StreamHandler(stream)
        handler.setFormatter(logging.Formatter("[%(asctime)s] [%(levelname)s] %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S"))

        root = logging.getLogger(LOGGER_ROOT)
        root.setLevel(level)
        root.addHandler(handler)
        root.propagate = False
        _CONFIGURED = True


def _setup_file_logging() -> None:
    """
    初始化文件日志配置。
    WARN 和 ERROR 级别日志自动持久化到 output/crawler.log。
    使用普通 FileHandler 追加写入，多进程追加写在 Windows 上基本安全（<4KB 原子写）。
    日志文件的 rotation 由外部工具或手动管理。
    """
    global _FILE_HANDLER_CONFIGURED
    with _file_lock:
        if _FILE_HANDLER_CONFIGURED:
            return

        log_path = _get_log_file_path()

        file_handler = logging.FileHandler(
            str(log_path),
            encoding="utf-8",
        )
        file_handler.setLevel(logging.WARNING)
        file_handler.setFormatter(logging.Formatter(
            "[%(asctime)s] [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S",
        ))

        root = logging.getLogger(LOGGER_ROOT)
        root.addHandler(file_handler)
        _FILE_HANDLER_CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    """
    获取指定名称的 Logger 实例，自动为其添加系统根日志前缀。

    Args:
        name: 模块或类的 Logger 名称

    Returns:
        logging.Logger: 包含前缀的 Logger 实例
    """
    setup_console_logging()
    # 如果传入的名称已经包含系统根日志前缀，直接使用，避免重复嵌套前缀
    if name.startswith(LOGGER_ROOT):
        return logging.getLogger(name)
    return logging.getLogger(f"{LOGGER_ROOT}.{name}")


# ---------- 分级日志输出函数 ----------

# 日志级别常量
INFO = "INFO"
WARN = "WARN"
ERROR = "ERROR"


def log_line(log_callback, message: str) -> None:
    """
    统一的日志输出函数，封装 None 防护。默认 INFO 级别。
    推荐使用 log_warn / log_error 以获得更精确的分级。

    Args:
        log_callback: 日志回调函数（由 SimpleToolWindow 传入）
        message: 日志消息文本
    """
    if log_callback:
        log_callback(message)


def log_warn(log_callback, message: str) -> None:
    """
    输出 WARN 级别日志，同时持久化到日志文件。
    GUI 显示自动添加 [WARN] 前缀。

    Args:
        log_callback: 日志回调函数
        message: 日志消息文本
    """
    _setup_file_logging()
    if log_callback:
        log_callback(f"[WARN] {message}")
    logger = logging.getLogger(LOGGER_ROOT)
    logger.warning(message)


def log_error(log_callback, message: str) -> None:
    """
    输出 ERROR 级别日志，同时持久化到日志文件。
    GUI 显示自动添加 [ERROR] 前缀。

    Args:
        log_callback: 日志回调函数
        message: 日志消息文本
    """
    _setup_file_logging()
    if log_callback:
        log_callback(f"[ERROR] {message}")
    logger = logging.getLogger(LOGGER_ROOT)
    logger.error(message)


def make_keyword_log(log_callback, keyword: str):
    """
    为并行关键词任务创建带前缀的日志回调。
    返回的回调会自动在消息前添加 [keyword] 前缀，用于区分不同关键词的日志。

    Args:
        log_callback: 原始日志回调函数
        keyword: 关键词文本

    Returns:
        带前缀的日志回调函数
    """
    def _log(msg: str) -> None:
        log_line(log_callback, f"[{keyword}] {msg}")
    return _log

