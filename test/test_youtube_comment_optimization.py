from unittest.mock import MagicMock

from googleapiclient.errors import HttpError

from src.platforms.youtube import comments as comments_module
from src.platforms.youtube.comments import (
    COMMENT_MODE_DEEP,
    COMMENT_MODE_FAST,
    CommentFetchTask,
    effective_comment_scan_limit,
    fetch_top_comments_for_videos,
    has_zero_comment_count,
    normalize_comment_workers,
    parse_comment_count,
)


def make_http_error(status: int, content: bytes) -> HttpError:
    response = MagicMock(status=status)
    return HttpError(response, content)


def test_effective_comment_scan_limit_fast_and_deep_modes():
    assert effective_comment_scan_limit(500, 100, COMMENT_MODE_FAST) == 100
    assert effective_comment_scan_limit(500, 100, COMMENT_MODE_DEEP) == 500
    assert effective_comment_scan_limit(50, 100, COMMENT_MODE_DEEP) == 100


def test_normalize_comment_workers_bounds_values():
    assert normalize_comment_workers("5") == 5
    assert normalize_comment_workers(0) == 1
    assert normalize_comment_workers(99) == 10


def test_parse_comment_count_and_zero_detection():
    assert parse_comment_count("0") == 0
    assert parse_comment_count("1,234") == 1234
    assert parse_comment_count("") is None
    assert parse_comment_count("隐藏") is None
    assert has_zero_comment_count("0") is True
    assert has_zero_comment_count("1") is False
    assert has_zero_comment_count("") is False


def test_comment_403_forbidden_is_not_quota_error():
    error = make_http_error(
        403,
        b"""{
          "error": {
            "message": "One or more of the requested comment threads cannot be retrieved due to insufficient permissions.",
            "errors": [{"domain": "youtube.commentThread", "reason": "forbidden"}]
          }
        }""",
    )

    assert comments_module.is_comment_unavailable_error(error) is True
    assert comments_module.is_comment_quota_error(error) is False
    assert comments_module.is_comment_quota_error_message(str(error), 403) is False


def test_comment_403_quota_is_quota_error():
    error = make_http_error(
        403,
        b"""{
          "error": {
            "message": "The request cannot be completed because you have exceeded your quota.",
            "errors": [{"reason": "quotaExceeded"}]
          }
        }""",
    )

    assert comments_module.is_comment_quota_error(error) is True
    assert comments_module.is_comment_unavailable_error(error) is False
    assert comments_module.is_comment_quota_error_message(str(error), 403) is True


def test_fetch_top_comments_for_videos_keeps_video_mapping_and_failures(monkeypatch):
    calls = []

    class DummyPool:
        def __init__(self, api_keys):
            self.api_keys = api_keys

    def fake_fetch(_pool, video_id, max_scan_comments, *_args, **_kwargs):
        calls.append((video_id, max_scan_comments))
        if video_id == "fail":
            raise RuntimeError("boom")
        return [{"like_count": 1, "text": f"text-{video_id}", "published_at": ""}]

    monkeypatch.setattr(comments_module, "YouTubeClientPool", DummyPool)
    monkeypatch.setattr(comments_module, "fetch_top_level_comments", fake_fetch)

    results = fetch_top_comments_for_videos(
        ["key"],
        [CommentFetchTask("b"), CommentFetchTask("a"), CommentFetchTask("fail")],
        max_scan_comments=500,
        top_comment_limit=100,
        comment_mode=COMMENT_MODE_FAST,
        workers=1,
        log_callback=lambda _msg: None,
    )

    assert calls == [("b", 100), ("a", 100), ("fail", 100)]
    assert results["b"].comments[0]["text"] == "text-b"
    assert results["a"].comments[0]["text"] == "text-a"
    assert results["fail"].status == "error"


def test_fetch_top_comments_for_videos_treats_forbidden_comment_as_disabled(monkeypatch):
    class DummyPool:
        def __init__(self, api_keys):
            self.api_keys = api_keys

    forbidden_error = make_http_error(
        403,
        b"""{
          "error": {
            "message": "One or more of the requested comment threads cannot be retrieved due to insufficient permissions.",
            "errors": [{"domain": "youtube.commentThread", "reason": "forbidden"}]
          }
        }""",
    )

    def fake_fetch(_pool, _video_id, *_args, **_kwargs):
        raise forbidden_error

    monkeypatch.setattr(comments_module, "YouTubeClientPool", DummyPool)
    monkeypatch.setattr(comments_module, "fetch_top_level_comments", fake_fetch)

    results = fetch_top_comments_for_videos(
        ["key"],
        [CommentFetchTask("forbidden")],
        max_scan_comments=500,
        top_comment_limit=100,
        comment_mode=COMMENT_MODE_FAST,
        workers=1,
        log_callback=lambda _msg: None,
    )

    assert results["forbidden"].status == "disabled"


def test_fetch_top_comments_for_videos_marks_lower_level_forbidden_as_disabled(monkeypatch):
    class DummyClient:
        def commentThreads(self):
            return self

        def list(self, **_kwargs):
            return object()

    class DummyPool:
        def __init__(self, api_keys):
            self.api_keys = api_keys
            self.client = DummyClient()

        def next_client(self):
            return False

    forbidden_error = make_http_error(
        403,
        b"""{
          "error": {
            "message": "One or more of the requested comment threads cannot be retrieved due to insufficient permissions.",
            "errors": [{"domain": "youtube.commentThread", "reason": "forbidden"}]
          }
        }""",
    )

    monkeypatch.setattr(comments_module, "YouTubeClientPool", DummyPool)
    monkeypatch.setattr(comments_module, "execute_with_retry", lambda *_args, **_kwargs: (_ for _ in ()).throw(forbidden_error))

    results = fetch_top_comments_for_videos(
        ["key"],
        [CommentFetchTask("forbidden")],
        max_scan_comments=500,
        top_comment_limit=100,
        comment_mode=COMMENT_MODE_FAST,
        workers=1,
        log_callback=lambda _msg: None,
    )

    assert results["forbidden"].status == "disabled"


def test_fetch_top_comments_for_videos_invokes_result_callback(monkeypatch):
    callback_results = []

    class DummyPool:
        def __init__(self, api_keys):
            self.api_keys = api_keys

    def fake_fetch(_pool, video_id, *_args, **_kwargs):
        return [{"like_count": 1, "text": f"text-{video_id}", "published_at": ""}]

    monkeypatch.setattr(comments_module, "YouTubeClientPool", DummyPool)
    monkeypatch.setattr(comments_module, "fetch_top_level_comments", fake_fetch)

    fetch_top_comments_for_videos(
        ["key"],
        [CommentFetchTask("a"), CommentFetchTask("b")],
        max_scan_comments=500,
        top_comment_limit=100,
        comment_mode=COMMENT_MODE_FAST,
        workers=1,
        log_callback=lambda _msg: None,
        result_callback=lambda task, result: callback_results.append((task.video_id, result.status)) or True,
    )

    assert callback_results == [("a", "ok"), ("b", "ok")]


def test_fetch_top_comments_callback_mode_consumes_iterator_without_retaining_results(monkeypatch):
    callback_results = []

    class DummyPool:
        def __init__(self, api_keys):
            self.api_keys = api_keys

    monkeypatch.setattr(comments_module, "YouTubeClientPool", DummyPool)
    monkeypatch.setattr(
        comments_module,
        "fetch_top_level_comments",
        lambda _pool, video_id, *_args, **_kwargs: [
            {"comment_id": f"comment-{video_id}", "like_count": 1, "text": video_id, "published_at": ""}
        ],
    )
    tasks = (CommentFetchTask(f"video-{index}") for index in range(20))

    results = fetch_top_comments_for_videos(
        ["key"],
        tasks,
        max_scan_comments=100,
        top_comment_limit=10,
        comment_mode=COMMENT_MODE_FAST,
        workers=3,
        log_callback=lambda _msg: None,
        result_callback=lambda task, _result: callback_results.append(task.video_id) or True,
        retain_results=False,
        queue_capacity=16,
        total_tasks=20,
    )

    assert results == {}
    assert set(callback_results) == {f"video-{index}" for index in range(20)}


def test_fetch_top_comments_for_videos_reuses_client_pool_in_serial(monkeypatch):
    created_pools = []

    class DummyPool:
        def __init__(self, api_keys):
            self.api_keys = api_keys
            created_pools.append(self)

    def fake_fetch(_pool, video_id, *_args, **_kwargs):
        return [{"like_count": 1, "text": f"text-{video_id}", "published_at": ""}]

    monkeypatch.setattr(comments_module, "YouTubeClientPool", DummyPool)
    monkeypatch.setattr(comments_module, "fetch_top_level_comments", fake_fetch)

    fetch_top_comments_for_videos(
        ["key"],
        [CommentFetchTask("a"), CommentFetchTask("b")],
        max_scan_comments=500,
        top_comment_limit=100,
        comment_mode=COMMENT_MODE_FAST,
        workers=1,
        log_callback=lambda _msg: None,
    )

    assert len(created_pools) == 1


def test_fetch_top_comments_for_videos_retries_transient_comment_error(monkeypatch):
    calls = []
    refresh_count = []

    class DummyPool:
        def __init__(self, api_keys):
            self.api_keys = api_keys

        def refresh_current_client(self):
            refresh_count.append(1)

    def fake_fetch(_pool, video_id, *_args, **_kwargs):
        calls.append(video_id)
        if len(calls) == 1:
            raise ConnectionResetError(10054, "远程主机强迫关闭了一个现有的连接")
        return [{"like_count": 2, "text": f"text-{video_id}", "published_at": ""}]

    monkeypatch.setattr(comments_module, "YouTubeClientPool", DummyPool)
    monkeypatch.setattr(comments_module, "fetch_top_level_comments", fake_fetch)
    monkeypatch.setattr(comments_module, "interruptible_sleep", lambda *_args, **_kwargs: False)

    results = fetch_top_comments_for_videos(
        ["key"],
        [CommentFetchTask("a")],
        max_scan_comments=500,
        top_comment_limit=100,
        comment_mode=COMMENT_MODE_FAST,
        workers=1,
        log_callback=lambda _msg: None,
        video_retries=2,
    )

    assert calls == ["a", "a"]
    assert refresh_count == [1]
    assert results["a"].status == "ok"
    assert results["a"].comments[0]["text"] == "text-a"
