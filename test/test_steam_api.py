from __future__ import annotations

from src.platforms.steam import api as steam_api
from src.platforms.steam.api import (
    APP_FIELDS,
    NEWS_FIELDS,
    PLAYER_ACHIEVEMENT_FIELDS,
    PLAYER_BADGE_FIELDS,
    PLAYER_FRIEND_FIELDS,
    PLAYER_LIBRARY_FIELDS,
    PLAYER_PROFILE_FIELDS,
    PLAYER_RECENT_FIELDS,
    REVIEW_MIN_TIMEOUT,
    REVIEW_PAGE_MAX_RETRIES,
    REVIEW_FIELDS,
    REVIEW_FULL_HISTORY_DAYS,
    fetch_reviews,
    normalize_keywords,
    parse_app_ids,
    parse_steam_ids,
    player_contexts_from_review_rows,
)
from src.studio.discovery import discover_tools
from src.studio.registry import TOOLS


def test_parse_steam_app_ids_from_numbers_and_urls():
    values = """
    1623730
    https://store.steampowered.com/app/294100/RimWorld/
    https://store.steampowered.com/app/1623730/Palworld/
    not-a-steam-link
    """
    assert parse_app_ids(values) == [1623730, 294100]


def test_normalize_keywords_deduplicates_case_and_spaces():
    values = "monster taming games\n Monster   Taming Games \nポケモンライク\n"
    assert normalize_keywords(values) == ["monster taming games", "ポケモンライク"]


def test_steam_api_tool_registered():
    static_ids = {tool.tool_id for tool in TOOLS}
    assert "steam_api_research" in static_ids
    assert "steam_player_profiles" in static_ids
    discovered, errors = discover_tools()
    discovered_ids = {tool.tool_id for tool in discovered}
    assert "steam_api_research" in discovered_ids
    assert "steam_player_profiles" in discovered_ids
    assert not [error for error in errors if "steam" in error.lower()]


def test_steam_api_extended_fields_present():
    assert "PC最低配置" in APP_FIELDS
    assert "DLC AppID列表" in APP_FIELDS
    assert "成就样本列表" in APP_FIELDS
    assert "总游玩小时" in REVIEW_FIELDS
    assert "开发者回复" in REVIEW_FIELDS
    assert "评论内容" in REVIEW_FIELDS
    assert "Feed类型" in NEWS_FIELDS


def test_parse_steam_ids_from_plain_values_and_profile_urls():
    values = """
    76561198000000000
    https://steamcommunity.com/profiles/76561198000000001/
    https://steamcommunity.com/profiles/76561198000000000/recommended/1623730/
    """
    assert parse_steam_ids(values) == ["76561198000000000", "76561198000000001"]


def test_player_contexts_from_review_rows_deduplicates_by_target_game():
    rows = [
        {
            "SteamID": "76561198000000000",
            "AppID": 1623730,
            "游戏名": "Palworld",
            "来源类型": "直接输入",
            "搜索词": "",
            "是否推荐": "是",
            "总游玩小时": "10.5",
            "近两周游玩小时": "2.0",
            "评价时游玩小时": "5.0",
            "最后游玩时间": "2026-01-01 00:00:00",
        },
        {"SteamID": "76561198000000000", "AppID": 1623730, "游戏名": "Palworld"},
    ]
    contexts = player_contexts_from_review_rows(rows)
    assert len(contexts) == 1
    assert contexts[0].steamid == "76561198000000000"
    assert contexts[0].target_appid == 1623730
    assert contexts[0].target_play_hours == "10.5"


def test_player_profile_fields_present():
    assert "Steam等级" in PLAYER_PROFILE_FIELDS
    assert "公开游戏总小时" in PLAYER_PROFILE_FIELDS
    assert "隐私/错误" in PLAYER_PROFILE_FIELDS
    assert "库游戏AppID" in PLAYER_LIBRARY_FIELDS
    assert "最近游戏AppID" in PLAYER_RECENT_FIELDS
    assert "是否解锁" in PLAYER_ACHIEVEMENT_FIELDS
    assert "徽章ID" in PLAYER_BADGE_FIELDS
    assert "好友SteamID" in PLAYER_FRIEND_FIELDS


def test_fetch_reviews_caps_to_steam_total(monkeypatch):
    calls = []
    responses = iter(
        [
            {"query_summary": {"total_reviews": 3, "total_positive": 2, "total_negative": 1}},
            {
                "query_summary": {"total_reviews": 3, "total_positive": 2, "total_negative": 1},
                "reviews": [
                    {"recommendationid": "1"},
                    {"recommendationid": "2"},
                    {"recommendationid": "3"},
                ],
                "cursor": "next",
            },
        ]
    )

    def fake_request_json(*args, **kwargs):
        calls.append(kwargs)
        return next(responses)

    monkeypatch.setattr(steam_api, "_request_json", fake_request_json)

    summary, reviews = fetch_reviews(
        object(),
        2016590,
        max_reviews=10,
        language="all",
        filter_mode="all",
        timeout=30,
        request_delay=0,
    )

    assert summary["total_reviews"] == 3
    assert [review["recommendationid"] for review in reviews] == ["1", "2", "3"]
    assert len(calls) == 2
    assert calls[0]["params"]["num_per_page"] == 0
    assert calls[0]["params"]["filter"] == "all"
    assert calls[0]["params"]["day_range"] == REVIEW_FULL_HISTORY_DAYS
    assert calls[0]["timeout"] == REVIEW_MIN_TIMEOUT
    assert calls[0]["max_retries"] == REVIEW_PAGE_MAX_RETRIES
    assert calls[1]["params"]["num_per_page"] == 3
    assert calls[1]["params"]["filter"] == "recent"
    assert calls[1]["params"]["day_range"] == REVIEW_FULL_HISTORY_DAYS
    assert calls[1]["timeout"] == REVIEW_MIN_TIMEOUT
    assert calls[1]["max_retries"] == REVIEW_PAGE_MAX_RETRIES


def test_fetch_reviews_stops_on_duplicate_page(monkeypatch):
    responses = iter(
        [
            {"query_summary": {"total_reviews": 10}},
            {
                "query_summary": {"total_reviews": 10},
                "reviews": [{"recommendationid": "1"}, {"recommendationid": "2"}],
                "cursor": "cursor-2",
            },
            {
                "query_summary": {"total_reviews": 10},
                "reviews": [{"recommendationid": "1"}, {"recommendationid": "2"}],
                "cursor": "cursor-3",
            },
        ]
    )

    def fake_request_json(*args, **kwargs):
        return next(responses)

    monkeypatch.setattr(steam_api, "_request_json", fake_request_json)

    _, reviews = fetch_reviews(
        object(),
        2016590,
        max_reviews=10,
        language="all",
        filter_mode="all",
        timeout=30,
        request_delay=0,
    )

    assert [review["recommendationid"] for review in reviews] == ["1", "2"]


def test_fetch_reviews_preserves_prefetched_summary(monkeypatch):
    responses = iter(
        [
            {"query_summary": {"total_reviews": 2, "review_score_desc": "Mixed"}},
            {
                "query_summary": {},
                "reviews": [{"recommendationid": "1"}, {"recommendationid": "2"}],
                "cursor": "next",
            },
        ]
    )

    def fake_request_json(*args, **kwargs):
        return next(responses)

    monkeypatch.setattr(steam_api, "_request_json", fake_request_json)

    summary, reviews = fetch_reviews(
        object(),
        2016590,
        max_reviews=10,
        language="all",
        filter_mode="all",
        timeout=30,
        request_delay=0,
    )

    assert summary["total_reviews"] == 2
    assert summary["review_score_desc"] == "Mixed"
    assert len(reviews) == 2


if __name__ == "__main__":
    test_parse_steam_app_ids_from_numbers_and_urls()
    test_normalize_keywords_deduplicates_case_and_spaces()
    test_steam_api_tool_registered()
    test_steam_api_extended_fields_present()
    test_parse_steam_ids_from_plain_values_and_profile_urls()
    test_player_contexts_from_review_rows_deduplicates_by_target_game()
    test_player_profile_fields_present()
    print("[PASS] Steam API tests passed")
