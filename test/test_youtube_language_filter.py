import src.platforms.youtube.keyword as keyword_module
from src.platforms.youtube.keyword import (
    fetch_video_rows,
    iter_search_video_id_batches,
    language_matches_snippet,
    parse_language_filter,
)


def test_parse_language_filter_normalizes_common_separators():
    assert parse_language_filter(" zh-CN, zh-TW; en\nJA  ") == {"zh-cn", "zh-tw", "en", "ja"}
    assert parse_language_filter("") == set()
    assert parse_language_filter(None) == set()


def test_language_matches_snippet_prefers_audio_then_text_and_is_strict():
    assert language_matches_snippet({"defaultAudioLanguage": "ZH-CN", "defaultLanguage": "en"}, {"zh-cn"}) == (
        True,
        "defaultAudioLanguage",
    )
    assert language_matches_snippet({"defaultLanguage": "en"}, {"en"}) == (True, "defaultLanguage")
    assert language_matches_snippet({"defaultAudioLanguage": "zh-TW"}, {"zh-cn"}) == (False, "mismatch")
    assert language_matches_snippet({"defaultAudioLanguage": "zh-CN"}, {"zh"}) == (True, "defaultAudioLanguage(prefix)")
    assert language_matches_snippet({}, {"en"}) == (False, "missing")
    assert language_matches_snippet({}, set()) == (True, "disabled")


def test_search_relevance_language_is_added_only_when_passed(monkeypatch):
    captured_params = []

    def fake_search(_client_pool, params, _log_callback, _stop_event=None):
        captured_params.append(dict(params))
        return {"items": [{"id": {"videoId": "abc123def45"}}]}

    monkeypatch.setattr(keyword_module, "_search_with_rotation", fake_search)

    list(
        iter_search_video_id_batches(
            object(),
            "demo",
            1,
            False,
            None,
            None,
            None,
            relevance_language="en",
        )
    )
    list(iter_search_video_id_batches(object(), "demo", 1, False, None, None, None))

    assert captured_params[0]["relevanceLanguage"] == "en"
    assert "relevanceLanguage" not in captured_params[1]


class _VideosResource:
    def __init__(self, client):
        self.client = client

    def list(self, **params):
        self.client.last_params = params
        return "request"


class _Client:
    def __init__(self):
        self.last_params = {}

    def videos(self):
        return _VideosResource(self)


class _ClientPool:
    def __init__(self):
        self.client = _Client()


def test_fetch_video_rows_includes_language_fields_and_filters(monkeypatch):
    pool = _ClientPool()

    def fake_api_call(_pool, build_request, _log_callback, _stop_event=None):
        build_request()
        return {
            "items": [
                {
                    "id": "keepaudio01",
                    "snippet": {
                        "title": "Keep audio",
                        "channelId": "UC1",
                        "publishedAt": "2024-01-01T00:00:00Z",
                        "defaultAudioLanguage": "en",
                        "defaultLanguage": "zh-CN",
                    },
                    "contentDetails": {"duration": "PT1M"},
                    "statistics": {"viewCount": "10", "likeCount": "1"},
                },
                {
                    "id": "keeptext001",
                    "snippet": {
                        "title": "Keep text",
                        "channelId": "UC2",
                        "publishedAt": "2024-01-02T00:00:00Z",
                        "defaultLanguage": "en",
                    },
                    "contentDetails": {"duration": "PT2M"},
                    "statistics": {"viewCount": "20", "likeCount": "2"},
                },
                {
                    "id": "dropmismatch",
                    "snippet": {"title": "Drop mismatch", "defaultAudioLanguage": "zh-CN"},
                    "contentDetails": {"duration": "PT3M"},
                    "statistics": {},
                },
                {
                    "id": "dropmissing",
                    "snippet": {"title": "Drop missing"},
                    "contentDetails": {"duration": "PT4M"},
                    "statistics": {},
                },
            ]
        }

    monkeypatch.setattr(keyword_module, "_api_call_with_rotation", fake_api_call)

    rows = fetch_video_rows(
        pool,
        "demo",
        ["keepaudio01", "keeptext001", "dropmismatch", "dropmissing"],
        batch_size=50,
        log_callback=lambda _msg: None,
        target_languages={"en"},
    )

    assert "defaultAudioLanguage,defaultLanguage" in pool.client.last_params["fields"]
    assert [row["视频标题"] for row in rows] == ["Keep audio", "Keep text"]
