# -*- coding: utf-8 -*-
import threading
from pathlib import Path
import pytest
from unittest.mock import MagicMock, patch
from googleapiclient.errors import HttpError
from src.core.output import generate_run_id, get_output_root
from src.platforms.youtube.profiles import (
    validate_and_normalize_youtube_url,
    classify_api_error,
    resolve_channel,
    run_channel_spider,
    RunStatus
)

def test_url_validation_success():
    # UC ID
    url, method, val = validate_and_normalize_youtube_url("youtube.com/channel/UC1234567890abcdef")
    assert url == "https://www.youtube.com/channel/UC1234567890abcdef"
    assert method == "channel_id"
    assert val == "UC1234567890abcdef"

    # Handle
    url, method, val = validate_and_normalize_youtube_url("https://www.youtube.com/@elonmusk")
    assert url == "https://www.youtube.com/@elonmusk"
    assert method == "handle"
    assert val == "@elonmusk"

    # User
    url, method, val = validate_and_normalize_youtube_url("www.youtube.com/user/billgates")
    assert url == "https://www.youtube.com/user/billgates"
    assert method == "username"
    assert val == "billgates"

def test_handle_returns_valid_homepage_url():
    url, method, val = validate_and_normalize_youtube_url("m.youtube.com/@some_user")
    assert url == "https://www.youtube.com/@some_user"

def test_rejects_invalid_scheme_httpx():
    with pytest.raises(ValueError, match="不支持的 scheme"):
        validate_and_normalize_youtube_url("httpx://www.youtube.com/@foo")

def test_accepts_host_with_standard_port():
    url, method, val = validate_and_normalize_youtube_url("youtube.com:443/@elonmusk")
    assert url == "https://www.youtube.com/@elonmusk"

def test_rejects_non_youtube_host():
    with pytest.raises(ValueError, match="不支持的域名"):
        validate_and_normalize_youtube_url("https://notyoutube.com/@elonmusk")

def test_rejects_extra_path_after_handle():
    with pytest.raises(ValueError, match="handle 路径包含额外段"):
        validate_and_normalize_youtube_url("https://youtube.com/@elonmusk/videos")

def test_url_validation_rejection():
    # Fake domain
    with pytest.raises(ValueError, match="不支持的域名"):
        validate_and_normalize_youtube_url("fake-youtube.com/channel/UC123")
        
    # Empty
    with pytest.raises(ValueError, match="URL为空"):
        validate_and_normalize_youtube_url("")

    # Video / Playlist / Shorts
    with pytest.raises(ValueError, match="不支持的资源链接类型"):
        validate_and_normalize_youtube_url("youtube.com/watch?v=123")
    with pytest.raises(ValueError, match="不支持的资源链接类型"):
        validate_and_normalize_youtube_url("youtube.com/playlist?list=123")
    with pytest.raises(ValueError, match="不支持的资源链接类型"):
        validate_and_normalize_youtube_url("youtube.com/shorts/123")

    # c or custom alias
    with pytest.raises(ValueError, match="不支持自定义别名"):
        validate_and_normalize_youtube_url("youtube.com/c/nvidia")

def test_api_error_classification():
    # 400
    resp_400 = MagicMock(status=400)
    exc_400 = HttpError(resp_400, b"bad request")
    assert classify_api_error(exc_400) == "invalid_request"

    # 401
    resp_auth = MagicMock(status=401)
    exc_auth = HttpError(resp_auth, b"invalid key")
    assert classify_api_error(exc_auth) == "auth_invalid"

    # 403 quota
    resp_quota = MagicMock(status=403)
    content_quota = b'{"error": {"message": "The request cannot be completed because you have exceeded your quota.", "errors": [{"reason": "quotaExhausted"}]}}'
    exc_quota = HttpError(resp_quota, content_quota)
    assert classify_api_error(exc_quota) == "quota_exhausted"

    # 403 forbidden non-quota
    resp_forbidden = MagicMock(status=403)
    content_forbidden = b'{"error": {"message": "Access forbidden.", "errors": [{"reason": "forbidden"}]}}'
    exc_forbidden = HttpError(resp_forbidden, content_forbidden)
    assert classify_api_error(exc_forbidden) == "forbidden_resource"

    # 403 API Key suspended
    content_suspended = b'{"error": {"message": "Permission denied: Consumer \'api_key:abc\' has been suspended.", "errors": [{"reason": "forbidden"}]}}'
    exc_suspended = HttpError(resp_forbidden, content_suspended)
    assert classify_api_error(exc_suspended) == "api_key_suspended"

    # 404
    resp_404 = MagicMock(status=404)
    exc_404 = HttpError(resp_404, b"not found")
    assert classify_api_error(exc_404) == "not_found"

    # 429
    resp_429 = MagicMock(status=429)
    exc_429 = HttpError(resp_429, b"rate limited")
    assert classify_api_error(exc_429) == "rate_limited"

    # 503
    resp_503 = MagicMock(status=503)
    exc_503 = HttpError(resp_503, b"service unavailable")
    assert classify_api_error(exc_503) == "transient_network"

def test_key_rotation_restrict():
    client_pool = MagicMock()
    client_pool.next_client.return_value = True
    
    # 1. Quota error triggers rotation
    resp_quota = MagicMock(status=403)
    exc_quota = HttpError(resp_quota, b"quota exceeded")
    
    with patch('src.platforms.youtube.profiles.execute_with_retry') as mock_exec:
        call_count = 0
        def side_effect(req, cb):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise exc_quota
            return {"items": [{"id": "UC123", "snippet": {}, "statistics": {}}]}
        mock_exec.side_effect = side_effect
        
        resolve_channel(client_pool, "http://youtube.com/channel/UC123", "channel_id", "UC123")
        assert client_pool.next_client.call_count == 1

    # 2. Forbidden resource (other than quota) does NOT trigger rotation
    resp_forbidden = MagicMock(status=403)
    exc_forbidden = HttpError(resp_forbidden, b"access forbidden")
    client_pool.next_client.reset_mock()
    
    with patch('src.platforms.youtube.profiles.execute_with_retry') as mock_exec:
        def side_effect_forbidden(req, cb):
            raise exc_forbidden
        mock_exec.side_effect = side_effect_forbidden
        
        with pytest.raises(HttpError):
            resolve_channel(client_pool, "http://youtube.com/channel/UC123", "channel_id", "UC123")
        assert client_pool.next_client.call_count == 0

    # 3. A suspended API Key is a key-level error and triggers rotation
    resp_suspended = MagicMock(status=403)
    content_suspended = b'{"error": {"message": "Permission denied: Consumer \'api_key:abc\' has been suspended.", "errors": [{"reason": "forbidden"}]}}'
    exc_suspended = HttpError(resp_suspended, content_suspended)
    client_pool.next_client.reset_mock()

    with patch('src.platforms.youtube.profiles.execute_with_retry') as mock_exec:
        call_count = 0

        def side_effect_suspended(req, cb):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise exc_suspended
            return {"items": [{"id": "UC123", "snippet": {}, "statistics": {}}]}

        mock_exec.side_effect = side_effect_suspended

        resolve_channel(client_pool, "http://youtube.com/channel/UC123", "channel_id", "UC123")
        assert client_pool.next_client.call_count == 1

def test_run_channel_spider_cancellation(tmp_path):
    txt = tmp_path / "urls.txt"
    with open(txt, "w") as f:
        f.write("youtube.com/channel/UC1234567890abcdef\n")
        f.write("youtube.com/channel/UC1111111111bbbbbb\n")
        
    stop_event = threading.Event()
    stop_event.set()
    run_id = "test_profiles_" + generate_run_id()
    
    outcome = run_channel_spider(
        api_keys=["key"],
        txt_file_path=str(txt),
        log_callback=MagicMock(),
        finish_callback=MagicMock(),
        stop_event=stop_event,
        config={"max_parallel_tabs": 1},
        run_id=run_id,
    )
    
    assert outcome.status == RunStatus.CANCELLED
    assert outcome.output_path is None
    assert not any(a.label == "YouTube博主信息数据" for a in outcome.artifacts)
    assert outcome.stats.skipped_count == 2
    report_artifact = next(a for a in outcome.artifacts if a.label == "任务执行报告")
    expected_run_dir = get_output_root() / "youtube" / "profiles" / run_id
    assert Path(report_artifact.path).parent == expected_run_dir
    assert not (get_output_root() / "youtube_profiles" / run_id).exists()
