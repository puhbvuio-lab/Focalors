import unittest
from unittest.mock import MagicMock, patch
import os
import tempfile

from src.platforms.youtube.comments import (
    extract_video_id,
    format_youtube_duration,
    parse_video_entries,
    fetch_video_metrics,
)

class TestYouTubeMetrics(unittest.TestCase):

    def test_extract_video_id(self):
        self.assertEqual(extract_video_id("https://www.youtube.com/watch?v=dQw4w9WgXcQ"), "dQw4w9WgXcQ")
        self.assertEqual(extract_video_id("https://youtu.be/dQw4w9WgXcQ"), "dQw4w9WgXcQ")
        self.assertEqual(extract_video_id("https://www.youtube.com/shorts/dQw4w9WgXcQ"), "dQw4w9WgXcQ")
        self.assertEqual(extract_video_id("https://www.youtube.com/embed/dQw4w9WgXcQ"), "dQw4w9WgXcQ")
        self.assertEqual(extract_video_id("dQw4w9WgXcQ"), "") # invalid url format

    def test_format_youtube_duration(self):
        self.assertEqual(format_youtube_duration("PT1H2M10S"), "01:02:10")
        self.assertEqual(format_youtube_duration("PT4M1S"), "00:04:01")
        self.assertEqual(format_youtube_duration("PT3S"), "00:00:03")
        self.assertEqual(format_youtube_duration("PT1H10S"), "01:00:10")
        self.assertEqual(format_youtube_duration("P1D"), "24:00:00")

    def test_parse_video_entries(self):
        with tempfile.NamedTemporaryFile(mode='w', delete=False, encoding='utf-8') as f:
            f.write("https://www.youtube.com/watch?v=A\nhttps://youtu.be/B\n# comment\nhttps://www.youtube.com/watch?v=A")
            temp_path = f.name
        
        try:
            entries = parse_video_entries(temp_path)
            self.assertEqual(len(entries), 2)
            self.assertEqual(entries[0]["视频ID"], "A")
            self.assertEqual(entries[1]["视频ID"], "B")
            self.assertEqual(entries[0]["有效行数"], 3)
            self.assertEqual(entries[0]["重复行数"], 1)
        finally:
            os.remove(temp_path)

    @patch("src.platforms.youtube.comments.execute_with_retry")
    def test_fetch_video_metrics(self, mock_execute):
        mock_youtube = MagicMock()
        mock_youtube.client.videos.return_value.list.return_value = "mock_request"
        mock_execute.return_value = {
            "items": [
                {
                    "id": "vid1",
                    "snippet": {
                        "title": "Test Video 1",
                        "channelTitle": "Test Channel",
                        "channelId": "UC123456",
                        "publishedAt": "2023-01-01T12:00:00Z",
                        "description": "Short description"
                    },
                    "statistics": {
                        "viewCount": "1000",
                        "likeCount": "100",
                        "commentCount": "10"
                     },
                     "contentDetails": {
                         "duration": "PT1M15S"
                     }
                }
            ]
        }
        
        metrics = fetch_video_metrics(mock_youtube, ["vid1"])
        self.assertIn("vid1", metrics)
        self.assertEqual(metrics["vid1"]["标题"], "Test Video 1")
        self.assertEqual(metrics["vid1"]["频道名称"], "Test Channel")
        self.assertEqual(metrics["vid1"]["频道ID"], "UC123456")
        self.assertEqual(metrics["vid1"]["发布日期"], "2023-01-01 12:00:00")
        self.assertEqual(metrics["vid1"]["视频时长"], "00:01:15")
        self.assertEqual(metrics["vid1"]["视频简介"], "Short description")
        self.assertEqual(metrics["vid1"]["播放量"], "1000")
        self.assertEqual(metrics["vid1"]["点赞数"], "100")
        self.assertEqual(metrics["vid1"]["评论数"], "10")

    def test_format_youtube_datetime(self):
        from src.platforms.youtube.comments import format_youtube_datetime
        self.assertEqual(format_youtube_datetime("2023-11-20T12:34:56Z"), "2023-11-20 12:34:56")
        self.assertEqual(format_youtube_datetime("2023-11-20 12:34:56"), "2023-11-20 12:34:56")
        self.assertEqual(format_youtube_datetime("2023-11-20"), "2023-11-20")
        self.assertEqual(format_youtube_datetime(""), "")

    def test_build_video_url(self):
        from src.platforms.youtube.comments import build_video_url
        self.assertEqual(build_video_url("vid123", "Shorts"), "https://www.youtube.com/shorts/vid123")
        self.assertEqual(build_video_url("vid123", "普通视频"), "https://www.youtube.com/watch?v=vid123")
        self.assertEqual(build_video_url("vid123", "未知"), "https://www.youtube.com/watch?v=vid123")
        self.assertEqual(build_video_url("vid123", "已删除"), "https://www.youtube.com/watch?v=vid123")
        self.assertEqual(build_video_url("", "Shorts"), "")

    def test_safe_filename_part(self):
        from src.platforms.youtube.keyword import safe_filename_part
        self.assertEqual(safe_filename_part("hello/world?"), "helloworld")
        self.assertEqual(safe_filename_part("test  spaces"), "test_spaces")
        self.assertEqual(safe_filename_part(""), "keyword")

    def test_keyword_duration_format(self):
        from src.platforms.youtube.keyword import format_youtube_duration
        self.assertEqual(format_youtube_duration("PT1H2M10S"), "01:02:10")
        self.assertEqual(format_youtube_duration("PT4M1S"), "00:04:01")
        self.assertEqual(format_youtube_duration("PT3S"), "00:00:03")

if __name__ == '__main__':
    unittest.main()


