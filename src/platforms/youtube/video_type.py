# -*- coding: utf-8 -*-
"""Shared YouTube long/short video type detection helpers."""

from __future__ import annotations

import concurrent.futures
import time
import urllib.error
import urllib.request


SHORTS = "Shorts"
NORMAL_VIDEO = "普通视频"
UNKNOWN = "未知"
REDIRECT_STATUSES = (301, 302, 303, 307, 308)
DEFAULT_VIDEO_TYPE_WORKERS = 2
MAX_VIDEO_TYPE_WORKERS = 10


class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Stop on redirects so Shorts URL behavior can be inspected."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def check_video_type(video_id: str, opener=None, max_attempts: int = 3, timeout: int = 5) -> str:
    """Detect whether a YouTube video is Shorts or a regular video."""
    vid = (video_id or "").strip()
    if not vid:
        return UNKNOWN

    if opener is None:
        opener = urllib.request.build_opener(NoRedirectHandler)

    req = urllib.request.Request(f"https://www.youtube.com/shorts/{vid}", method="HEAD")
    req.add_header("User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36")

    for attempt in range(max_attempts):
        try:
            resp = opener.open(req, timeout=timeout)
            try:
                if getattr(resp, "status", None) == 200:
                    return SHORTS
            finally:
                close = getattr(resp, "close", None)
                if callable(close):
                    close()
        except urllib.error.HTTPError as exc:
            if exc.code in REDIRECT_STATUSES:
                return NORMAL_VIDEO
            if attempt < max_attempts - 1:
                time.sleep(0.5 * (2 ** attempt))
                continue
        except (urllib.error.URLError, Exception):
            if attempt < max_attempts - 1:
                time.sleep(0.5 * (2 ** attempt))
                continue
    return UNKNOWN


def normalize_video_type_workers(value, default: int = DEFAULT_VIDEO_TYPE_WORKERS) -> int:
    """Normalize configured video type detection worker count."""
    try:
        workers = int(value)
    except (TypeError, ValueError):
        workers = default
    return max(1, min(MAX_VIDEO_TYPE_WORKERS, workers))


def check_video_type_bulk(video_ids: list[str], max_workers: int = DEFAULT_VIDEO_TYPE_WORKERS,
                          log_callback=None, stop_event=None, pause_event=None) -> dict[str, str]:
    """Detect video types in parallel using the Shorts HEAD redirect rule."""
    from src.core import log_line as _log_line, should_stop as _should_stop, wait_if_paused as _wait_if_paused

    unique_ids = list(dict.fromkeys((vid or "").strip() for vid in video_ids if (vid or "").strip()))
    if not unique_ids:
        return {}

    worker_count = min(normalize_video_type_workers(max_workers), len(unique_ids))
    results: dict[str, str] = {}
    completed = 0
    total = len(unique_ids)
    with concurrent.futures.ThreadPoolExecutor(max_workers=worker_count) as executor:
        # 每个任务由 check_video_type 内部各自构建 opener。
        # urllib 的 OpenerDirector 非线程安全，不能在多 worker 间共享同一实例。
        future_to_vid = {executor.submit(check_video_type, vid): vid for vid in unique_ids}
        for future in concurrent.futures.as_completed(future_to_vid):
            if _should_stop(stop_event) or _wait_if_paused(pause_event, stop_event):
                for f in future_to_vid:
                    f.cancel()
                break
            vid = future_to_vid[future]
            try:
                results[vid] = future.result()
            except Exception:
                results[vid] = UNKNOWN
            completed += 1
            if completed % max(1, min(worker_count * 10, 100)) == 0 or completed == total:
                _log_line(log_callback, f"  视频类型检测进度：{completed}/{total}")
    return results
