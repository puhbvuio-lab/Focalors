from datetime import datetime
from pathlib import Path
import sys

project_root = Path(__file__).parent.parent
sys.path.append(str(project_root))

import src.platforms.tiktok.hashtag_author_works as hashtag_author_works
from src.platforms.tiktok.hashtag_author_works import (
    HASHTAG_AUTHOR_FIELDS,
    HASHTAG_VIDEO_FIELDS,
    _author_row_for_hashtag,
    _author_sheet_row_for_hashtag,
    _video_row_for_hashtag,
    collect_hashtag_seed_authors,
    normalize_hashtag_input,
    parse_hashtag_sources,
)
from src.platforms.tiktok.keyword_author_works import TikTokAuthorSeed
from src.platforms.tiktok.windows import TikTokHashtagAuthorWorksWindow
from src.studio.discovery import discover_tools
from src.studio.registry import TOOLS


def test_normalize_hashtag_inputs():
    source = normalize_hashtag_input("https://www.tiktok.com/tag/palworld?lang=en")
    assert source.label == "#palworld"
    assert source.url == "https://www.tiktok.com/tag/palworld"

    source = normalize_hashtag_input("palworld")
    assert source.label == "#palworld"
    assert source.url == "https://www.tiktok.com/tag/palworld"

    source = normalize_hashtag_input("#monster taming")
    assert source.label == "#monster taming"
    assert source.url == "https://www.tiktok.com/tag/monster%20taming"

    sources = parse_hashtag_sources(["palworld", "https://www.tiktok.com/tag/palworld", "https://www.tiktok.com/@notatag"], skip_invalid=True)
    assert len(sources) == 1


def test_hashtag_author_row_uses_topic_column():
    seed = TikTokAuthorSeed(
        profile_url="https://www.tiktok.com/@demo",
        author_name="Demo",
        author_id="@demo",
        keywords=["#palworld"],
        seed_links=["https://www.tiktok.com/@demo/video/1"],
    )
    row = _author_row_for_hashtag(
        seed,
        {"博主主页链接": "https://www.tiktok.com/@demo", "博主名称": "Demo", "博主ID": "@demo", "粉丝量": "100", "作者简介": "bio"},
        [
            {"desc": "first\nvideo", "video_url": "https://www.tiktok.com/@demo/video/2", "published_at": "2026-06-01 00:00:00"},
            {"desc": "old video", "video_url": "https://www.tiktok.com/@demo/video/3", "published_at": "2025-01-01 00:00:00"},
        ],
        True,
        datetime(2026, 6, 1),
        datetime(2026, 6, 30),
    )

    assert "搜索词" not in row
    assert row["话题"] == "#palworld"
    assert row["采集作品数"] == "2"
    assert row["时间窗口内作品数"] == "1"
    assert row["作品标题列表"] == "first video\nold video"


def test_hashtag_author_works_sheet_rows_use_topic_column():
    seed = TikTokAuthorSeed(
        profile_url="https://www.tiktok.com/@demo",
        author_name="Demo",
        author_id="@demo",
        keywords=["#palworld"],
        seed_links=["https://www.tiktok.com/@demo/video/1"],
    )
    profile = {"博主主页链接": "https://www.tiktok.com/@demo", "博主名称": "Demo", "博主ID": "@demo", "粉丝量": "100", "作者简介": "bio"}
    work = {
        "desc": "first\nvideo",
        "video_url": "https://www.tiktok.com/@demo/video/7000000000000000001",
        "published_at": "2026-06-01 00:00:00",
    }

    author_row = _author_sheet_row_for_hashtag(seed, profile, [work], True, datetime(2026, 6, 1), datetime(2026, 6, 30))
    video_row = _video_row_for_hashtag(1, seed, profile, work)

    assert list(author_row.keys()) == HASHTAG_AUTHOR_FIELDS
    assert list(video_row.keys()) == HASHTAG_VIDEO_FIELDS
    assert "搜索词" not in author_row
    assert "搜索词" not in video_row
    assert author_row["话题"] == "#palworld"
    assert video_row["话题"] == "#palworld"
    assert video_row["标题"] == "first video"


def test_hashtag_seed_prefilter_skips_obvious_out_of_window_video_id():
    old_timestamp = int(datetime(2020, 1, 1, 12, 0, 0).timestamp())
    old_video_id = str(old_timestamp << 32)
    old_video_url = f"https://www.tiktok.com/@demo/video/{old_video_id}"
    extract_calls = []

    originals = {
        "open_hashtag_page": hashtag_author_works.open_hashtag_page,
        "dynamic_search_scroll_limit": hashtag_author_works.dynamic_search_scroll_limit,
        "collect_visible_video_items": hashtag_author_works.collect_visible_video_items,
        "extract_video_row": hashtag_author_works.extract_video_row,
        "trigger_search_lazy_load": hashtag_author_works.trigger_search_lazy_load,
        "interruptible_sleep": hashtag_author_works.interruptible_sleep,
    }
    try:
        hashtag_author_works.open_hashtag_page = lambda *args, **kwargs: True
        hashtag_author_works.dynamic_search_scroll_limit = lambda *args, **kwargs: 1
        hashtag_author_works.collect_visible_video_items = lambda page, seen: [
            {"视频链接": old_video_url, "播放量": "", "博主主页链接": "https://www.tiktok.com/@demo"}
        ]
        hashtag_author_works.extract_video_row = lambda *args, **kwargs: extract_calls.append(args[2]) or {}
        hashtag_author_works.trigger_search_lazy_load = lambda *args, **kwargs: None
        hashtag_author_works.interruptible_sleep = lambda *args, **kwargs: False

        authors = collect_hashtag_seed_authors(
            object(),
            object(),
            parse_hashtag_sources(["palworld"]),
            datetime(2026, 6, 1),
            datetime(2026, 6, 30),
            True,
            lambda message: None,
            max_seed_works=10,
            max_authors=10,
            max_topic_scrolls=1,
        )
    finally:
        for name, value in originals.items():
            setattr(hashtag_author_works, name, value)

    assert authors == {}
    assert extract_calls == []


def test_hashtag_page_failure_skips_source():
    sources = parse_hashtag_sources(["palworld"])
    originals = {
        "open_hashtag_page": hashtag_author_works.open_hashtag_page,
        "dynamic_search_scroll_limit": hashtag_author_works.dynamic_search_scroll_limit,
        "collect_visible_video_items": hashtag_author_works.collect_visible_video_items,
    }
    try:
        hashtag_author_works.open_hashtag_page = lambda *args, **kwargs: False
        hashtag_author_works.dynamic_search_scroll_limit = lambda *args, **kwargs: 1
        hashtag_author_works.collect_visible_video_items = lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("should skip failed page"))

        authors = collect_hashtag_seed_authors(
            object(),
            object(),
            sources,
            None,
            None,
            False,
            lambda message: None,
            max_seed_works=10,
            max_authors=10,
            max_topic_scrolls=1,
        )
    finally:
        for name, value in originals.items():
            setattr(hashtag_author_works, name, value)

    assert authors == {}


def test_hashtag_seed_budget_applies_per_source():
    class Page:
        current_label = ""

    topic_page = Page()
    sources = parse_hashtag_sources(["first topic", "second topic"])
    extracted = []

    originals = {
        "open_hashtag_page": hashtag_author_works.open_hashtag_page,
        "dynamic_search_scroll_limit": hashtag_author_works.dynamic_search_scroll_limit,
        "collect_visible_video_items": hashtag_author_works.collect_visible_video_items,
        "extract_video_row": hashtag_author_works.extract_video_row,
        "trigger_search_lazy_load": hashtag_author_works.trigger_search_lazy_load,
        "interruptible_sleep": hashtag_author_works.interruptible_sleep,
    }

    def fake_open(page, source, *args, **kwargs):
        page.current_label = source.label
        return True

    def fake_items(page, seen):
        handle = page.current_label.replace("#", "").replace(" ", "")
        return [{"视频链接": f"https://www.tiktok.com/@{handle}/video/7600000000000000000", "播放量": "", "博主主页链接": f"https://www.tiktok.com/@{handle}"}]

    def fake_row(page, keyword, video_url, *args, **kwargs):
        extracted.append((keyword, video_url))
        handle = video_url.split("/@")[1].split("/")[0]
        return {"博主主页链接": f"https://www.tiktok.com/@{handle}", "博主名称": handle, "博主ID": f"@{handle}"}

    try:
        hashtag_author_works.open_hashtag_page = fake_open
        hashtag_author_works.dynamic_search_scroll_limit = lambda *args, **kwargs: 1
        hashtag_author_works.collect_visible_video_items = fake_items
        hashtag_author_works.extract_video_row = fake_row
        hashtag_author_works.trigger_search_lazy_load = lambda *args, **kwargs: None
        hashtag_author_works.interruptible_sleep = lambda *args, **kwargs: False

        authors = collect_hashtag_seed_authors(
            topic_page,
            object(),
            sources,
            None,
            None,
            False,
            lambda message: None,
            max_seed_works=1,
            max_authors=1,
            max_topic_scrolls=1,
        )
    finally:
        for name, value in originals.items():
            setattr(hashtag_author_works, name, value)

    assert [item[0] for item in extracted] == ["#first topic", "#second topic"]
    assert len(authors) == 2
    assert [seed.keywords for seed in authors.values()] == [["#first topic"], ["#second topic"]]


def test_hashtag_seed_cache_roundtrips_and_skips_completed_source():
    class MemoryCheckpoint:
        state = {}

        def get_state(self, key, default=None):
            return self.state.get(key, default)

        def set_state(self, key, value):
            self.state[key] = value

    source_ids = ["https://www.tiktok.com/tag/first", "https://www.tiktok.com/tag/second"]
    seed = TikTokAuthorSeed(
        profile_url="https://www.tiktok.com/@first",
        author_name="First",
        author_id="@first",
        keywords=["#first"],
        seed_links=["https://www.tiktok.com/@first/video/1"],
    )
    checkpoint = MemoryCheckpoint()
    hashtag_author_works.save_seed_author_cache(checkpoint, source_ids, {"first": seed}, {source_ids[0]})

    authors, completed_sources = hashtag_author_works.load_seed_author_cache(checkpoint, source_ids)

    assert list(authors) == ["first"]
    assert authors["first"].profile_url == "https://www.tiktok.com/@first"
    assert completed_sources == {source_ids[0]}


def test_hashtag_cached_authors_count_toward_source_author_limit():
    sources = parse_hashtag_sources(["cached topic"])
    source = sources[0]
    cached_seed = TikTokAuthorSeed(
        profile_url="https://www.tiktok.com/@cached",
        author_id="@cached",
        keywords=[source.label],
        seed_links=["https://www.tiktok.com/@cached/video/1"],
    )
    initial_authors = {
        f"{source.url.lower()}|cached": cached_seed,
    }

    originals = {
        "open_hashtag_page": hashtag_author_works.open_hashtag_page,
        "dynamic_search_scroll_limit": hashtag_author_works.dynamic_search_scroll_limit,
        "collect_visible_video_items": hashtag_author_works.collect_visible_video_items,
    }
    try:
        hashtag_author_works.open_hashtag_page = lambda *args, **kwargs: True
        hashtag_author_works.dynamic_search_scroll_limit = lambda *args, **kwargs: 1
        hashtag_author_works.collect_visible_video_items = lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("cached author already fills source limit"))

        authors = collect_hashtag_seed_authors(
            object(),
            object(),
            sources,
            None,
            None,
            False,
            lambda message: None,
            max_seed_works=10,
            max_authors=1,
            max_topic_scrolls=1,
            initial_authors=initial_authors,
        )
    finally:
        for name, value in originals.items():
            setattr(hashtag_author_works, name, value)

    assert authors == initial_authors


def test_hashtag_author_works_tool_registered():
    window = TikTokHashtagAuthorWorksWindow.__new__(TikTokHashtagAuthorWorksWindow)
    defaults = {param.key: param.default for param in window.tool_config_params()}
    assert defaults["max_profile_works_per_author"] == 50
    assert defaults["max_authors"] == 300
    assert defaults["max_topic_scrolls"] == 360

    static_ids = {tool.tool_id for tool in TOOLS}
    assert "tiktok_hashtag_author_works" in static_ids

    discovered, errors = discover_tools()
    discovered_ids = {tool.tool_id for tool in discovered}
    assert "tiktok_hashtag_author_works" in discovered_ids
    assert not [error for error in errors if "tiktok_hashtag_author_works" in error]


if __name__ == "__main__":
    test_normalize_hashtag_inputs()
    test_hashtag_author_row_uses_topic_column()
    test_hashtag_author_works_sheet_rows_use_topic_column()
    test_hashtag_seed_prefilter_skips_obvious_out_of_window_video_id()
    test_hashtag_page_failure_skips_source()
    test_hashtag_seed_budget_applies_per_source()
    test_hashtag_seed_cache_roundtrips_and_skips_completed_source()
    test_hashtag_cached_authors_count_toward_source_author_limit()
    test_hashtag_author_works_tool_registered()
    print("tiktok hashtag author works tests passed")
