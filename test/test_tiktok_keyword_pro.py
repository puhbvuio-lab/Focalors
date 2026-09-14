# -*- coding: utf-8 -*-
"""
TikTok Keyword Pro crawler and window tests.
"""

import sys
import os
import pytest
from datetime import datetime
from unittest.mock import MagicMock, patch

from PyQt5.QtWidgets import QApplication

# Ensure project root is in sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.platforms.tiktok.keyword_pro import run_tiktok_keyword_pro_spider
from src.platforms.tiktok.windows import TikTokKeywordProWindow


def test_tiktok_keyword_pro_spider_loop():
    """Verify that running run_tiktok_keyword_pro_spider triggers crawler for specified max runs
    and that start/end dates roll forward correctly.
    """
    mock_run_spider = MagicMock()
    mock_sleep = MagicMock(return_value=False)  # Sleep returns False to indicate it was not interrupted

    keywords_list = ["test_key"]
    max_videos = 10
    max_candidates = 30
    limit_time_str = "是"
    start_date = "2026-06-01"
    end_date = "2026-06-05"
    get_comments_str = "否"
    max_comments = 100
    cdp_port_or_url = "http://localhost:9222"
    log_callback = MagicMock()

    # We want to capture the finish callback calls
    finish_callback = MagicMock()

    config = {
        "enable_timer": "是",
        "timer_interval_minutes": 10,
        "timer_max_runs": 3,
    }

    fixed_now = datetime(2026, 6, 10, 12, 0, 0)

    class MockDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed_now

    with patch("src.platforms.tiktok.keyword_pro.run_tiktok_spider", mock_run_spider), \
         patch("src.platforms.tiktok.keyword_pro.interruptible_sleep", mock_sleep), \
         patch("src.platforms.tiktok.keyword_pro.datetime", MockDatetime):

        run_tiktok_keyword_pro_spider(
            keywords_list=keywords_list,
            max_videos=max_videos,
            max_candidates=max_candidates,
            limit_time_str=limit_time_str,
            start_date=start_date,
            end_date=end_date,
            get_comments_str=get_comments_str,
            max_comments=max_comments,
            cdp_port_or_url=cdp_port_or_url,
            log_callback=log_callback,
            finish_callback=finish_callback,
            stop_event=None,
            pause_event=None,
            config=config,
        )

    # Verify run_tiktok_spider was called 3 times
    assert mock_run_spider.call_count == 3

    calls = mock_run_spider.call_args_list
    # First call arguments:
    args_1 = calls[0][0]
    assert args_1[4] == "2026-06-01"  # start_date
    assert args_1[5] == "2026-06-05"  # end_date

    # Second call arguments:
    args_2 = calls[1][0]
    assert args_2[4] == "2026-06-05"  # start_date
    assert args_2[5] == "2026-06-10"  # end_date

    # Third call arguments:
    args_3 = calls[2][0]
    assert args_3[4] == "2026-06-10"  # start_date
    assert args_3[5] == "2026-06-10"  # end_date

    # Verify that sleep was called 2 times
    assert mock_sleep.call_count == 2
    mock_sleep.assert_called_with(600, None)

    # Verify finish callback is called once
    finish_callback.assert_called_once()


def test_tiktok_keyword_pro_spider_stop_event():
    """Verify that setting stop_event immediately terminates the loop."""
    mock_run_spider = MagicMock()
    mock_sleep = MagicMock(return_value=True)  # True indicates sleep was interrupted

    stop_event = MagicMock()
    stop_event.is_set.side_effect = [False, True]  # Stop after first run

    keywords_list = ["test_key"]
    config = {
        "enable_timer": "是",
        "timer_interval_minutes": 10,
        "timer_max_runs": 3,
    }

    finish_callback = MagicMock()

    with patch("src.platforms.tiktok.keyword_pro.run_tiktok_spider", mock_run_spider), \
         patch("src.platforms.tiktok.keyword_pro.interruptible_sleep", mock_sleep):

        run_tiktok_keyword_pro_spider(
            keywords_list=keywords_list,
            max_videos=10,
            max_candidates=30,
            limit_time_str="否",
            start_date="",
            end_date="",
            get_comments_str="否",
            max_comments=100,
            cdp_port_or_url="http://localhost:9222",
            log_callback=MagicMock(),
            finish_callback=finish_callback,
            stop_event=stop_event,
            pause_event=None,
            config=config,
        )

    # Should run only once because stop_event becomes set
    assert mock_run_spider.call_count == 1
    assert mock_sleep.call_count == 0
    finish_callback.assert_called_once()


def test_tiktok_keyword_pro_window():
    """Instantiate TikTokKeywordProWindow, verify compilation, field specs, and validation logic."""
    app = QApplication.instance() or QApplication([])

    window = TikTokKeywordProWindow()
    assert window.tool_id == "tiktok_keyword_metrics_pro"
    assert window.windowTitle() == "TikTok 关键词搜索 Pro"

    # Check bindings
    limit_combo = window.widgets.get("limit_time")
    start_date_widget = window.widgets.get("start_date")
    end_date_widget = window.widgets.get("end_date")

    assert limit_combo is not None
    assert start_date_widget is not None
    assert end_date_widget is not None

    # Test validation logic
    # 1. No keyword
    with pytest.raises(ValueError, match="至少需要输入一个关键词"):
        window.validate_values({
            "keywords": "",
            "limit_time": "否",
            "enable_timer": "否",
        })

    # 2. enable_timer is 是 but limit_time is 否
    with pytest.raises(ValueError, match="定时模式必须开启时间过滤"):
        window.validate_values({
            "keywords": "test",
            "limit_time": "否",
            "enable_timer": "是",
        })

    # 3. limit_time is 是, but start_date > end_date
    with pytest.raises(ValueError, match="开始日期不能晚于结束日期"):
        window.validate_values({
            "keywords": "test",
            "limit_time": "是",
            "start_date": "2026-06-05",
            "end_date": "2026-06-01",
            "enable_timer": "否",
        })

    # 4. Valid validation
    window.validate_values({
        "keywords": "test",
        "limit_time": "是",
        "start_date": "2026-06-01",
        "end_date": "2026-06-05",
        "enable_timer": "是",
    })

    # Verify tool_config_params works
    params = window.tool_config_params()
    assert len(params) > 0
    param_keys = [p.key for p in params]
    assert "max_videos" in param_keys
    assert "max_candidates" in param_keys
