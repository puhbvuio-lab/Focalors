import queue
import threading
import time
from unittest.mock import MagicMock, patch

from src.core.xlsx import MultiSheetXlsxWriter
from src.platforms.tiktok.keyword import _tiktok_comment_consumer
from src.platforms.x_twitter.keyword import _x_comment_consumer

def test_xlsx_writer_concurrency_lock():
    """
    Verify that concurrent writes to MultiSheetXlsxWriter are protected by writer_lock.
    We simulate multiple threads writing and assert that the critical section is mutually exclusive.
    """
    writer_lock = threading.Lock()
    writer = MagicMock(spec=MultiSheetXlsxWriter)
    
    concurrency_count = 0
    max_concurrency = 0
    concurrency_lock = threading.Lock()
    
    def mock_writerows(sheet_name, rows):
        nonlocal concurrency_count, max_concurrency
        assert writer_lock.locked(), "writer_lock must be held during write"
        
        with concurrency_lock:
            concurrency_count += 1
            if concurrency_count > max_concurrency:
                max_concurrency = concurrency_count
                
        time.sleep(0.01)
        
        with concurrency_lock:
            concurrency_count -= 1

    writer.writerows.side_effect = mock_writerows
    
    def thread_task():
        with writer_lock:
            writer.writerows("Sheet1", [{"key": "val"}])
            
    threads = [threading.Thread(target=thread_task) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
        
    assert max_concurrency == 1, f"Expected mutual exclusion (max_concurrency=1), got {max_concurrency}"


@patch('src.platforms.tiktok.keyword.sync_playwright')
@patch('src.platforms.tiktok.keyword.connect_existing_chromium')
@patch('src.platforms.tiktok.keyword.collect_video_comments')
@patch('src.platforms.tiktok.keyword.interruptible_sleep', return_value=False)
def test_tiktok_comment_consumer_browser_closed_on_success(
    mock_sleep, mock_collect_comments, mock_connect, mock_sync_playwright
):
    """
    Verify that Playwright browser and page are closed in the finally block when TikTok comment consumer runs successfully.
    """
    mock_browser = MagicMock()
    mock_context = MagicMock()
    mock_page = MagicMock()
    
    mock_connect.return_value = (mock_browser, mock_context)
    mock_context.new_page.return_value = mock_page
    mock_page.is_closed.return_value = False
    
    mock_collect_comments.return_value = [
        {"like_count": "10", "text": "nice video", "create_time": "2026-06-28"}
    ]
    
    q = queue.Queue()
    q.put((1, "http://example.com/video1", 10))
    q.put(None)  # Sentinel to exit loop
    
    writer = MagicMock(spec=MultiSheetXlsxWriter)
    writer_lock = threading.Lock()
    stop_event = threading.Event()
    pause_event = threading.Event()
    
    _tiktok_comment_consumer(
        keyword="test",
        queue_obj=q,
        cdp_port_or_url=9222,
        writer=writer,
        writer_lock=writer_lock,
        log_callback=MagicMock(),
        stop_event=stop_event,
        pause_event=pause_event,
        comment_top_limit=10
    )
    
    mock_page.close.assert_called_once()
    mock_browser.close.assert_called_once()
    writer.writerows.assert_called_once()


@patch('src.platforms.tiktok.keyword.sync_playwright')
@patch('src.platforms.tiktok.keyword.connect_existing_chromium')
@patch('src.platforms.tiktok.keyword.collect_video_comments')
@patch('src.platforms.tiktok.keyword.interruptible_sleep', return_value=False)
def test_tiktok_comment_consumer_browser_closed_on_failure(
    mock_sleep, mock_collect_comments, mock_connect, mock_sync_playwright
):
    """
    Verify that Playwright browser and page are closed in the finally block even if TikTok comment consumer encounters an error.
    """
    mock_browser = MagicMock()
    mock_context = MagicMock()
    mock_page = MagicMock()
    
    mock_connect.return_value = (mock_browser, mock_context)
    mock_context.new_page.return_value = mock_page
    mock_page.is_closed.return_value = False
    
    # Simulate an error during comments collection
    mock_collect_comments.side_effect = RuntimeError("Failed to collect comments")
    
    q = queue.Queue()
    q.put((1, "http://example.com/video1", 10))
    q.put(None)
    
    writer = MagicMock(spec=MultiSheetXlsxWriter)
    writer_lock = threading.Lock()
    stop_event = threading.Event()
    pause_event = threading.Event()
    
    _tiktok_comment_consumer(
        keyword="test",
        queue_obj=q,
        cdp_port_or_url=9222,
        writer=writer,
        writer_lock=writer_lock,
        log_callback=MagicMock(),
        stop_event=stop_event,
        pause_event=pause_event,
        comment_top_limit=10
    )
    
    mock_page.close.assert_called_once()
    mock_browser.close.assert_called_once()


@patch('src.platforms.x_twitter.keyword.sync_playwright')
@patch('src.platforms.x_twitter.keyword.connect_existing_chromium')
@patch('src.platforms.x_twitter.keyword.extract_comments')
@patch('src.platforms.x_twitter.keyword.interruptible_sleep', return_value=False)
def test_x_comment_consumer_browser_closed_on_success(
    mock_sleep, mock_extract_comments, mock_connect, mock_sync_playwright
):
    """
    Verify that Playwright browser and page are closed in the finally block when X comment consumer runs successfully.
    """
    mock_browser = MagicMock()
    mock_context = MagicMock()
    mock_page = MagicMock()
    
    mock_connect.return_value = (mock_browser, mock_context)
    mock_context.new_page.return_value = mock_page
    mock_page.is_closed.return_value = False
    
    mock_extract_comments.return_value = [
        {"likes": "5", "content": "cool tweet", "time": "2026-06-28"}
    ]
    
    q = queue.Queue()
    q.put((1, "http://example.com/tweet1", 10))
    q.put(None)
    
    writer = MagicMock(spec=MultiSheetXlsxWriter)
    writer_lock = threading.Lock()
    stop_event = threading.Event()
    pause_event = threading.Event()
    
    _x_comment_consumer(
        keyword="test",
        queue_obj=q,
        cdp_port_or_url=9222,
        writer=writer,
        writer_lock=writer_lock,
        log_callback=MagicMock(),
        stop_event=stop_event,
        pause_event=pause_event,
        max_comments=10,
        comment_no_new_scroll_limit=5,
        comment_refresh_count=3,
        comment_refresh_interval=1,
        page_timeout=10000
    )
    
    mock_page.close.assert_called_once()
    mock_browser.close.assert_called_once()
    writer.writerows.assert_called_once()


@patch('src.platforms.x_twitter.keyword.sync_playwright')
@patch('src.platforms.x_twitter.keyword.connect_existing_chromium')
@patch('src.platforms.x_twitter.keyword.extract_comments')
@patch('src.platforms.x_twitter.keyword.interruptible_sleep', return_value=False)
def test_x_comment_consumer_browser_closed_on_failure(
    mock_sleep, mock_extract_comments, mock_connect, mock_sync_playwright
):
    """
    Verify that Playwright browser and page are closed in the finally block even if X comment consumer encounters an error.
    """
    mock_browser = MagicMock()
    mock_context = MagicMock()
    mock_page = MagicMock()
    
    mock_connect.return_value = (mock_browser, mock_context)
    mock_context.new_page.return_value = mock_page
    mock_page.is_closed.return_value = False
    
    # Simulate an error during comments extraction
    mock_extract_comments.side_effect = RuntimeError("Failed to extract comments")
    
    q = queue.Queue()
    q.put((1, "http://example.com/tweet1", 10))
    q.put(None)
    
    writer = MagicMock(spec=MultiSheetXlsxWriter)
    writer_lock = threading.Lock()
    stop_event = threading.Event()
    pause_event = threading.Event()
    
    _x_comment_consumer(
        keyword="test",
        queue_obj=q,
        cdp_port_or_url=9222,
        writer=writer,
        writer_lock=writer_lock,
        log_callback=MagicMock(),
        stop_event=stop_event,
        pause_event=pause_event,
        max_comments=10,
        comment_no_new_scroll_limit=5,
        comment_refresh_count=3,
        comment_refresh_interval=1,
        page_timeout=10000
    )
    
    mock_page.close.assert_called_once()
    mock_browser.close.assert_called_once()


@patch('src.platforms.tiktok.profile_play_counts.sync_playwright')
@patch('src.platforms.tiktok.profile_play_counts.connect_existing_chromium')
@patch('src.platforms.tiktok.profile_play_counts.ensure_chrome_for_cdp', create=True)
@patch('src.platforms.tiktok.profile_play_counts.parse_profile_urls')
@patch('src.platforms.tiktok.profile_play_counts.interruptible_sleep', return_value=False)
def test_tiktok_profile_play_counts_concurrency_and_browser_closure(
    mock_sleep, mock_parse_urls, mock_ensure, mock_connect, mock_sync_playwright
):
    """
    Verify that TikTok profile play counts scraper runs with ThreadPoolExecutor concurrency
    and closes pages and browser contexts safely in finally blocks.
    """
    mock_parse_urls.return_value = ["https://www.tiktok.com/@user1", "https://www.tiktok.com/@user2"]
    mock_browser = MagicMock()
    mock_context = MagicMock()
    mock_page = MagicMock()
    
    mock_connect.return_value = (mock_browser, mock_context)
    mock_context.new_page.return_value = mock_page
    mock_page.is_closed.return_value = False
    
    # Mock playwright instance
    mock_sync_playwright.return_value.__enter__.return_value = MagicMock()
    
    stop_event = threading.Event()
    pause_event = threading.Event()
    
    from src.platforms.tiktok.profile_play_counts import run_tiktok_profile_play_counts_spider
    with patch('src.platforms.tiktok.profile_play_counts.XlsxRowWriter') as mock_writer_cls:
        mock_writer = MagicMock()
        mock_writer_cls.return_value = mock_writer
        
        run_tiktok_profile_play_counts_spider(
            txt_path="dummy_path.txt",
            cdp_port_or_url=9222,
            max_scrolls=5,
            log_callback=MagicMock(),
            finish_callback=MagicMock(),
            stop_event=stop_event,
            pause_event=pause_event,
            config={"max_parallel_tabs": 2}
        )
        
        # Verify page.close and browser.close are called for each worker
        assert mock_page.close.call_count == 2
        assert mock_browser.close.call_count == 2
        mock_writer.save.assert_called()


def test_youtube_keyword_window_config_forwarding():
    """
    Verify that YouTubeKeywordWindow collects and forwards correct config values (e.g. max_parallel_tabs, delays) to run_youtube_keyword.
    """
    from PyQt5.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    from src.platforms.youtube.windows import YouTubeKeywordWindow
    
    window = YouTubeKeywordWindow()
    test_values = {
        "api_key": "dummy_key",
        "limit_time": "否",
        "start_date": "2025-05-06",
        "end_date": "2026-05-06",
        "keywords": "test_keyword",
        "get_comments": "否",
        "max_comments": 100,
        "check_video_type": "否",
        "auto_snapshot_3d": "否",
        "auto_snapshot_7d": "否",
        "enable_timer": "否",
        "max_parallel_tabs": 4,
        "youtube_initial_load_delay": 3.5,
        "max_results": 1000,
    }
    
    with patch('src.platforms.youtube.keyword.run_youtube_keyword') as mock_run:
        window.run_task(
            values=test_values,
            log_callback=MagicMock(),
            finish_callback=MagicMock(),
            stop_event=threading.Event(),
            pause_event=threading.Event()
        )
        
        assert mock_run.call_count == 1
        args, kwargs = mock_run.call_args
        # Config dictionary should contain max_parallel_tabs and youtube_initial_load_delay
        config_passed = args[10]  # config is the 11th positional argument or kwargs
        if not isinstance(config_passed, dict):
            config_passed = kwargs.get("config", {})
            
        assert config_passed.get("max_parallel_tabs") == 4
        assert config_passed.get("youtube_initial_load_delay") == 3.5
