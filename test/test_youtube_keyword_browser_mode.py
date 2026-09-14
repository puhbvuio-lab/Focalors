from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.core import TaskFeedback
from src.platforms.youtube.keyword import run_youtube_keyword


class _DummyWriter:
    def writerow(self, *args, **kwargs):
        return None

    def writerows(self, *args, **kwargs):
        return None

    def save(self):
        return None


def test_browser_failure_only_falls_back_for_current_youtube_keyword(tmp_path: Path):
    browser = MagicMock()
    browser.close.return_value = None

    first_page = MagicMock()
    second_page = MagicMock()
    browser.new_page.side_effect = [first_page, second_page]

    playwright_manager = MagicMock()
    playwright_manager.start.return_value = MagicMock()

    output_paths = [str(tmp_path / "kw1.xlsx"), str(tmp_path / "kw2.xlsx")]
    finish_callback = MagicMock()
    feedback = TaskFeedback()

    with (
        patch("src.platforms.youtube.keyword.build_output_path", side_effect=output_paths),
        patch("src.platforms.youtube.keyword.XlsxRowWriter", side_effect=lambda *args, **kwargs: _DummyWriter()),
        patch("src.platforms.youtube.keyword.MultiSheetXlsxWriter", side_effect=lambda *args, **kwargs: _DummyWriter()),
        patch("src.platforms.youtube.keyword.YouTubeClientPool", return_value=MagicMock()),
        patch("src.platforms.youtube.keyword.fetch_video_rows_pro", return_value=[]),
        patch("src.platforms.youtube.keyword.iter_search_video_id_batches", side_effect=lambda *args, **kwargs: iter(())),
        patch("src.platforms.youtube.snapshot_scheduler.process_due_jobs"),
        patch("src.platforms.youtube.snapshot_scheduler.register_job"),
        patch("src.core.connect_existing_chromium", return_value=(browser, None)),
        patch("playwright.sync_api.sync_playwright", return_value=playwright_manager),
        patch(
            "src.platforms.youtube.keyword.collect_video_ids_with_playwright",
            side_effect=[Exception("first keyword failed"), ["video_2"]],
        ) as collect_ids,
    ):
        run_youtube_keyword(
            api_keys=["key-a"],
            keywords_list=["kw1", "kw2"],
            max_results=10,
            limit_time_str="否",
            start_date="2026-06-01",
            end_date="2026-06-24",
            get_comments_str="否",
            max_comments=100,
            check_video_type_str="否",
            auto_snapshot_3d_str="否",
            auto_snapshot_7d_str="否",
            log_callback=MagicMock(),
            finish_callback=finish_callback,
            stop_event=None,
            config={"youtube_search_method": "浏览器优先（省配额）", "_task_feedback": feedback},
            pause_event=None,
        )

    assert collect_ids.call_count == 2
    finish_callback.assert_called_once_with(output_paths[-1])
    assert feedback.snapshot()["completed"] == feedback.snapshot()["total"] == 2
