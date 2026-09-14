from __future__ import annotations

import os
import sys
from unittest.mock import MagicMock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from PyQt5.QtWidgets import QApplication

from src.platforms.x_twitter.keyword import build_search_url, resolve_search_tab_filter
from src.platforms.x_twitter.windows import XKeywordWindow


def test_x_keyword_helpers_support_latest_and_top():
    assert resolve_search_tab_filter("latest") == "live"
    assert resolve_search_tab_filter("top") == "top"
    assert build_search_url("genshin impact", "latest").endswith("&f=live")
    assert build_search_url("genshin impact", "top").endswith("&f=top")


def test_x_keyword_window_passes_search_tab_to_spider():
    app = QApplication.instance() or QApplication([])
    window = XKeywordWindow()

    assert window.widgets["search_tab"].currentText() == "最新 (Latest)"

    values = {
        "keywords": "genshin",
        "lang": "英文 (en)",
        "search_tab": "热门 (Top)",
        "limit_time": "否",
        "start_date": "2026-06-01",
        "end_date": "2026-06-24",
        "get_comments": "否",
        "max_comments": 500,
    }

    with patch("src.platforms.x_twitter.keyword.run_x_spider") as mock_run:
        window.run_task(values, MagicMock(), MagicMock(), None, None)

    adv_params = mock_run.call_args.args[1]
    assert adv_params["lang"] == "en"
    assert adv_params["search_tab"] == "top"
