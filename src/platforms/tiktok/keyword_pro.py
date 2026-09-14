# -*- coding: utf-8 -*-
"""
TikTok 关键词搜索 Pro 模块。
支持基于时间间隔的自动循环定时采集，支持中途停止和休眠。
"""

from __future__ import annotations

from datetime import datetime

from src.core import interruptible_sleep, should_stop, log_line, log_error
from src.platforms.tiktok.keyword import run_tiktok_spider, parse_date_range

def run_tiktok_keyword_pro_spider(
    keywords_list,
    max_videos,
    max_candidates,
    limit_time_str,
    start_date,
    end_date,
    get_comments_str,
    max_comments,
    cdp_port_or_url,
    log_callback,
    finish_callback,
    stop_event=None,
    pause_event=None,
    config=None,
):
    """
    TikTok 关键词搜索 Pro 定时爬虫主入口。
    包装原有的 run_tiktok_spider，支持定时循环抓取及时间范围自动推移。
    """
    if config is None:
        config = {}

    enable_timer = config.get("enable_timer", "否") == "是"
    timer_interval_minutes = int(config.get("timer_interval_minutes", 60))
    timer_max_runs = int(config.get("timer_max_runs", 3))

    limit_time_bool = limit_time_str == "是"
    start_dt, end_dt = None, None
    if limit_time_bool:
        try:
            start_dt, end_dt = parse_date_range(start_date, end_date)
        except Exception as exc:
            log_error(log_callback, f"解析时间范围失败: {exc}")
            finish_callback(None)
            return

    all_output_paths = []

    def local_finish_callback(path):
        if path:
            all_output_paths.append(path)

    current_run = 0

    try:
        while True:
            if should_stop(stop_event):
                log_line(log_callback, "任务已停止。")
                break

            current_run += 1

            if enable_timer and limit_time_bool:
                now = datetime.now()
                if current_run == 1:
                    end_dt = min(end_dt, now)
                else:
                    end_dt = now

            if enable_timer:
                log_line(log_callback, f"=== 开始执行第 {current_run} 次任务 ===")
                if limit_time_bool:
                    log_line(
                        log_callback,
                        f"  定时模式：本次时间范围 {start_dt.strftime('%Y-%m-%d')} 至 {end_dt.strftime('%Y-%m-%d')}"
                    )

            start_date_param = start_dt.strftime("%Y-%m-%d") if limit_time_bool else start_date
            end_date_param = end_dt.strftime("%Y-%m-%d") if limit_time_bool else end_date

            run_tiktok_spider(
                keywords_list,
                max_videos,
                max_candidates,
                limit_time_str,
                start_date_param,
                end_date_param,
                get_comments_str,
                max_comments,
                cdp_port_or_url,
                log_callback,
                local_finish_callback,
                stop_event=stop_event,
                pause_event=pause_event,
                config=config,
            )

            if enable_timer and limit_time_bool and end_dt is not None:
                start_dt = end_dt

            if should_stop(stop_event):
                break

            if not enable_timer or current_run >= timer_max_runs:
                break

            log_line(log_callback, f"=== 本次执行完毕。等待 {timer_interval_minutes} 分钟后进行下一次执行 ===")
            if interruptible_sleep(timer_interval_minutes * 60, stop_event):
                log_line(log_callback, "休眠被中断，任务终止。")
                break

    except Exception as exc:
        log_error(log_callback, f"Pro 定时任务执行异常: {exc}")
    finally:
        finish_callback(all_output_paths if all_output_paths else None)
