import inspect
import json
import os
import threading
import unittest
from contextlib import contextmanager
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from unittest.mock import patch

from src.core import browser


@contextmanager
def serve_json_version(status: int, body: str, content_type: str = "application/json"):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path != "/json/version":
                self.send_response(404)
                self.end_headers()
                return
            payload = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, format, *args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


class TestBrowserLifecycle(unittest.TestCase):
    def setUp(self):
        browser._chrome_processes.clear()
        browser._managed_browser_processes.clear()

    def tearDown(self):
        browser._chrome_processes.clear()
        browser._managed_browser_processes.clear()

    def test_plain_http_200_is_not_cdp(self):
        with serve_json_version(200, "ok", content_type="text/plain") as base_url:
            self.assertFalse(browser.is_valid_cdp_endpoint(base_url))

    def test_http_400_is_not_cdp(self):
        with serve_json_version(400, json.dumps({"error": "bad request"})) as base_url:
            self.assertFalse(browser.is_valid_cdp_endpoint(base_url))

    def test_valid_cdp_json_is_recognized(self):
        payload = json.dumps(
            {
                "Browser": "Chrome/126.0.0.0",
                "webSocketDebuggerUrl": "ws://127.0.0.1/devtools/browser/test",
            }
        )
        with serve_json_version(200, payload) as base_url:
            self.assertTrue(browser.is_valid_cdp_endpoint(base_url))

    def test_remote_cdp_failure_does_not_launch_or_kill_local_browser(self):
        with (
            patch("src.core.browser.is_valid_cdp_endpoint", return_value=False),
            patch("src.core.browser.launch_chrome_for_cdp") as mock_launch,
            patch("src.core.browser._kill_chrome_on_port") as mock_kill,
        ):
            with self.assertRaisesRegex(RuntimeError, "Remote CDP endpoint is unavailable"):
                browser.ensure_chrome_for_cdp("http://198.51.100.20:9222", browser=browser.BROWSER_CHROME)

        mock_launch.assert_not_called()
        mock_kill.assert_not_called()

    def test_launch_rejects_remote_cdp_target(self):
        with patch("src.core.browser.subprocess.Popen") as mock_popen:
            with self.assertRaisesRegex(ValueError, "remote CDP host"):
                browser.launch_chrome_for_cdp("http://198.51.100.20:9222")
        mock_popen.assert_not_called()

    def test_all_loopback_spellings_are_local(self):
        for cdp_url in ("http://127.0.0.1:9222", "http://[::1]:9222"):
            with self.subTest(cdp_url=cdp_url):
                with (
                    patch("src.core.browser.is_valid_cdp_endpoint", return_value=True),
                    patch("src.core.browser._ensure_cdp_page_target", return_value=True),
                    patch("src.core.browser.launch_chrome_for_cdp") as mock_launch,
                    patch("src.core.browser._kill_chrome_on_port") as mock_kill,
                ):
                    self.assertFalse(browser.ensure_chrome_for_cdp(cdp_url, browser=browser.BROWSER_CHROME))
                    mock_launch.assert_not_called()
                    mock_kill.assert_not_called()

    def test_local_unmanaged_cdp_with_page_does_not_terminate(self):
        with (
            patch("src.core.browser.is_valid_cdp_endpoint", return_value=True),
            patch("src.core.browser._ensure_cdp_page_target", return_value=True),
            patch("src.core.browser._kill_chrome_on_port") as mock_kill,
            patch("src.core.browser.launch_chrome_for_cdp") as mock_launch,
        ):
            launched = browser.ensure_chrome_for_cdp("9222", browser=browser.BROWSER_CHROME)

        self.assertFalse(launched)
        mock_kill.assert_not_called()
        mock_launch.assert_not_called()

    def test_reachable_local_cdp_without_page_does_not_restart_unmanaged_browser(self):
        with (
            patch("src.core.browser.is_valid_cdp_endpoint", return_value=True),
            patch("src.core.browser._ensure_cdp_page_target", return_value=False),
            patch("src.core.browser._get_managed_browser_process", return_value=None),
            patch("src.core.browser._kill_chrome_on_port") as mock_kill,
            patch("src.core.browser.launch_chrome_for_cdp") as mock_launch,
        ):
            with self.assertRaisesRegex(RuntimeError, "not managed by this app"):
                browser.ensure_chrome_for_cdp("9222", browser=browser.BROWSER_CHROME)

        mock_kill.assert_not_called()
        mock_launch.assert_not_called()

    def test_occupied_non_cdp_local_port_with_unmanaged_browser_raises_clear_error(self):
        with (
            patch("src.core.browser.is_valid_cdp_endpoint", return_value=False),
            patch("src.core.browser.is_tcp_port_open", return_value=True),
            patch("src.core.browser._get_managed_browser_process", return_value=None),
            patch("src.core.browser._kill_chrome_on_port") as mock_kill,
            patch("src.core.browser.launch_chrome_for_cdp") as mock_launch,
        ):
            with self.assertRaisesRegex(RuntimeError, "refusing to terminate"):
                browser.ensure_chrome_for_cdp("9222", browser=browser.BROWSER_CHROME)

        mock_kill.assert_not_called()
        mock_launch.assert_not_called()

    def test_managed_pid_can_be_terminated(self):
        pid = 4567
        record = browser.ManagedBrowserProcess(
            pid=pid,
            browser_name=browser.BROWSER_CHROME,
            host="localhost",
            port=9222,
            user_data_dir="C:/managed",
            launched_by_app=True,
            owner_pid=os.getpid(),
            started_at=datetime.now(timezone.utc),
        )
        browser._managed_browser_processes[pid] = record

        with (
            patch("src.core.browser._is_process_alive", return_value=True),
            patch("src.core.browser._process_image_name", return_value="chrome.exe"),
            patch("src.core.browser.subprocess.run", return_value=SimpleNamespace(returncode=0)) as mock_run,
        ):
            self.assertTrue(browser._terminate_managed_browser_pid(pid, force=True))

        self.assertNotIn(pid, browser._managed_browser_processes)
        self.assertEqual(mock_run.call_args.args[0], ["taskkill", "/PID", str(pid), "/T", "/F"])

    def test_missing_managed_pid_is_forgotten_without_taskkill(self):
        pid = 4568
        browser._managed_browser_processes[pid] = browser.ManagedBrowserProcess(
            pid=pid,
            browser_name=browser.BROWSER_CHROME,
            host="localhost",
            port=9222,
            user_data_dir="C:/managed",
            launched_by_app=True,
            owner_pid=os.getpid(),
            started_at=datetime.now(timezone.utc),
        )
        with (
            patch("src.core.browser._is_process_alive", return_value=False),
            patch("src.core.browser.subprocess.run") as mock_run,
        ):
            self.assertFalse(browser._terminate_managed_browser_pid(pid, force=True))
        mock_run.assert_not_called()
        self.assertNotIn(pid, browser._managed_browser_processes)

    def test_image_mismatch_refuses_to_terminate_pid(self):
        pid = 4569
        browser._managed_browser_processes[pid] = browser.ManagedBrowserProcess(
            pid=pid,
            browser_name=browser.BROWSER_CHROME,
            host="localhost",
            port=9222,
            user_data_dir="C:/managed",
            launched_by_app=True,
            owner_pid=os.getpid(),
            started_at=datetime.now(timezone.utc),
        )
        with (
            patch("src.core.browser._is_process_alive", return_value=True),
            patch("src.core.browser._process_image_name", return_value="notepad.exe"),
            patch("src.core.browser.subprocess.run") as mock_run,
        ):
            self.assertFalse(browser._terminate_managed_browser_pid(pid, force=True))
        mock_run.assert_not_called()
        self.assertIn(pid, browser._managed_browser_processes)

    def test_termination_failure_does_not_fall_back_to_image_kill(self):
        pid = 4570
        browser._managed_browser_processes[pid] = browser.ManagedBrowserProcess(
            pid=pid,
            browser_name=browser.BROWSER_CHROME,
            host="localhost",
            port=9222,
            user_data_dir="C:/managed",
            launched_by_app=True,
            owner_pid=os.getpid(),
            started_at=datetime.now(timezone.utc),
        )
        with (
            patch("src.core.browser._is_process_alive", side_effect=[True, True]),
            patch("src.core.browser._process_image_name", return_value="chrome.exe"),
            patch("src.core.browser.subprocess.run", return_value=SimpleNamespace(returncode=1)) as mock_run,
        ):
            self.assertFalse(browser._terminate_managed_browser_pid(pid, force=True))
        self.assertEqual(mock_run.call_args.args[0], ["taskkill", "/PID", str(pid), "/T", "/F"])
        self.assertNotIn("/IM", mock_run.call_args.args[0])
        self.assertIn(pid, browser._managed_browser_processes)

    def test_launch_failure_never_triggers_process_cleanup(self):
        with (
            patch("src.core.browser.is_valid_cdp_endpoint", return_value=False),
            patch("src.core.browser.is_tcp_port_open", return_value=False),
            patch("src.core.browser.launch_chrome_for_cdp", side_effect=OSError("launch failed")),
            patch("src.core.browser._kill_chrome_on_port") as mock_kill,
        ):
            with self.assertRaisesRegex(OSError, "launch failed"):
                browser.ensure_chrome_for_cdp("9222", browser=browser.BROWSER_CHROME)
        mock_kill.assert_not_called()

    def test_production_browser_module_has_no_image_level_taskkill(self):
        source = inspect.getsource(browser)
        self.assertNotIn('"/IM"', source)
        self.assertNotIn("'/IM'", source)


if __name__ == "__main__":
    unittest.main(verbosity=2)
