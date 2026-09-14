import unittest
from unittest.mock import MagicMock, patch

from src.platforms.youtube.keyword import (
    YouTubeClientPool,
    _api_call_with_rotation,
    execute_with_retry,
    is_transient_connection_error,
)


class TestYouTubeApiRetry(unittest.TestCase):
    def test_transient_connection_error_detection(self):
        self.assertTrue(is_transient_connection_error(ConnectionResetError(10054, "主机关闭了一个已有的连接")))
        self.assertTrue(is_transient_connection_error(TimeoutError("timed out")))
        self.assertFalse(is_transient_connection_error(ValueError("not network related")))

    def test_execute_with_retry_clears_stale_connections(self):
        connection = MagicMock()
        request = MagicMock()
        request.http.connections = {"youtube": connection}
        request.execute.side_effect = [ConnectionResetError(10054, "主机关闭了一个已有的连接"), {"ok": True}]

        with patch("src.platforms.youtube.keyword.interruptible_sleep", return_value=False):
            result = execute_with_retry(request)

        self.assertEqual(result, {"ok": True})
        connection.close.assert_called_once()
        self.assertEqual(request.http.connections, {})
        self.assertEqual(request.execute.call_count, 2)

    @patch("src.platforms.youtube.keyword.build")
    def test_api_call_with_rotation_refreshes_client_after_retry_exhaustion(self, mock_build):
        first_client = MagicMock()
        second_client = MagicMock()
        mock_build.side_effect = [first_client, second_client]

        pool = YouTubeClientPool(["key"])
        request = MagicMock()
        request.execute.side_effect = ConnectionResetError(10054, "主机关闭了一个已有的连接")
        ok_request = MagicMock()
        ok_request.execute.return_value = {"items": []}

        build_request = MagicMock(side_effect=[request, ok_request])
        with patch("src.platforms.youtube.keyword.interruptible_sleep", return_value=False):
            result = _api_call_with_rotation(pool, build_request, None)

        self.assertEqual(result, {"items": []})
        self.assertIs(pool.client, second_client)
        self.assertEqual(mock_build.call_count, 2)

    @patch("src.platforms.youtube.keyword.build")
    def test_next_client_does_not_skip_key_after_concurrent_rotation(self, mock_build):
        mock_build.side_effect = [MagicMock(), MagicMock(), MagicMock()]
        pool = YouTubeClientPool(["key-a", "key-b", "key-c"])
        pool.current_idx = 1

        self.assertTrue(pool.next_client(failed_idx=0))
        self.assertEqual(pool.current_idx, 1)


if __name__ == "__main__":
    unittest.main()
