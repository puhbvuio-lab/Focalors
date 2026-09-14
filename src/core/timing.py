"""
延时与频率限制控制模块，提供支持“开始/暂停/继续/停止”状态机调度的可中断式休眠功能。
"""

from __future__ import annotations

import random
import time

from src.core.app_logging import log_line


def should_stop(stop_event=None) -> bool:
    """
    检查爬虫任务是否已被用户触发停止（通过线程事件判断）。
    """
    return bool(stop_event and stop_event.is_set())


def interruptible_sleep(seconds: float, stop_event=None, step: float = 0.2) -> bool:
    """
    分段式休眠。支持在此期间随时响应并中断（如用户点击“停止”）。
    采用分步检查（默认每步最多 0.2 秒），使 GUI 界面对“停止”操作保持毫秒级灵敏度。

    Args:
        seconds: 总休眠秒数
        stop_event: 线程停止信号事件 (threading.Event)
        step: 单次轮询休眠的最大步长（秒）

    Returns:
        bool: 如果被用户强制停止则返回 True，否则顺利睡足时间返回 False。
    """
    end_time = time.time() + max(0, seconds)
    while time.time() < end_time:
        if should_stop(stop_event):
            return True
        # 睡剩余时长和步长之间的较小值，避免睡超时间或者因单次 sleep 过长导致响应卡顿
        time.sleep(min(step, max(0, end_time - time.time())))
    return should_stop(stop_event)


def random_cooldown(log_callback=None, stop_event=None, min_seconds: float = 3.0, max_seconds: float = 8.0, reason: str = "降低访问频率"):
    """
    生成随机冷却等待时间，模仿人类阅读或浏览轨迹，降低因高频请求被平台封禁 IP 的几率。
    """
    seconds = random.uniform(min_seconds, max_seconds)
    log_line(log_callback, f"  随机等待 {seconds:.1f} 秒，{reason}。")
    return interruptible_sleep(seconds, stop_event)


def interruptible_random_sleep(min_seconds: float, max_seconds: float, log_callback=None, stop_event=None, reason: str = "降低访问频率"):
    """
    带随机区间的可中断休眠，支持自定义日志原因。
    自动钳位保证 min/max 合法，返回 True 表示被中断。
    """
    min_seconds = max(0.0, float(min_seconds or 0))
    max_seconds = max(min_seconds, float(max_seconds or min_seconds))
    seconds = random.uniform(min_seconds, max_seconds)
    if seconds <= 0:
        return False
    log_line(log_callback, f"    {reason}，随机等待 {seconds:.1f} 秒。")
    return interruptible_sleep(seconds, stop_event)


def wait_if_paused(pause_event=None, stop_event=None) -> bool:
    """
    如果任务处于暂停状态（即 pause_event 被 set），则进入等待循环，直到暂停解除或收到停止信号。
    """
    while pause_event and pause_event.is_set():
        if should_stop(stop_event):
            return True
        time.sleep(0.1)
    return should_stop(stop_event)

