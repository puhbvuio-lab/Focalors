from pathlib import Path
import sys
from datetime import datetime
import json

project_root = Path(__file__).parent.parent
sys.path.append(str(project_root))

import src.platforms.tiktok.keyword_author_works as tiktok_author_works
from src.platforms.x_twitter.keyword_author_works import (
    build_author_row as build_x_author_row,
    merge_seed_author as merge_x_seed_author,
    resolve_profile_work_limit as resolve_x_profile_work_limit,
)
from src.platforms.tiktok.keyword_author_works import (
    AUTHOR_FIELDS as TIKTOK_AUTHOR_FIELDS,
    VIDEO_FIELDS as TIKTOK_VIDEO_FIELDS,
    build_author_sheet_row as build_tiktok_author_sheet_row,
    build_author_row as build_tiktok_author_row,
    build_video_row as build_tiktok_video_row,
    merge_seed_author as merge_tiktok_seed_author,
    resolve_profile_work_limit as resolve_tiktok_profile_work_limit,
)
from src.platforms.tiktok.profile_videos import collect_visible_video_links
from src.platforms.x_twitter.windows import XKeywordAuthorWorksWindow
from src.platforms.tiktok.windows import TikTokKeywordAuthorWorksWindow
from src.studio.discovery import discover_tools
from src.studio.registry import TOOLS


def test_x_author_seed_dedup_and_row_aggregation():
    authors = {}
    merge_x_seed_author(
        authors,
        "ai",
        "https://x.com/user/status/1",
        {"profile_url": "https://x.com/User", "account_id": "User", "author_name": "User Name"},
    )
    merge_x_seed_author(
        authors,
        "animation",
        "https://x.com/user/status/2",
        {"profile_url": "https://twitter.com/user?ref=search", "account_id": "user", "author_name": ""},
    )

    assert len(authors) == 1
    seed = next(iter(authors.values()))
    assert seed.keywords == ["ai", "animation"]
    assert len(seed.seed_links) == 2

    row = build_x_author_row(
        seed,
        {"作者主页链接": "https://x.com/user", "作者的名称": "User Name", "账号ID": "user", "粉丝数": "100", "简介": "bio"},
        [
            {"content": "first\npost", "url": "https://x.com/user/status/3", "published_at": "2026-06-01 00:00:00"},
            {"content": "second post", "url": "https://x.com/user/status/4", "published_at": "2026-06-02 00:00:00"},
            {"content": "outside post", "url": "https://x.com/user/status/5", "published_at": "2025-12-31 00:00:00"},
        ],
        True,
        datetime(2026, 6, 1),
        datetime(2026, 6, 30),
    )

    assert row["命中作品数"] == "2"
    assert row["采集作品数"] == "3"
    assert row["时间窗口内作品数"] == "2"
    assert row["作品标题列表"] == "first post\nsecond post\noutside post"
    assert row["作品链接列表"] == "https://x.com/user/status/3\nhttps://x.com/user/status/4\nhttps://x.com/user/status/5"


def test_tiktok_author_seed_dedup_and_row_aggregation():
    authors = {}
    merge_tiktok_seed_author(
        authors,
        "ai",
        "https://www.tiktok.com/@demo/video/1",
        {"博主主页链接": "https://www.tiktok.com/@Demo", "博主名称": "Demo", "博主ID": "@Demo", "粉丝量": "123", "作者简介": "bio"},
    )
    merge_tiktok_seed_author(
        authors,
        "animation",
        "https://www.tiktok.com/@demo/video/2",
        {"博主主页链接": "https://www.tiktok.com/@demo?lang=en", "博主名称": "", "博主ID": "@demo"},
    )

    assert len(authors) == 1
    seed = next(iter(authors.values()))
    assert seed.keywords == ["ai", "animation"]
    assert len(seed.seed_links) == 2

    row = build_tiktok_author_row(
        seed,
        {"博主主页链接": "https://www.tiktok.com/@demo", "博主名称": "Demo", "博主ID": "@demo", "粉丝量": "123", "作者简介": "bio"},
        [
            {"desc": "first\nvideo", "video_url": "https://www.tiktok.com/@demo/video/3", "published_at": "2026-06-01 00:00:00"},
            {"desc": "second video", "video_url": "https://www.tiktok.com/@demo/video/4", "published_at": "2026-06-02 00:00:00"},
            {"desc": "older video", "video_url": "https://www.tiktok.com/@demo/video/5", "published_at": "2025-12-31 00:00:00"},
            {"desc": "unknown date", "video_url": "https://www.tiktok.com/@demo/video/6", "published_at": ""},
        ],
        True,
        datetime(2026, 6, 1),
        datetime(2026, 6, 30),
    )

    assert row["命中作品数"] == "2"
    assert row["采集作品数"] == "4"
    assert row["时间窗口内作品数"] == "2"
    assert row["作品标题列表"] == "first video\nsecond video\nolder video\nunknown date"
    assert row["作品链接列表"] == "https://www.tiktok.com/@demo/video/3\nhttps://www.tiktok.com/@demo/video/4\nhttps://www.tiktok.com/@demo/video/5\nhttps://www.tiktok.com/@demo/video/6"


def test_tiktok_author_works_sheet_rows_are_split():
    seed = tiktok_author_works.TikTokAuthorSeed(
        profile_url="https://www.tiktok.com/@demo",
        author_name="Demo",
        author_id="@demo",
        followers="123",
        bio="bio",
        keywords=["monster games"],
        seed_links=["https://www.tiktok.com/@demo/video/1"],
    )
    profile = {"博主主页链接": "https://www.tiktok.com/@demo", "博主名称": "Demo", "博主ID": "@demo", "粉丝量": "123", "作者简介": "bio"}
    works = [
        {
            "desc": "first\nvideo",
            "video_url": "https://www.tiktok.com/@demo/video/7000000000000000001",
            "published_at": "2026-06-01 00:00:00",
            "likes": "10",
            "comments": "2",
            "collects": "3",
            "shares": "4",
        }
    ]

    author_row = build_tiktok_author_sheet_row(seed, profile, works, True, datetime(2026, 6, 1), datetime(2026, 6, 30))
    video_row = build_tiktok_video_row(1, seed, profile, works[0])

    assert list(author_row.keys()) == TIKTOK_AUTHOR_FIELDS
    assert "作品标题列表" not in author_row
    assert author_row["搜索词"] == "monster games"
    assert author_row["采集作品数"] == "1"
    assert list(video_row.keys()) == TIKTOK_VIDEO_FIELDS
    assert video_row["作者主页链接"] == "https://www.tiktok.com/@demo"
    assert video_row["标题"] == "first video"
    assert video_row["编号"] == "7000000000000000001"


def test_keyword_author_works_default_profile_limit_is_50():
    x_window = XKeywordAuthorWorksWindow.__new__(XKeywordAuthorWorksWindow)
    tiktok_window = TikTokKeywordAuthorWorksWindow.__new__(TikTokKeywordAuthorWorksWindow)

    x_defaults = {param.key: param.default for param in x_window.tool_config_params()}
    tiktok_defaults = {param.key: param.default for param in tiktok_window.tool_config_params()}

    assert x_defaults["max_profile_works_per_author"] == 50
    assert tiktok_defaults["max_profile_works_per_author"] == 50


def test_quick_mode_forces_latest_50_profile_works():
    config = {"max_profile_works_per_author": 500}

    assert resolve_x_profile_work_limit(config, "是") == 50
    assert resolve_tiktok_profile_work_limit(config, "是") == 50
    assert resolve_x_profile_work_limit(config, "否") == 500
    assert resolve_tiktok_profile_work_limit(config, "否") == 500


def test_tiktok_seed_prefilter_skips_obvious_out_of_window_video_id():
    old_timestamp = int(datetime(2020, 1, 1, 12, 0, 0).timestamp())
    old_video_id = str(old_timestamp << 32)
    old_video_url = f"https://www.tiktok.com/@demo/video/{old_video_id}"
    extract_calls = []

    originals = {
        "open_search_page": tiktok_author_works.open_search_page,
        "dynamic_search_scroll_limit": tiktok_author_works.dynamic_search_scroll_limit,
        "collect_visible_video_items": tiktok_author_works.collect_visible_video_items,
        "extract_video_row": tiktok_author_works.extract_video_row,
        "trigger_search_lazy_load": tiktok_author_works.trigger_search_lazy_load,
        "interruptible_sleep": tiktok_author_works.interruptible_sleep,
    }
    try:
        tiktok_author_works.open_search_page = lambda *args, **kwargs: None
        tiktok_author_works.dynamic_search_scroll_limit = lambda *args, **kwargs: 1
        tiktok_author_works.collect_visible_video_items = lambda page, seen: [
            {"视频链接": old_video_url, "播放量": "", "博主主页链接": "https://www.tiktok.com/@demo"}
        ]
        tiktok_author_works.extract_video_row = lambda *args, **kwargs: extract_calls.append(args[2]) or {}
        tiktok_author_works.trigger_search_lazy_load = lambda *args, **kwargs: None
        tiktok_author_works.interruptible_sleep = lambda *args, **kwargs: False

        authors = tiktok_author_works.collect_seed_authors(
            object(),
            object(),
            ["ai"],
            datetime(2026, 6, 1),
            datetime(2026, 6, 30),
            True,
            lambda message: None,
            max_seed_works=10,
            max_authors=10,
            max_search_scrolls=1,
        )
    finally:
        for name, value in originals.items():
            setattr(tiktok_author_works, name, value)

    assert authors == {}
    assert extract_calls == []


class FakeTikTokProfilePage:
    url = "https://www.tiktok.com/@demo"

    def evaluate(self, script):
        if "querySelectorAll" in script:
            return ["/@demo/video/7000000000000000001"]
        return json.dumps(
            {
                "universal": {
                    "ItemModule": {
                        "7000000000000000002": {
                            "id": "7000000000000000002",
                            "desc": "state video",
                            "createTime": 1760000000,
                            "author": {"uniqueId": "demo"},
                        },
                        "7000000000000000003": {
                            "id": "7000000000000000003",
                            "desc": "other author",
                            "createTime": 1760000000,
                            "author": {"uniqueId": "other"},
                        },
                    }
                }
            }
        )

    def content(self):
        return ""


def test_tiktok_profile_video_links_use_dom_and_state_fallbacks():
    seen = set()
    links = collect_visible_video_links(FakeTikTokProfilePage(), seen)

    assert links == [
        "https://www.tiktok.com/@demo/video/7000000000000000001",
        "https://www.tiktok.com/@demo/video/7000000000000000002",
    ]


def test_keyword_author_works_tools_registered():
    static_ids = {tool.tool_id for tool in TOOLS}
    assert "x_keyword_author_works" in static_ids
    assert "tiktok_keyword_author_works" in static_ids

    discovered, errors = discover_tools()
    discovered_ids = {tool.tool_id for tool in discovered}
    assert "x_keyword_author_works" in discovered_ids
    assert "tiktok_keyword_author_works" in discovered_ids
    assert not [error for error in errors if "keyword_author_works" in error]


if __name__ == "__main__":
    test_x_author_seed_dedup_and_row_aggregation()
    test_tiktok_author_seed_dedup_and_row_aggregation()
    test_tiktok_author_works_sheet_rows_are_split()
    test_keyword_author_works_default_profile_limit_is_50()
    test_quick_mode_forces_latest_50_profile_works()
    test_tiktok_seed_prefilter_skips_obvious_out_of_window_video_id()
    test_tiktok_profile_video_links_use_dom_and_state_fallbacks()
    test_keyword_author_works_tools_registered()
    print("keyword author works tests passed")
