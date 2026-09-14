import os
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from src.core import browser


class TestBrowserReopen(unittest.TestCase):
    def test_ensure_page_target_opens_blank_when_cdp_has_no_pages(self):
        with (
            patch("src.core.browser._wait_for_initial_page", side_effect=[False, True]) as mock_wait,
            patch("src.core.browser._open_cdp_page_target", return_value=True) as mock_open,
        ):
            self.assertTrue(browser._ensure_cdp_page_target("9222", timeout=0.1))

        self.assertEqual(mock_wait.call_count, 2)
        mock_open.assert_called_once_with("9222")

    def test_existing_local_cdp_with_page_is_reused_without_restart(self):
        with (
            patch("src.core.browser.is_valid_cdp_endpoint", return_value=True),
            patch("src.core.browser._ensure_cdp_page_target", return_value=True) as mock_ensure,
            patch("src.core.browser._kill_chrome_on_port") as mock_kill,
            patch("src.core.browser.launch_chrome_for_cdp") as mock_launch,
        ):
            launched = browser.ensure_chrome_for_cdp("9222", browser=browser.BROWSER_CHROME)

        self.assertFalse(launched)
        mock_ensure.assert_called_once()
        mock_kill.assert_not_called()
        mock_launch.assert_not_called()

    def test_managed_local_cdp_without_page_restarts_browser(self):
        record = browser.ManagedBrowserProcess(
            pid=4321,
            browser_name=browser.BROWSER_CHROME,
            host="localhost",
            port=9222,
            user_data_dir="C:/managed",
            launched_by_app=True,
            owner_pid=os.getpid(),
            started_at=datetime.now(timezone.utc),
        )
        with (
            patch("src.core.browser.is_valid_cdp_endpoint", side_effect=[True, True]),
            patch("src.core.browser._ensure_cdp_page_target", side_effect=[False, True]),
            patch("src.core.browser._get_managed_browser_process", return_value=record),
            patch("src.core.browser._kill_chrome_on_port", return_value=True) as mock_kill,
            patch("src.core.browser.launch_chrome_for_cdp") as mock_launch,
            patch("src.core.browser.get_chrome_user_data_dir", return_value="C:/managed"),
            patch("src.core.browser.time.sleep"),
        ):
            launched = browser.ensure_chrome_for_cdp("9222", browser=browser.BROWSER_CHROME, wait_seconds=1)

        self.assertTrue(launched)
        mock_kill.assert_called_once_with("http://localhost:9222", log_callback=None, browser=browser.BROWSER_CHROME)
        mock_launch.assert_called_once_with("http://localhost:9222", browser=browser.BROWSER_CHROME)


if __name__ == "__main__":
    unittest.main(verbosity=2)
