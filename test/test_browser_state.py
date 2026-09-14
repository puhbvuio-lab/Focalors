import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.core import browser

BROWSER_CHROME = browser.BROWSER_CHROME
BROWSER_EDGE = browser.BROWSER_EDGE
get_chrome_user_data_dir = browser.get_chrome_user_data_dir


def _touch_marker(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / "marker").write_text("1", encoding="utf-8")


class TestBrowserStatePaths(unittest.TestCase):
    def test_new_install_uses_stable_local_profile_dir(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {}, clear=False):
            os.environ.pop("SCRAPER_CHROME_USER_DATA_DIR", None)
            workspace = Path(tmp) / "workspace"
            state = Path(tmp) / "state"
            with patch("src.core.browser.get_workspace_root", return_value=workspace), patch(
                "src.core.browser.get_app_state_root",
                return_value=state,
            ):
                self.assertEqual(
                    Path(get_chrome_user_data_dir(BROWSER_CHROME)),
                    state / "browser_profiles" / "chrome",
                )

    def test_existing_legacy_profile_is_reused_before_empty_stable_dir(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {}, clear=False):
            os.environ.pop("SCRAPER_EDGE_USER_DATA_DIR", None)
            workspace = Path(tmp) / "workspace"
            state = Path(tmp) / "state"
            _touch_marker(workspace / "user_data_edge")
            (state / "browser_profiles" / "edge").mkdir(parents=True, exist_ok=True)
            with patch("src.core.browser.get_workspace_root", return_value=workspace), patch(
                "src.core.browser.get_app_state_root",
                return_value=state,
            ):
                self.assertEqual(Path(get_chrome_user_data_dir(BROWSER_EDGE)), workspace / "user_data_edge")

    def test_existing_stable_profile_wins_after_migration(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {}, clear=False):
            os.environ.pop("SCRAPER_CHROME_USER_DATA_DIR", None)
            workspace = Path(tmp) / "workspace"
            state = Path(tmp) / "state"
            _touch_marker(workspace / "user_data")
            _touch_marker(state / "browser_profiles" / "chrome")
            with patch("src.core.browser.get_workspace_root", return_value=workspace), patch(
                "src.core.browser.get_app_state_root",
                return_value=state,
            ):
                self.assertEqual(
                    Path(get_chrome_user_data_dir(BROWSER_CHROME)),
                    state / "browser_profiles" / "chrome",
                )


if __name__ == "__main__":
    unittest.main(verbosity=2)
