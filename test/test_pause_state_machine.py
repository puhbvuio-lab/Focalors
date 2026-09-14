"""Test the pause/resume state machine, config parameter flow, and button transitions.

These tests verify the core pause infrastructure without requiring a real browser
or API keys. They exercise SimpleToolWindow directly.
"""

from __future__ import annotations

import os
import sys
import threading
import time

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

pytest.importorskip("PyQt5")

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QApplication

from src.core.timing import wait_if_paused, should_stop
from src.ui.base import SimpleToolWindow, FieldSpec
from src.ui.config_dialog import ConfigParam, ConfigDialog

_app = None


def _ensure_app():
    global _app
    if _app is None:
        _app = QApplication.instance() or QApplication([])
    return _app


# ---------------------------------------------------------------------------
# Test wait_if_paused and should_stop
# ---------------------------------------------------------------------------

def test_should_stop_none():
    assert should_stop(None) is False


def test_should_stop_not_set():
    event = threading.Event()
    assert should_stop(event) is False


def test_should_stop_set():
    event = threading.Event()
    event.set()
    assert should_stop(event) is True


def test_wait_if_paused_no_events():
    """Neither event provided → returns False immediately."""
    assert wait_if_paused(None, None) is False


def test_wait_if_paused_not_paused():
    """pause_event is clear (not paused) → returns False immediately."""
    pause = threading.Event()
    stop = threading.Event()
    assert wait_if_paused(pause, stop) is False


def test_wait_if_paused_stop_while_paused():
    """Paused, then stop is set in another thread → returns True."""
    pause = threading.Event()
    stop = threading.Event()
    pause.set()  # paused

    result_container = {"ret": None}

    def waiter():
        result_container["ret"] = wait_if_paused(pause, stop)

    t = threading.Thread(target=waiter)
    t.start()
    time.sleep(0.15)  # let the waiter enter the loop
    stop.set()  # trigger stop
    t.join(timeout=2)
    assert not t.is_alive(), "Thread should have returned"
    assert result_container["ret"] is True


def test_wait_if_paused_resume_then_stop():
    """Paused, then resumed (pause cleared), then stop → returns True."""
    pause = threading.Event()
    stop = threading.Event()
    pause.set()

    result_container = {"ret": None}

    def waiter():
        result_container["ret"] = wait_if_paused(pause, stop)

    t = threading.Thread(target=waiter)
    t.start()
    time.sleep(0.15)
    pause.clear()  # resume
    t.join(timeout=2)
    assert not t.is_alive(), "Thread should have returned after resume"
    assert result_container["ret"] is False


# ---------------------------------------------------------------------------
# Test SimpleToolWindow state machine
# ---------------------------------------------------------------------------

class _TestWindow(SimpleToolWindow):
    """Minimal window for testing state transitions."""

    task_feedback_enabled = True

    def __init__(self):
        super().__init__(
            "测试窗口",
            [
                FieldSpec("text_field", "文本字段", default="hello"),
                FieldSpec("limit_time", "是否限制时间？", kind="combo", options=("是", "否"), default="是"),
                FieldSpec("start_date", "开始日期", default="2025-01-01"),
            ],
        )
        self.bind_field_visibility("limit_time", "是", ["start_date"])
        self.task_ran = False
        self.received_pause_event = None
        self.received_config = None

    def run_task(self, values, log_callback, finish_callback, stop_event, pause_event):
        self.task_ran = True
        self.received_pause_event = pause_event
        self.received_config = {k: v for k, v in values.items() if k not in ("text_field", "limit_time", "start_date")}
        log_callback("任务开始")
        # Run until stop or max 50 iterations
        for i in range(50):
            if should_stop(stop_event):
                log_callback("任务已停止")
                finish_callback(None)
                return None
            if wait_if_paused(pause_event, stop_event):
                log_callback("任务已停止(暂停中)")
                finish_callback(None)
                return None
            time.sleep(0.02)
        log_callback("任务完成")
        finish_callback("/fake/output.xlsx")
        return "/fake/output.xlsx"


def test_initial_button_state():
    _ensure_app()
    w = _TestWindow()
    assert w.action_button.text() == "开始"
    assert w.action_button.isEnabled() is True
    assert w.stop_button.isEnabled() is False
    assert w.progress_bar.value() == 0
    assert w.progress_label.text() == "尚未开始"


def test_structured_progress_updates_widgets():
    _ensure_app()
    w = _TestWindow()
    w.task_feedback.set_progress(
        3,
        total=10,
        phase="评论采集",
        activity="正在请求评论",
        stop_reason="等待 API 响应",
    )
    _ensure_app().processEvents()

    assert w.progress_bar.value() == 30
    assert "3/10（30%）" in w.progress_label.text()
    assert "正在请求评论" in w.progress_label.text()


def test_delayed_stop_reports_current_wait_reason(monkeypatch):
    _ensure_app()
    w = _TestWindow()

    class AliveThread:
        @staticmethod
        def is_alive():
            return True

    monkeypatch.setattr("src.ui.base.QMessageBox.show", lambda _self: None)
    w.worker_thread = AliveThread()
    w._stop_requested_at = time.monotonic() - 1.0
    w.task_feedback.set_activity("正在写入缓存", stop_reason="正在等待本地 SQLite 事务提交完成。")
    w._check_stop_wait()

    assert "SQLite 事务提交" in w.log_text.toPlainText()
    assert w._stop_wait_box is not None
    assert "SQLite 事务提交" in w._stop_wait_box.informativeText()
    w._cleanup_stop_feedback()


def test_start_transitions_to_running():
    _ensure_app()
    w = _TestWindow()
    w._do_start()
    assert w.action_button.text() == "暂停"
    assert w.action_button.isEnabled() is True
    assert w.stop_button.isEnabled() is True
    w.stop()
    w.worker_thread.join(timeout=2)


def test_pause_resume_cycle():
    _ensure_app()
    w = _TestWindow()
    w._do_start()
    assert w.action_button.text() == "暂停"
    w._toggle_pause()
    assert w.action_button.text() == "继续"
    assert w.pause_event.is_set() is True
    w._toggle_pause()
    assert w.action_button.text() == "暂停"
    assert w.pause_event.is_set() is False
    w.stop()
    w.worker_thread.join(timeout=2)


def test_stop_from_running():
    _ensure_app()
    w = _TestWindow()
    w._do_start()
    assert w.stop_button.isEnabled() is True
    w.stop()
    assert w.stop_event.is_set() is True
    w.worker_thread.join(timeout=2)
    assert w.stop_event.is_set() is True


def test_stop_from_paused():
    _ensure_app()
    w = _TestWindow()
    w._do_start()
    w._toggle_pause()
    assert w.pause_event.is_set() is True
    w.stop()
    assert w.pause_event.is_set() is False
    assert w.stop_event.is_set() is True
    w.worker_thread.join(timeout=2)


def test_task_receives_pause_event():
    _ensure_app()
    w = _TestWindow()
    w._do_start()
    w.worker_thread.join(timeout=3)
    assert w.task_ran is True
    assert w.received_pause_event is not None


def test_collect_values():
    _ensure_app()
    w = _TestWindow()
    values = w.collect_values()
    assert values is not None
    assert values["text_field"] == "hello"
    assert values["limit_time"] == "是"


def test_field_visibility_binding():
    _ensure_app()
    w = _TestWindow()
    assert w.widgets["start_date"].isHidden() is False  # "是" → visible
    combo = w.widgets["limit_time"]
    combo.setCurrentText("否")
    assert w.widgets["start_date"].isHidden() is True   # "否" → hidden
    combo.setCurrentText("是")
    assert w.widgets["start_date"].isHidden() is False  # back to visible


# ---------------------------------------------------------------------------
# Test config dialog and config parameter flow
# ---------------------------------------------------------------------------

def test_config_dialog_creates():
    _ensure_app()
    params = [
        ConfigParam("search_page_timeout", "搜索页加载超时(毫秒)", kind="int", default=40000, minimum=10000, maximum=120000),
        ConfigParam("scroll_cooldown_min", "滚动等待最小(秒)", kind="float", default=3.0, minimum=0.5, maximum=30.0),
    ]
    dialog = ConfigDialog("测试", params)
    values = dialog.get_values()
    assert values["search_page_timeout"] == 40000
    assert abs(values["scroll_cooldown_min"] - 3.0) < 0.01
    dialog.close()


def test_config_dialog_bool_param():
    _ensure_app()
    params = [
        ConfigParam("trust_local", "信任本地判断", kind="bool", default=True),
    ]
    dialog = ConfigDialog("测试", params)
    values = dialog.get_values()
    assert values["trust_local"] is True
    dialog.close()


def test_config_dialog_current_values_applied():
    """current_values (from previous config) should be loaded into widgets."""
    _ensure_app()
    params = [
        ConfigParam("timeout", "超时", kind="int", default=30000, minimum=1000, maximum=120000),
    ]
    dialog = ConfigDialog("测试", params, current_values={"timeout": 99999})
    # Current value should be loaded, not the default
    values = dialog.get_values()
    assert values["timeout"] == 99999
    # Restore defaults should reset to param.default
    dialog._apply_defaults()
    values = dialog.get_values()
    assert values["timeout"] == 30000
    dialog.close()


def test_config_values_flow_to_task():
    """Verify that config values set via dialog are injected into run_task."""
    _ensure_app()
    w = _TestWindow()
    w.config_values = {"test_param": 42, "another_param": "hello"}
    w._do_start()
    w.worker_thread.join(timeout=3)
    assert w.task_ran is True
    assert w.received_config is not None
    assert w.received_config.get("test_param") == 42
    assert w.received_config.get("another_param") == "hello"


def test_pause_event_is_threading_event():
    _ensure_app()
    w = _TestWindow()
    assert isinstance(w.pause_event, threading.Event)
    assert w.pause_event.is_set() is False


# ---------------------------------------------------------------------------
# Verify all windows import correctly and have pause_event in run_task
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Test text_or_file widget — dual-mode input (direct text / TXT file)
# ---------------------------------------------------------------------------

class _TextOrFileWindow(SimpleToolWindow):
    """Minimal window with a text_or_file field for testing."""

    def __init__(self, required=True, default_text=""):
        super().__init__(
            "测试 text_or_file",
            [
                FieldSpec(
                    "data",
                    "数据输入",
                    kind="text_or_file",
                    required=required,
                    placeholder="测试占位",
                    default=default_text,
                ),
            ],
        )

    def run_task(self, values, log_callback, finish_callback, stop_event, pause_event):
        pass


def test_text_or_file_initial_state():
    """Default mode is direct input, text_edit visible, file_edit not visible (parent hidden)."""
    _ensure_app()
    w = _TextOrFileWindow()
    widget = w.widgets["data"]
    assert widget.mode_combo.currentText() == "直接输入"
    assert widget.text_edit.isHidden() is False
    # file_edit's parent file_row is hidden initially
    assert widget.file_edit.parent().isHidden() is True


def test_text_or_file_mode_switch_to_file():
    """Switching to TXT file mode hides text_edit, shows file_row (file_edit's parent)."""
    _ensure_app()
    w = _TextOrFileWindow()
    widget = w.widgets["data"]
    widget.mode_combo.setCurrentText("TXT 文件")
    assert widget.text_edit.isHidden() is True
    # file_edit's parent is file_row — it should now be visible
    assert widget.file_edit.parent().isHidden() is False


def test_text_or_file_mode_switch_back():
    """Switching back to direct mode shows text_edit, hides file_row again."""
    _ensure_app()
    w = _TextOrFileWindow()
    widget = w.widgets["data"]
    widget.mode_combo.setCurrentText("TXT 文件")
    widget.mode_combo.setCurrentText("直接输入")
    assert widget.text_edit.isHidden() is False
    assert widget.file_edit.parent().isHidden() is True


def test_text_or_file_default_text_is_applied():
    """Field default should be rendered into the direct-input editor."""
    _ensure_app()
    w = _TextOrFileWindow(default_text="line1\nline2")
    widget = w.widgets["data"]
    assert widget.text_edit.toPlainText() == "line1\nline2"


def test_text_or_file_collect_direct_mode():
    """collect_values returns text content in direct input mode."""
    _ensure_app()
    w = _TextOrFileWindow()
    widget = w.widgets["data"]
    widget.text_edit.setPlainText("  line1\nline2\n# comment\n  ")
    values = w.collect_values()
    assert values is not None
    assert values["data"] == "line1\nline2\n# comment"


def test_text_or_file_collect_file_mode(tmp_path):
    """collect_values reads and returns file content in TXT file mode."""
    _ensure_app()
    w = _TextOrFileWindow()
    widget = w.widgets["data"]
    test_file = tmp_path / "test_input.txt"
    test_file.write_text("url1\nurl2\nurl3", encoding="utf-8")
    widget.mode_combo.setCurrentText("TXT 文件")
    widget.file_edit.setText(str(test_file))
    values = w.collect_values()
    assert values is not None
    assert values["data"] == "url1\nurl2\nurl3"


def test_text_or_file_collect_empty_direct_mode():
    """Empty required field returns None from collect_values (window must be shown for isVisible)."""
    _ensure_app()
    w = _TextOrFileWindow(required=True)
    widget = w.widgets["data"]
    widget.text_edit.setPlainText("")
    w.setAttribute(Qt.WA_DontShowOnScreen, True)
    w.show()
    # Patch QMessageBox.warning to avoid blocking in headless tests
    from PyQt5.QtWidgets import QMessageBox
    original = QMessageBox.warning
    try:
        QMessageBox.warning = lambda *a, **kw: None
        values = w.collect_values()
    finally:
        QMessageBox.warning = original
        w.hide()
    assert values is None


def test_text_or_file_collect_missing_file_path():
    """File mode with empty path returns None."""
    _ensure_app()
    w = _TextOrFileWindow(required=True)
    widget = w.widgets["data"]
    widget.mode_combo.setCurrentText("TXT 文件")
    widget.file_edit.setText("")
    w.setAttribute(Qt.WA_DontShowOnScreen, True)
    w.show()
    from PyQt5.QtWidgets import QMessageBox
    original = QMessageBox.warning
    try:
        QMessageBox.warning = lambda *a, **kw: None
        values = w.collect_values()
    finally:
        QMessageBox.warning = original
        w.hide()
    assert values is None


def test_text_or_file_collect_bad_file():
    """File mode with non-existent file returns None."""
    _ensure_app()
    w = _TextOrFileWindow(required=True)
    widget = w.widgets["data"]
    widget.mode_combo.setCurrentText("TXT 文件")
    widget.file_edit.setText("/nonexistent/path/to/file.txt")
    w.setAttribute(Qt.WA_DontShowOnScreen, True)
    w.show()
    from PyQt5.QtWidgets import QMessageBox
    original = QMessageBox.warning
    try:
        QMessageBox.warning = lambda *a, **kw: None
        values = w.collect_values()
    finally:
        QMessageBox.warning = original
        w.hide()
    assert values is None


def test_text_to_tempfile_creates_file():
    """_text_to_tempfile writes text to a temp file and returns the path."""
    _ensure_app()
    w = _TextOrFileWindow()
    path = w._text_to_tempfile("hello\nworld", prefix="test_input")
    from pathlib import Path
    assert Path(path).exists()
    content = Path(path).read_text(encoding="utf-8")
    assert content == "hello\nworld"
    Path(path).unlink()


def test_text_or_file_placeholder():
    """Placeholder text is set on the text_edit widget."""
    _ensure_app()
    w = _TextOrFileWindow()
    widget = w.widgets["data"]
    assert widget.text_edit.placeholderText() == "测试占位"


def test_text_or_file_e2e_tiktok_profiles_flow():
    """E2E: direct input → collect_values → _text_to_tempfile → parse_profile_urls."""
    _ensure_app()
    from pathlib import Path
    from src.platforms.tiktok.profiles import parse_profile_urls

    w = _TextOrFileWindow()
    widget = w.widgets["data"]
    widget.text_edit.setPlainText("https://www.tiktok.com/@user1\nhttps://www.tiktok.com/@user2")
    values = w.collect_values()
    assert values is not None
    assert values["data"] == "https://www.tiktok.com/@user1\nhttps://www.tiktok.com/@user2"
    temp_path = w._text_to_tempfile(values["data"], prefix="test_tiktok")
    try:
        urls = parse_profile_urls(temp_path)
        assert len(urls) == 2, f"Expected 2 URLs, got {len(urls)}: {urls}"
        assert urls[0] == "https://www.tiktok.com/@user1"
        assert urls[1] == "https://www.tiktok.com/@user2"
    finally:
        Path(temp_path).unlink(missing_ok=True)


def test_text_or_file_e2e_tempfile_content_matches_input():
    """E2E: the temp file written by _text_to_tempfile contains the exact input text."""
    _ensure_app()
    from pathlib import Path

    w = _TextOrFileWindow()
    widget = w.widgets["data"]
    test_text = "https://x.com/user/status/123\nhttps://twitter.com/other/status/456"
    widget.text_edit.setPlainText(test_text)
    values = w.collect_values()
    assert values is not None
    temp_path = w._text_to_tempfile(values["data"], prefix="test_e2e")
    try:
        content = Path(temp_path).read_text(encoding="utf-8")
        assert content == test_text
        # Verify backend reading works with same encoding used by all parse functions
        with open(temp_path, "r", encoding="utf-8-sig") as f:
            lines = [line.strip() for line in f if line.strip() and not line.strip().startswith("#")]
        assert len(lines) == 2
        assert lines[0] == "https://x.com/user/status/123"
        assert lines[1] == "https://twitter.com/other/status/456"
    finally:
        Path(temp_path).unlink(missing_ok=True)


def test_all_windows_have_pause_event_signature():
    """Ensure all 19 window classes accept pause_event in run_task."""
    import inspect
    import importlib

    windows_modules = [
        ("src.platforms.youtube.windows", ["YouTubeKeywordWindow", "YouTubeProfilesWindow", "YouTubeContextWindow", "YouTubeChannelWorksWindow", "YouTubeCommentsWindow"]),
        ("src.platforms.tiktok.windows", ["TikTokKeywordWindow", "TikTokProfilesWindow", "TikTokProfileVideosWindow", "TikTokContextWindow", "TikTokCommentsWindow"]),
        ("src.platforms.x_twitter.windows", ["XKeywordWindow", "XProfilesWindow", "XContextWindow", "XTweetMetricsWindow", "XProfileTweetsWindow", "XCommentsWindow"]),
        ("src.platforms.instagram.windows", ["InstagramProfileWorksWindow"]),
        ("src.processing.windows", ["JudgeAIGCWindow", "XlsxMergeWindow", "KeywordCandidateValidatorWindow", "AnomalyDetectionWindow"]),
    ]

    total = 0
    for module_name, class_names in windows_modules:
        mod = importlib.import_module(module_name)
        for class_name in class_names:
            cls = getattr(mod, class_name)
            sig = inspect.signature(cls.run_task)
            params = list(sig.parameters.keys())
            assert "pause_event" in params, f"{class_name}.run_task missing pause_event parameter. Found: {params}"
            total += 1
    assert total == 21, f"Expected 21 classes, found {total}"
