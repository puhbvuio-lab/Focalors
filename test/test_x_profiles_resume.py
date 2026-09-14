from concurrent.futures import Future
from unittest.mock import MagicMock, patch

from src.platforms.x_twitter.profiles import extract_profile_record, run_scraper


class InlineExecutor:
    def __init__(self, *args, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def submit(self, function, *args, **kwargs):
        future = Future()
        try:
            future.set_result(function(*args, **kwargs))
        except BaseException as exc:
            future.set_exception(exc)
        return future


@patch("src.platforms.x_twitter.profile_tweets.navigate_to_profile", return_value=False)
def test_profile_record_uses_shared_navigation_and_recovery(mock_navigate):
    page = MagicMock()
    recovery_config = {"x_recovery_wait_1": 1}

    result = extract_profile_record(
        page,
        "https://x.com/demo",
        log_callback=None,
        page_timeout=1234,
        recovery_config=recovery_config,
        use_search_entry=True,
    )

    assert result is None
    page.goto.assert_not_called()
    assert mock_navigate.call_args.kwargs["recovery_config"] is recovery_config
    assert mock_navigate.call_args.kwargs["use_search_entry"] is True


@patch("src.platforms.x_twitter.profiles.ThreadPoolExecutor", new=InlineExecutor)
@patch("src.platforms.x_twitter.profiles._scrape_single_profile_task")
@patch("src.platforms.x_twitter.profiles.open_checkpointed_row_writer")
@patch("src.platforms.x_twitter.profiles.open_task_checkpoint")
@patch("src.core.ensure_chrome_for_cdp")
def test_profiles_default_path_uses_checkpoint_and_search_entry(
    mock_ensure,
    mock_open_checkpoint,
    mock_open_writer,
    mock_scrape,
    tmp_path,
):
    input_path = tmp_path / "profiles.txt"
    input_path.write_text("https://x.com/demo\n", encoding="utf-8")
    checkpoint = MagicMock()
    mock_open_checkpoint.return_value = checkpoint
    writer = MagicMock()
    writer.worksheet.iter_rows.return_value = []
    mock_open_writer.return_value = ("resume-profiles.xlsx", writer)
    finish_callback = MagicMock()

    run_scraper(
        str(input_path),
        "博主链接",
        "http://localhost:9222",
        log_callback=None,
        finish_callback=finish_callback,
        config={
            "max_parallel_tabs": 1,
            "profile_entry_mode": "搜索页进入",
            "x_recovery_wait_1": 1,
        },
    )

    mock_open_checkpoint.assert_called_once()
    mock_open_writer.assert_called_once()
    checkpoint.add_output_path.assert_called_once_with("resume-profiles.xlsx")
    assert mock_scrape.call_args.kwargs["checkpoint"] is checkpoint
    assert mock_scrape.call_args.kwargs["output_path"] == "resume-profiles.xlsx"
    assert mock_scrape.call_args.kwargs["use_search_entry"] is True
    assert mock_scrape.call_args.kwargs["recovery_config"]["x_recovery_wait_1"] == 1
    checkpoint.close_run.assert_called_once()
    finish_callback.assert_called_once_with("resume-profiles.xlsx")
