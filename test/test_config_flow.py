from __future__ import annotations

import os
import sys
from unittest.mock import MagicMock, patch

# Ensure offscreen Qt platform
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import pytest
from PyQt5.QtWidgets import QApplication

from src.core.config_store import (
    save_config,
    GLOBAL_TOOL_ID,
    GLOBAL_CONFIG_DEFAULTS,
)
# Imports will be deferred inside the test functions to isolate potential import or syntax errors in platform modules.



_app = None


def _ensure_app():
    global _app
    if _app is None:
        _app = QApplication.instance() or QApplication([])
    return _app


@pytest.fixture
def temp_config_dir(tmp_path):
    """Patch get_config_dir to use a temporary directory for config isolation."""
    with patch("src.core.config_store.get_config_dir", return_value=tmp_path):
        yield tmp_path


def test_tiktok_config_flow(temp_config_dir):
    _ensure_app()

    # 1. Save custom global configurations
    global_custom = {
        "cooldown_min": 12.5,
        "cooldown_max": 19.5,
        "comment_top_limit": 150,
        "scroll_px": 3500,
    }
    # Update default global values to include our custom settings
    global_values = dict(GLOBAL_CONFIG_DEFAULTS)
    global_values.update(global_custom)
    save_config(GLOBAL_TOOL_ID, global_values, GLOBAL_CONFIG_DEFAULTS)

    # 2. Save custom TikTok keyword tool configurations
    tiktok_defaults = {
        "max_videos": 1000,
        "max_candidates": 3000,
        "max_search_scrolls": 360,
        "max_parallel_tabs": 1,
        "max_comment_tabs": 1,
        "max_queue_size": 5000,
        "cooldown_min": 3.0,
        "cooldown_max": 8.0,
    }
    tiktok_custom = {
        "max_parallel_tabs": 3,
        "max_videos": 500,
    }
    save_config("tiktok_keyword_metrics", tiktok_custom, tiktok_defaults)

    # 3. Instantiate window (loads configurations automatically)
    from src.platforms.tiktok.windows import TikTokKeywordWindow
    window = TikTokKeywordWindow()

    # Verify configurations loaded correctly
    assert window.config_values["max_parallel_tabs"] == 3
    assert window.config_values["max_videos"] == 500
    assert window.config_values["cooldown_min"] == 12.5  # Injected from global
    assert window.config_values["cooldown_max"] == 19.5  # Injected from global
    # Note: comment_top_limit is also a global config param
    assert window.config_values["comment_top_limit"] == 150

    # 4. Prepare fields and task values
    values = {
        "keywords": "test_keyword",
        "limit_time": "否",
        "start_date": "2025-05-06",
        "end_date": "2026-05-06",
        "get_comments": "否",
        "max_comments": 100,
    }
    
    # Simulate _run_worker merging config_values into values dict
    for key, val in window.config_values.items():
        values[key] = val

    # 5. Mock run_tiktok_spider and execute task
    with patch("src.platforms.tiktok.keyword.run_tiktok_spider") as mock_run:
        window.run_task(values, MagicMock(), MagicMock(), MagicMock(), MagicMock())

    # 6. Verify args injected into spider
    mock_run.assert_called_once()
    passed_config = mock_run.call_args.kwargs.get("config", {})
    assert passed_config["max_parallel_tabs"] == 3
    assert passed_config["cooldown_min"] == 12.5
    assert passed_config["cooldown_max"] == 19.5
    assert passed_config["comment_top_limit"] == 150
    assert passed_config["max_videos"] == 500


def test_facebook_config_flow(temp_config_dir):
    _ensure_app()

    # 1. Save custom global configurations
    global_custom = {
        "cooldown_min": 1.5,
        "cooldown_max": 4.5,
        "comment_top_limit": 120,
        "scroll_px": 1234,
    }
    global_values = dict(GLOBAL_CONFIG_DEFAULTS)
    global_values.update(global_custom)
    save_config(GLOBAL_TOOL_ID, global_values, GLOBAL_CONFIG_DEFAULTS)

    # 2. Save Facebook profile works specific configurations
    fb_defaults = {
        "max_posts": 100,
        "max_scrolls": 200,
        "page_load_timeout": 60000,
        "scroll_delay": 2000,
        "collect_comments": "否",
        "cooldown_min": 1.0,
        "cooldown_max": 3.0,
    }
    fb_custom = {
        "max_scrolls": 50,
        "max_posts": 80,
    }
    save_config("facebook_profile_works", fb_custom, fb_defaults)

    # 3. Instantiate window
    from src.platforms.facebook.windows import FacebookProfileWorksWindow
    window = FacebookProfileWorksWindow()

    # Verify configurations loaded correctly
    assert window.config_values["max_scrolls"] == 50
    assert window.config_values["max_posts"] == 80
    assert window.config_values["cooldown_min"] == 1.5
    assert window.config_values["cooldown_max"] == 4.5
    assert window.config_values["comment_top_limit"] == 120
    assert window.config_values["scroll_px"] == 1234

    # 4. Prepare fields and task values
    values = {
        "profile_urls": "https://www.facebook.com/test_user",
        "limit_time": "否",
        "start_date": "2025-06-04",
        "end_date": "2026-06-04",
        "collect_comments": "否",
        "force_exact_time": "否",
    }

    # Simulate _run_worker merging config_values into values dict
    for key, val in window.config_values.items():
        values[key] = val

    # 5. Mock run_facebook_profile_works_spider and execute task
    # Note: For Facebook, the spider function is imported at module-level in windows.py
    with patch("src.platforms.facebook.windows.run_facebook_profile_works_spider") as mock_run:
        window.run_task(values, MagicMock(), MagicMock(), MagicMock(), MagicMock())

    # 6. Verify args injected into spider
    mock_run.assert_called_once()
    passed_kwargs = mock_run.call_args.kwargs
    assert passed_kwargs["max_scrolls"] == 50
    assert passed_kwargs["max_posts"] == 80
    assert passed_kwargs["cooldown_min"] == 1.5
    assert passed_kwargs["cooldown_max"] == 4.5
    assert passed_kwargs["comment_top_limit"] == 120
    assert passed_kwargs["scroll_px"] == 1234


def test_x_twitter_config_flow(temp_config_dir):
    _ensure_app()

    # 1. Save custom global configurations
    global_custom = {
        "cooldown_min": 5.5,
        "cooldown_max": 12.5,
        "scroll_px": 1800,
    }
    global_values = dict(GLOBAL_CONFIG_DEFAULTS)
    global_values.update(global_custom)
    save_config(GLOBAL_TOOL_ID, global_values, GLOBAL_CONFIG_DEFAULTS)

    # 2. Save X profile tweets specific configurations
    x_defaults = {
        "max_scrolls": 300,
        "truncate_threshold": 1000,
        "date_window_size": 20,
        "initial_load_delay": 2.0,
        "page_load_timeout": 30000,
        "scroll_interval": 3.2,
        "scroll_px": 2800,
        "no_new_scroll_limit": 10,
        "save_batch_size": 10,
        "cooldown_min": 6.0,
        "cooldown_max": 15.0,
        "guarantee_min_scrolls": 15,
        "max_parallel_tabs": 3,
    }
    x_custom = {
        "max_parallel_tabs": 4,
        "max_scrolls": 150,
        "cooldown_min": 7.5,
        "cooldown_max": 14.5,
        "scroll_px": 1850,
    }
    save_config("x_profile_tweets", x_custom, x_defaults)

    # 3. Instantiate window
    from src.platforms.x_twitter.windows import XProfileTweetsWindow
    window = XProfileTweetsWindow()

    # Verify configurations loaded correctly
    assert window.config_values["max_parallel_tabs"] == 4
    assert window.config_values["max_scrolls"] == 150
    assert window.config_values["cooldown_min"] == 7.5
    assert window.config_values["cooldown_max"] == 14.5
    assert window.config_values["scroll_px"] == 1850

    # 4. Prepare fields and task values
    values = {
        "profile_urls": "https://x.com/test_user",
        "keywords": "",
        "limit_time": "否",
        "start_date": "2025-05-06",
        "end_date": "2026-05-06",
        "get_comments": "否",
        "max_comments": 100,
    }

    # Simulate _run_worker merging config_values into values dict
    for key, val in window.config_values.items():
        values[key] = val

    # 5. Mock run_x_profile_tweets_spider and execute task
    with patch("src.platforms.x_twitter.profile_tweets.run_x_profile_tweets_spider") as mock_run:
        window.run_task(values, MagicMock(), MagicMock(), MagicMock(), MagicMock())

    # 6. Verify args injected into spider
    mock_run.assert_called_once()
    passed_config = mock_run.call_args.kwargs.get("config", {})
    assert passed_config["max_parallel_tabs"] == 4
    assert passed_config["max_scrolls"] == 150
    assert passed_config["cooldown_min"] == 7.5
    assert passed_config["cooldown_max"] == 14.5
    assert passed_config["scroll_px"] == 1850
