from __future__ import annotations

from src.platforms.tiktok.video_details import (
    _detail_has_content,
    clean_video_input,
    parse_video_urls,
    resolve_video_url,
)


def test_clean_video_input_accepts_tiktok_video_and_share_hosts():
    assert clean_video_input("www.tiktok.com/@demo/video/123") == "https://www.tiktok.com/@demo/video/123"
    assert clean_video_input("https://vm.tiktok.com/ZS123/?foo=1") == "https://vm.tiktok.com/ZS123/?foo=1"
    assert clean_video_input("https://example.com/video/123") == ""


def test_parse_video_urls_skips_comments_and_deduplicates(tmp_path):
    source = tmp_path / "videos.txt"
    source.write_text(
        "# comment\nhttps://www.tiktok.com/@demo/video/123\nhttps://www.tiktok.com/@demo/video/123\nhttps://vt.tiktok.com/ZS456/\ninvalid\n",
        encoding="utf-8",
    )
    assert parse_video_urls(str(source)) == [
        "https://www.tiktok.com/@demo/video/123",
        "https://vt.tiktok.com/ZS456",
    ]


class _RedirectPage:
    def __init__(self):
        self.url = ""

    def goto(self, url, **_kwargs):
        assert url == "https://vm.tiktok.com/ZS123"
        self.url = "https://www.tiktok.com/@demo/video/123?is_from_webapp=1"


def test_resolve_video_url_expands_share_url():
    assert resolve_video_url(_RedirectPage(), "https://vm.tiktok.com/ZS123", 1000) == "https://www.tiktok.com/@demo/video/123"


def test_detail_has_content_requires_a_detail_field():
    assert _detail_has_content({"desc": "hello"}) is True
    assert _detail_has_content({"video_url": "https://www.tiktok.com/@demo/video/123"}) is False
