import os
import subprocess
import sys
from pathlib import Path


def test_visibility():
    project_root = Path(__file__).resolve().parents[1]
    script = r"""
from PyQt5.QtWidgets import QApplication
from src.studio.qt_app import ThreePlatformCrawlerQtApp
from src.platforms.tiktok.windows import TikTokKeywordWindow, TikTokProfileVideosWindow, TikTokVideoDetailsWindow
from src.platforms.x_twitter.windows import XKeywordWindow, XTweetMetricsWindow, XProfileTweetsWindow
from src.platforms.youtube.windows import YouTubeKeywordWindow, YouTubeChannelWorksWindow

app = QApplication.instance() or QApplication([])
windows = [
    ThreePlatformCrawlerQtApp(),
    TikTokKeywordWindow(),
    TikTokProfileVideosWindow(),
    TikTokVideoDetailsWindow(),
    XKeywordWindow(),
    XTweetMetricsWindow(),
    XProfileTweetsWindow(),
    YouTubeKeywordWindow(),
    YouTubeChannelWorksWindow(),
]
try:
    for window in windows:
        widgets = getattr(window, "widgets", {})
        limit_combo = widgets.get("limit_time")
        if limit_combo:
            limit_combo.setCurrentText("否")
            limit_combo.setCurrentText("是")
        get_comments_combo = widgets.get("get_comments")
        if get_comments_combo:
            get_comments_combo.setCurrentText("是")
finally:
    for window in windows:
        window.deleteLater()
    app.processEvents()
"""
    environment = os.environ.copy()
    environment.setdefault("QT_QPA_PLATFORM", "offscreen")
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=project_root,
        env=environment,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
