# -*- coding: utf-8 -*-
"""YouTube 视频数据与评论采集核心模块。

本模块基于 Google YouTube v3 API，提供视频详情（标题、播放量、发布日期、时长、简介等）、
精确视频类型检测（通过 HEAD 请求判断是否为 Shorts 短视频），以及视频主楼评论的高效分页采集。
"""

from __future__ import annotations

from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass
import hashlib
import itertools
import json
import os
import queue
import re
import threading
import time
from urllib.parse import parse_qs, urlparse

from googleapiclient.errors import HttpError
from src.platforms.youtube.keyword import YouTubeClientPool, execute_with_retry, is_transient_connection_error
from src.platforms.youtube.video_type import check_video_type_bulk, normalize_video_type_workers

from src.core import (
    ArtifactRef,
    RunError,
    RunOutcome,
    RunStats,
    RunStatus,
    SpoolRow,
    SpoolSheet,
    SqliteRowSpool,
    build_output_path,
    build_spool_path,
    export_spool_to_xlsx,
    feedback_from_config,
    find_latest_spool_path,
    generate_run_id,
    interruptible_sleep,
    log_error,
    log_line,
    log_warn,
    remove_spool_files,
    should_stop,
    wait_if_paused,
)
from src.core.task_checkpoint import TaskCheckpoint, open_task_checkpoint, task_fingerprint

# Excel 表头定义
VIDEO_FIELDS = ["编号", "视频链接", "博主主页链接", "标题", "频道名称", "发布日期", "视频类型", "直播状态", "关联视频标题", "关联视频链接", "视频时长", "视频简介", "播放量", "点赞数", "评论数"]
COMMENT_FIELDS = ["编号", "视频链接", "评论的点赞量", "评论内容", "发布时间"]

# 默认导出热门评论的上限
TOP_COMMENT_LIMIT = 100
# 默认扫描评论的最大安全阈值
DEFAULT_SCAN_LIMIT = 500
COMMENT_MODE_FAST = "快速模式"
COMMENT_MODE_DEEP = "深扫模式"
DEFAULT_COMMENT_WORKERS = 5
DEFAULT_COMMENT_VIDEO_RETRIES = 2
VIDEO_METRICS_TOOL_ID = "youtube_top_comments"
VIDEO_METRICS_STORAGE_VERSION = 2
COMMENT_QUOTA_ERROR_MARKERS = (
    "quotaexceeded",
    "dailylimitexceeded",
    "ratelimitexceeded",
    "userratelimitexceeded",
    "rate limit",
    "quota exceeded",
)
COMMENT_UNAVAILABLE_ERROR_MARKERS = (
    "commentsdisabled",
    "comments disabled",
    "cannot be retrieved due to insufficient permissions",
    "insufficient permissions",
    "not be properly authorized",
    "youtube.commentthread",
)


@dataclass(frozen=True)
class CommentFetchTask:
    video_id: str
    video_url: str = ""
    index: str = ""


@dataclass
class CommentFetchResult:
    video_id: str
    comments: list[dict]
    status: str = "ok"
    error: str = ""
    http_status: int = 0


class CommentUnavailableError(Exception):
    """Raised when one video's comment thread is inaccessible but the overall run can continue."""

    def __init__(self, video_id: str, error: HttpError):
        self.video_id = video_id
        self.error = error
        super().__init__(str(error))


def normalize_comment_mode(value: str | None) -> str:
    if str(value or "").strip() == COMMENT_MODE_DEEP:
        return COMMENT_MODE_DEEP
    return COMMENT_MODE_FAST


def effective_comment_scan_limit(max_scan_comments: int, top_comment_limit: int, comment_mode: str) -> int:
    scan_limit = max(0, int(max_scan_comments or 0))
    top_limit = max(1, int(top_comment_limit or TOP_COMMENT_LIMIT))
    if normalize_comment_mode(comment_mode) == COMMENT_MODE_FAST:
        return min(scan_limit, top_limit) if scan_limit > 0 else top_limit
    return max(scan_limit, top_limit)


def normalize_comment_workers(value) -> int:
    try:
        workers = int(value)
    except (TypeError, ValueError):
        workers = DEFAULT_COMMENT_WORKERS
    return max(1, min(workers, 10))


def _http_error_text(error: HttpError) -> str:
    parts = [str(error)]
    content = getattr(error, "content", b"")
    if isinstance(content, bytes):
        try:
            content = content.decode("utf-8", errors="ignore")
        except Exception:
            content = ""
    if content:
        parts.append(str(content))
        try:
            payload = json.loads(content)
            parts.append(json.dumps(payload, ensure_ascii=False))
        except Exception:
            pass
    return " ".join(parts).lower()


def is_comment_quota_error(error: HttpError) -> bool:
    status = getattr(getattr(error, "resp", None), "status", 0)
    if status == 429:
        return True
    text = _http_error_text(error)
    return any(marker in text for marker in COMMENT_QUOTA_ERROR_MARKERS)


def is_comment_unavailable_error(error: HttpError) -> bool:
    if is_comment_quota_error(error):
        return False
    status = getattr(getattr(error, "resp", None), "status", 0)
    text = _http_error_text(error)
    if "disabled" in text and "comment" in text:
        return True
    if status == 403 and any(marker in text for marker in COMMENT_UNAVAILABLE_ERROR_MARKERS):
        return True
    if status == 403 and "forbidden" in text and "comment" in text:
        return True
    return False


def is_comment_quota_error_message(message: str, http_status: int = 0) -> bool:
    if int(http_status or 0) == 429:
        return True
    text = str(message or "").lower()
    return any(marker in text for marker in COMMENT_QUOTA_ERROR_MARKERS)


def is_recoverable_youtube_run_error(error: BaseException) -> bool:
    """Return whether rerunning with the retained spool can reasonably recover."""

    if is_transient_connection_error(error):
        return True
    if isinstance(error, HttpError):
        status = int(getattr(getattr(error, "resp", None), "status", 0) or 0)
        return status in {403, 408, 409, 429} or status >= 500
    return False


def parse_comment_count(value) -> int | None:
    text = str(value or "").strip()
    if not text:
        return None
    normalized = text.replace(",", "").replace("，", "")
    return int(normalized) if normalized.isdigit() else None


def has_zero_comment_count(value) -> bool:
    return parse_comment_count(value) == 0


def format_youtube_datetime(date_str: str) -> str:
    """格式化 YouTube 返回的 ISO 8601 日期时间字符串为 "YYYY-MM-DD HH:MM:SS"。

    Args:
        date_str: 原始日期时间字符串（例如 "2026-06-04T12:00:00Z"）。

    Returns:
        str: 规整后的日期时间字符串。
    """
    if not date_str:
        return ""
    date_str = date_str.strip()
    cleaned = date_str.replace("T", " ").replace("Z", "").strip()
    if "." in cleaned:
        cleaned = cleaned.split(".")[0]
    return cleaned


def build_video_url(video_id: str, video_type: str) -> str:
    """根据视频 ID 和类型组装标准的视频播放 URL。

    Args:
        video_id: 视频的唯一 ID。
        video_type: 视频的类别（"Shorts" 或 其他）。

    Returns:
        str: 完整的播放链接。
    """
    if not video_id:
        return ""
    if video_type == "Shorts":
        return f"https://www.youtube.com/shorts/{video_id}"
    return f"https://www.youtube.com/watch?v={video_id}"


def normalize_youtube_url(url: str) -> str:
    """清洗并规范化输入的 YouTube 链接，丢弃锚点。

    Args:
        url: 原始链接。

    Returns:
        str: 规范化后的链接。
    """
    value = (url or "").strip()
    if not value:
        return ""
    if value.startswith("//"):
        value = "https:" + value
    if not value.startswith("http"):
        value = "https://" + value
    return value.split("#")[0].strip()


def extract_video_id(url: str) -> str:
    """从各种格式的 YouTube 链接中提取 11 位的视频 ID。

    支持的链接样式：
    - Standard: youtube.com/watch?v=VIDEO_ID
    - Shorts: youtube.com/shorts/VIDEO_ID
    - Embed: youtube.com/embed/VIDEO_ID
    - Share link: youtu.be/VIDEO_ID
    - Live: youtube.com/live/VIDEO_ID

    Args:
        url: 输入的播放链接。

    Returns:
        str: 11 位的视频唯一 ID，若提取失败返回空。
    """
    normalized = normalize_youtube_url(url)
    parsed = urlparse(normalized)
    host = parsed.netloc.lower()
    path_parts = [part for part in parsed.path.split("/") if part]

    if "youtu.be" in host and path_parts:
        return path_parts[0]
    if "youtube.com" in host:
        query_id = parse_qs(parsed.query).get("v", [""])[0]
        if query_id:
            return query_id
        if len(path_parts) >= 2 and path_parts[0] in {"shorts", "embed", "live"}:
            return path_parts[1]

    # 正则作为后备兜底匹配
    match = re.search(r"(?:v=|/video/|youtu\.be/|/shorts/|/embed/)([A-Za-z0-9_-]{6,})", normalized)
    return match.group(1) if match else ""


def canonical_video_url(video_id: str) -> str:
    """根据视频 ID 生成规范的普通视频链接。"""
    return f"https://www.youtube.com/watch?v={video_id}" if video_id else ""


def parse_video_entries(txt_path: str) -> list[dict[str, object]]:
    """读取 TXT 视频列表输入文件，提取唯一的视频链接及 ID 并去重。

    Args:
        txt_path: 存放链接的文本文件。

    Returns:
        list[dict]: 包含去重后视频编号、链接及 ID 的数据词典列表。
    """
    entries: list[dict[str, object]] = []
    seen_video_ids: set[str] = set()
    valid_line_count = 0
    duplicate_count = 0
    with open(txt_path, "r", encoding="utf-8-sig") as f:
        for line in f:
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            raw_url = normalize_youtube_url(stripped.split()[0])
            video_id = extract_video_id(raw_url)
            if not video_id:
                continue
            valid_line_count += 1
            if video_id in seen_video_ids:
                duplicate_count += 1
                continue
            seen_video_ids.add(video_id)
            entries.append(
                {
                    "编号": len(entries) + 1,
                    "视频链接": canonical_video_url(video_id),
                    "视频ID": video_id,
                    "预测类型": "Shorts" if "/shorts/" in raw_url else "视频",
                }
            )
    # 为每一条条目记录本次解析的行数特征，方便驱动端日志打印
    for entry in entries:
        entry["有效行数"] = valid_line_count
        entry["重复行数"] = duplicate_count
    return entries


def clean_comment_text(text: str) -> str:
    """清洗评论内容文本，去除换行符并替换为特征空格以防破坏表格布局。"""
    return (text or "").replace("\r", "").replace("\n", " | ").strip()


def non_text_placeholder(snippet: dict) -> str:
    """针对富媒体/非纯文本的评论生成占位占字符标记。"""
    keys = " ".join(str(key).lower() for key in snippet.keys())
    if "image" in keys or "photo" in keys:
        return "[图片]"
    if "video" in keys:
        return "[视频]"
    if "sticker" in keys:
        return "[贴纸]"
    return "[非文本]"


def format_youtube_duration(iso_duration: str) -> str:
    """将 YouTube 返回的 ISO 8601 时长格式（如 PT1H23M45S）转换为标准时间格式（HH:MM:SS）。

    Args:
        iso_duration: ISO 8601 时长字符串。

    Returns:
        str: "HH:MM:SS" 格式的时间长度字符串。
    """
    match = re.fullmatch(
        r"P(?:(?P<days>\d+)D)?(?:T(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?(?:(?P<seconds>\d+)S)?)?",
        iso_duration or "",
    )
    if not match:
        return ""
    days = int(match.group("days") or 0)
    hours = int(match.group("hours") or 0) + days * 24
    minutes = int(match.group("minutes") or 0)
    seconds = int(match.group("seconds") or 0)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def fetch_video_metrics(client_pool, video_ids: list[str], live_stream_policy: str = "不处理",
                         log_callback=None, stop_event=None, pause_event=None) -> dict[str, dict]:
    """调用 API 批量拉取视频的基本指标参数信息，单批上限 50 个。

    Args:
        client_pool: YouTubeClientPool 实例。
        video_ids: 视频 ID 列表。
        live_stream_policy: 直播处理策略。
        log_callback: 日志回调。
        stop_event: 线程停止信号。
        pause_event: 线程暂停信号。

    Returns:
        dict[str, dict]: 视频 ID 到指标字典的映射映射表。
    """
    result = {}
    api_part = "snippet,statistics,contentDetails"
    if live_stream_policy in ("保留并标记", "直接排除"):
        api_part += ",liveStreamingDetails"

    total_count = len(video_ids)
    for i in range(0, total_count, 50):
        if should_stop(stop_event) or wait_if_paused(pause_event, stop_event):
            break
        batch = video_ids[i:i+50]
        while True:
            try:
                response = execute_with_retry(
                    client_pool.client.videos().list(
                        part=api_part,
                        id=",".join(batch)
                    ),
                    None
                )
                break
            except HttpError as e:
                if e.resp.status in [403, 429]:
                    if client_pool.next_client():
                        continue
                raise e
        for item in response.get("items", []):
            vid = item.get("id")
            snippet = item.get("snippet", {})
            stats = item.get("statistics", {})
            content = item.get("contentDetails", {})
            pub_date = str(snippet.get("publishedAt", "")).replace("T", " ").replace("Z", "")
            if "." in pub_date:
                pub_date = pub_date.split(".")[0]
            desc = (snippet.get("description") or "").replace("\n", " | ").replace("\r", "")
            # 简介截断，防止表格内容过于臃肿
            if len(desc) > 300:
                desc = desc[:300] + "..."
            
            # 检测直播状态
            live_status = "非直播"
            if live_stream_policy != "不处理":
                broadcast_content = snippet.get("liveBroadcastContent", "none").lower()
                has_live_details = "liveStreamingDetails" in item
                
                if broadcast_content == "live":
                    live_status = "正在直播"
                elif broadcast_content == "upcoming":
                    live_status = "预告直播"
                elif has_live_details:
                    live_status = "直播回放"
                
                if live_stream_policy == "直接排除" and live_status != "非直播":
                    result[vid] = {"is_excluded": True}
                    continue
            
            result[vid] = {
                "标题": snippet.get("title", ""),
                "频道名称": snippet.get("channelTitle", ""),
                "频道ID": snippet.get("channelId", ""),
                "发布日期": pub_date,
                "直播状态": live_status if live_stream_policy != "不处理" else "",
                "视频时长": format_youtube_duration(content.get("duration", "")),
                "视频简介": desc,
                "播放量": stats.get("viewCount", ""),
                "点赞数": stats.get("likeCount", ""),
                "评论数": stats.get("commentCount", "")
            }
        log_line(log_callback, f"  视频详情进度：{min(i + 50, total_count)}/{total_count}")
    return result


def fetch_top_level_comments(client_pool, video_id: str, max_scan_comments: int, log_callback, stop_event=None, pause_event=None, api_page_size: int = 100) -> list[dict]:
    """调用 YouTube API 分页获取指定视频下相关性排序的首层主楼评论。

    Args:
        youtube: API 客户端。
        video_id: 目标视频 ID。
        max_scan_comments: 最多扫描的评论条数。
        log_callback: 日志回调。
        stop_event: 线程停止信号。
        pause_event: 线程暂停信号。
        api_page_size: API 每次拉取的页面数据大小。

    Returns:
        list[dict]: 提取出的评论列表数据。
    """
    comments: list[dict] = []
    next_page_token = None
    page_size = max(1, min(api_page_size, 100))

    while len(comments) < max_scan_comments:
        if should_stop(stop_event):
            log_line(log_callback, "  任务已停止。")
            break
        if wait_if_paused(pause_event, stop_event):
            break
        
        # 请求 API 获取评论线程列表
        while True:
            try:
                response = execute_with_retry(
                    client_pool.client.commentThreads().list(
                        part="snippet",
                        videoId=video_id,
                        maxResults=min(page_size, max_scan_comments - len(comments)),
                        pageToken=next_page_token,
                        order="relevance",
                        textFormat="plainText",
                    ),
                    log_callback,
                    stop_event,
                )
                break
            except HttpError as e:
                if is_comment_unavailable_error(e):
                    log_line(log_callback, f"  [API] 视频评论不可获取或权限不足 ({video_id})，跳过...")
                    raise CommentUnavailableError(video_id, e) from e
                if is_comment_quota_error(e):
                    if client_pool.next_client():
                        log_line(log_callback, f"  [API] 评论获取配额/限流受限 ({e.resp.status})，切换 Key ({client_pool.current_idx + 1}/{len(client_pool.api_keys)})...")
                        continue
                    log_line(log_callback, f"  [API] 所有 API Key 配额/限流均不可用 ({e.resp.status})，终止评论获取。")
                raise e

        for item in response.get("items", []):
            if should_stop(stop_event):
                break
            top_comment = item.get("snippet", {}).get("topLevelComment", {})
            snippet = top_comment.get("snippet", {})
            text = clean_comment_text(snippet.get("textDisplay") or snippet.get("textOriginal") or "")
            if not text:
                text = non_text_placeholder(snippet)
            published_at = str(snippet.get("publishedAt") or "")
            if published_at:
                published_at = format_youtube_datetime(published_at)
            comments.append(
                {
                    "comment_id": str(top_comment.get("id") or item.get("id") or ""),
                    "like_count": int(snippet.get("likeCount", 0) or 0),
                    "text": text,
                    "published_at": published_at,
                }
            )
            if len(comments) >= max_scan_comments:
                log_line(log_callback, f"  已达扫描上限 {max_scan_comments} 条，停止翻页。")
                break

        if len(comments) % 200 == 0 or len(comments) < 100:
            log_line(log_callback, f"  已扫描主楼评论 {len(comments)} 条。")

        # 检查是否还有下一页
        next_page_token = response.get("nextPageToken")
        if not next_page_token:
            log_line(log_callback, f"  评论已翻到底，共 {len(comments)} 条。")
            break

    return comments


def build_comment_rows(video_index: str, video_url: str, comments: list[dict], top_comment_limit: int = TOP_COMMENT_LIMIT) -> list[dict[str, str]]:
    sorted_comments = sorted(comments, key=lambda item: item["like_count"], reverse=True)
    rows: list[dict[str, str]] = []
    for comment in sorted_comments[:top_comment_limit]:
        rows.append(
            {
                "编号": str(video_index),
                "视频链接": video_url,
                "评论的点赞量": str(comment["like_count"]),
                "评论内容": comment["text"],
                "发布时间": comment.get("published_at", ""),
            }
        )
    return rows


def fetch_top_comments_for_videos(
    api_keys: list[str],
    video_tasks,
    max_scan_comments: int,
    top_comment_limit: int,
    comment_mode: str,
    workers: int,
    log_callback,
    stop_event=None,
    pause_event=None,
    api_page_size: int = 100,
    result_callback=None,
    video_retries: int = DEFAULT_COMMENT_VIDEO_RETRIES,
    retain_results: bool = True,
    queue_capacity: int = 16,
    total_tasks: int | None = None,
) -> dict[str, CommentFetchResult]:
    if total_tasks is None and hasattr(video_tasks, "__len__"):
        total_tasks = len(video_tasks)
    task_iterator = iter(task for task in video_tasks if task.video_id)
    try:
        first_task = next(task_iterator)
    except StopIteration:
        return {}
    tasks = itertools.chain((first_task,), task_iterator)

    scan_limit = effective_comment_scan_limit(max_scan_comments, top_comment_limit, comment_mode)
    worker_count = normalize_comment_workers(workers)
    if total_tasks is not None:
        worker_count = min(worker_count, max(1, total_tasks))
    retry_count = max(1, int(video_retries or DEFAULT_COMMENT_VIDEO_RETRIES))
    mode = normalize_comment_mode(comment_mode)
    log_line(log_callback, f"  评论采集：{mode}，并发 {worker_count}，每视频扫描上限 {scan_limit}，输出前 {top_comment_limit} 条，断线重试 {retry_count} 次。")

    results: dict[str, CommentFetchResult] = {}
    completed = 0
    failed = 0
    empty = 0
    written_comments = 0

    def _fetch_one(task: CommentFetchTask, worker_pool: YouTubeClientPool) -> CommentFetchResult:
        if should_stop(stop_event):
            return CommentFetchResult(task.video_id, [], "stopped")
        if wait_if_paused(pause_event, stop_event):
            return CommentFetchResult(task.video_id, [], "stopped")
        for attempt in range(retry_count):
            try:
                comments = fetch_top_level_comments(
                    worker_pool,
                    task.video_id,
                    scan_limit,
                    None,
                    stop_event,
                    pause_event,
                    api_page_size=api_page_size,
                )
                comments.sort(key=lambda item: item["like_count"], reverse=True)
                return CommentFetchResult(task.video_id, comments[:top_comment_limit], "ok" if comments else "empty")
            except HttpError as exc:
                if is_comment_unavailable_error(exc):
                    return CommentFetchResult(task.video_id, [], "disabled", str(exc), http_status=exc.resp.status)
                return CommentFetchResult(task.video_id, [], "error", str(exc), http_status=exc.resp.status)
            except CommentUnavailableError as exc:
                status = getattr(getattr(exc.error, "resp", None), "status", 0)
                return CommentFetchResult(task.video_id, [], "disabled", str(exc), http_status=status)
            except Exception as exc:
                if is_transient_connection_error(exc) and attempt < retry_count - 1:
                    worker_pool.refresh_current_client()
                    wait_seconds = min(2 ** attempt, 8)
                    log_warn(log_callback, f"  评论连接中断 ({task.video_id})，刷新连接后 {wait_seconds} 秒重试（{attempt + 2}/{retry_count}）。")
                    if interruptible_sleep(wait_seconds, stop_event):
                        return CommentFetchResult(task.video_id, [], "stopped")
                    continue
                return CommentFetchResult(task.video_id, [], "error", str(exc))
        return CommentFetchResult(task.video_id, [], "error", "评论重试次数耗尽")

    def _record_result(task: CommentFetchTask, result: CommentFetchResult) -> bool:
        nonlocal completed, failed, empty, written_comments
        if retain_results:
            results[task.video_id] = result
        completed += 1
        if result.status == "error":
            failed += 1
        if result.status in {"empty", "disabled"}:
            empty += 1
        written_comments += len(result.comments)
        if result_callback is not None and result_callback(task, result) is False:
            return False
        return True

    if worker_count <= 1:
        worker_pool = YouTubeClientPool(api_keys)
        for task in tasks:
            if should_stop(stop_event):
                break
            result = _fetch_one(task, worker_pool)
            if not _record_result(task, result):
                break
            if completed % max(1, worker_count) == 0:
                total_label = str(total_tasks) if total_tasks is not None else "?"
                log_line(log_callback, f"  评论采集进度：{completed}/{total_label}，失败 {failed}，空/禁评 {empty}，已取评论 {written_comments} 条。")
    else:
        capacity = max(worker_count, int(queue_capacity or 16))
        task_queue: queue.Queue[CommentFetchTask | object] = queue.Queue(maxsize=capacity)
        result_queue: queue.Queue[tuple[CommentFetchTask, CommentFetchResult]] = queue.Queue(maxsize=capacity)
        stop_dispatch = threading.Event()
        feeder_done = threading.Event()
        task_sentinel = object()

        def _feed_tasks() -> None:
            try:
                for task in tasks:
                    while not stop_dispatch.is_set() and not should_stop(stop_event):
                        try:
                            task_queue.put(task, timeout=0.2)
                            break
                        except queue.Full:
                            continue
                    else:
                        break
                if not stop_dispatch.is_set() and not should_stop(stop_event):
                    for _ in range(worker_count):
                        while not stop_dispatch.is_set():
                            try:
                                task_queue.put(task_sentinel, timeout=0.2)
                                break
                            except queue.Full:
                                continue
            finally:
                feeder_done.set()

        def _worker_loop() -> None:
            worker_pool = YouTubeClientPool(api_keys)
            while not stop_dispatch.is_set() and not should_stop(stop_event):
                if wait_if_paused(pause_event, stop_event):
                    stop_dispatch.set()
                    break
                try:
                    task = task_queue.get(timeout=0.2)
                except queue.Empty:
                    if feeder_done.is_set():
                        break
                    continue
                try:
                    if task is task_sentinel:
                        break
                    assert isinstance(task, CommentFetchTask)
                    result = _fetch_one(task, worker_pool)
                    while not stop_dispatch.is_set():
                        try:
                            result_queue.put((task, result), timeout=0.2)
                            break
                        except queue.Full:
                            continue
                    if result.status == "stopped":
                        stop_dispatch.set()
                        break
                finally:
                    task_queue.task_done()

        feeder = threading.Thread(target=_feed_tasks, name="youtube-comment-feeder", daemon=True)
        feeder.start()
        executor = ThreadPoolExecutor(max_workers=worker_count)
        futures = [executor.submit(_worker_loop) for _ in range(worker_count)]
        try:
            while True:
                if should_stop(stop_event):
                    stop_dispatch.set()
                    break
                try:
                    task, result = result_queue.get(timeout=0.2)
                except queue.Empty:
                    if feeder_done.is_set() and all(f.done() for f in futures) and result_queue.empty():
                        break
                    continue
                if not _record_result(task, result):
                    stop_dispatch.set()
                    break
                if completed % max(1, worker_count) == 0:
                    total_label = str(total_tasks) if total_tasks is not None else "?"
                    log_line(log_callback, f"  评论采集进度：{completed}/{total_label}，失败 {failed}，空/禁评 {empty}，已取评论 {written_comments} 条。")
        finally:
            stop_dispatch.set()
            executor.shutdown(wait=False, cancel_futures=True)

    total_label = str(total_tasks) if total_tasks is not None else str(completed)
    log_line(log_callback, f"  评论采集完成：{completed}/{total_label}，失败 {failed}，空/禁评 {empty}，已取评论 {written_comments} 条。")
    return results


def empty_video_row(video_index: int, video_url: str) -> dict[str, str]:
    """当视频无评论或获取失败时，构建的空评论占位行。"""
    return {
        "编号": str(video_index),
        "视频链接": video_url,
        "评论的点赞量": "",
        "评论内容": "",
        "发布时间": "",
    }


def build_video_metrics_scope(
    video_ids: list[str],
    fetch_shorts_related: str,
    live_stream_policy: str,
    get_comments: str,
    check_type: str,
    max_scan_comments: int,
    config: dict | None = None,
) -> dict:
    """Build a stable, secret-free fingerprint scope for resumable video collection."""

    settings = config or {}
    return {
        "storage_version": VIDEO_METRICS_STORAGE_VERSION,
        "video_ids": list(video_ids),
        "fetch_shorts_related": fetch_shorts_related,
        "live_stream_policy": live_stream_policy,
        "get_comments": get_comments,
        "check_type": check_type,
        "max_scan_comments": int(max_scan_comments),
        "comment_top_limit": int(settings.get("comment_top_limit", TOP_COMMENT_LIMIT)),
        "comment_mode": normalize_comment_mode(settings.get("youtube_comment_mode", COMMENT_MODE_FAST)),
    }


def video_metrics_spool_path(scope: dict) -> str:
    fingerprint = task_fingerprint(VIDEO_METRICS_TOOL_ID, scope)
    return str(find_latest_spool_path(VIDEO_METRICS_TOOL_ID, fingerprint))


def _comment_spool_key(video_id: str, comment: dict, rank: int) -> str:
    comment_id = str(comment.get("comment_id") or "").strip()
    if comment_id:
        return comment_id
    raw = "|".join(
        (
            video_id,
            str(comment.get("published_at") or ""),
            str(comment.get("like_count") or 0),
            str(comment.get("text") or ""),
            str(rank),
        )
    )
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def run_youtube_video_metrics_spider(api_keys: list[str], txt_path: str, fetch_shorts_related: str, live_stream_policy: str, get_comments: str, check_type: str, max_scan_comments: int, log_callback, finish_callback, stop_event=None, config=None, pause_event=None):
    """Collect video details/comments into SQLite and export XLSX exactly once."""

    config = dict(config or {})
    get_comments_bool = get_comments == "是"
    check_type_bool = check_type == "是"
    fetch_shorts_related_bool = fetch_shorts_related == "是"
    top_comment_limit = int(config.get("comment_top_limit", TOP_COMMENT_LIMIT))
    api_page_size = int(config.get("youtube_api_page_size", 100))
    comment_mode = normalize_comment_mode(config.get("youtube_comment_mode", COMMENT_MODE_FAST))
    comment_workers = normalize_comment_workers(config.get("youtube_comment_workers", DEFAULT_COMMENT_WORKERS))
    video_type_workers = normalize_video_type_workers(config.get("youtube_video_type_workers"))
    shorts_related_delay = float(config.get("youtube_shorts_related_delay", 1.0))
    max_parallel_tabs = max(1, int(config.get("max_parallel_tabs", 3)))
    resume_action = str(config.get("_resume_action", "continue") or "continue")
    feedback = feedback_from_config(config)
    run_id = generate_run_id()
    output_path = build_output_path("youtube", f"youtube_video_metrics_{time.strftime('%Y%m%d_%H%M%S')}.xlsx", channel="video_metrics")
    report_path = str(output_path).replace(".xlsx", ".report.json")
    outcome = RunOutcome(run_id, VIDEO_METRICS_TOOL_ID, RunStatus.FAILED, stats=RunStats())
    spool: SqliteRowSpool | None = None
    checkpoint: TaskCheckpoint | None = None
    spool_closed = False
    keep_spool = True
    clear_checkpoint = False
    last_checkpoint_heartbeat = 0.0

    try:
        entries = parse_video_entries(txt_path)
        outcome.stats.input_count = len(entries)
        if not entries:
            outcome.errors.append(RunError("NO_VALID_INPUT", "TXT 中没有找到有效的 YouTube 视频链接。"))
            log_warn(log_callback, outcome.errors[-1].message)
            return outcome

        progress_total = len(entries) * (2 if get_comments_bool else 1) + 1
        if feedback is not None:
            feedback.set_total(progress_total, phase="视频详情与评论")
            feedback.set_activity(
                "正在准备采集任务",
                stop_reason="正在初始化本地恢复状态，请等待当前步骤完成。",
            )

        valid_line_count = int(entries[0].get("有效行数", len(entries)))
        duplicate_count = int(entries[0].get("重复行数", 0))
        log_line(log_callback, f"读取到 {valid_line_count} 行有效视频链接，去重后唯一视频 {len(entries)} 个，重复链接 {duplicate_count} 行。")
        video_ids = [str(entry["视频ID"]) for entry in entries]
        scope = build_video_metrics_scope(
            video_ids,
            fetch_shorts_related,
            live_stream_policy,
            get_comments,
            check_type,
            max_scan_comments,
            config,
        )
        existing_spool_path = video_metrics_spool_path(scope)
        checkpoint = open_task_checkpoint(VIDEO_METRICS_TOOL_ID, scope, log_callback)
        fingerprint = checkpoint.fingerprint
        concurrent_run = checkpoint.has_other_active_runs()
        if resume_action == "export":
            spool_path = existing_spool_path
        elif concurrent_run:
            spool_path = str(build_spool_path(VIDEO_METRICS_TOOL_ID, fingerprint, checkpoint.run_id[-8:]))
            log_line(log_callback, f"检测到相同任务正在其他窗口运行，本窗口使用独立缓存：{spool_path}")
        else:
            spool_path = existing_spool_path
        if resume_action == "restart":
            remove_spool_files(spool_path)
        if resume_action == "export" and os.path.exists(spool_path):
            partial_path = build_output_path(
                "youtube",
                f"youtube_video_metrics_partial_{time.strftime('%Y%m%d_%H%M%S')}.xlsx",
                channel="video_metrics",
            )
            if feedback is not None:
                feedback.set_progress(
                    0,
                    total=1,
                    activity="正在导出当前数据",
                    stop_reason="正在写入 XLSX 并等待文件原子替换完成。",
                )
            export_stats = export_spool_to_xlsx(spool_path, partial_path, lambda message: log_line(log_callback, message))
            if feedback is not None:
                feedback.advance(1, activity="当前数据导出完成", stop_reason="")
            outcome.status = RunStatus.PARTIAL
            outcome.output_path = partial_path
            outcome.stats.extra.update(export_stats)
            outcome.stats.success_count = min(
                outcome.stats.input_count,
                int(export_stats.get("stage_counts", {}).get("video", 0)),
            )
            outcome.artifacts.append(ArtifactRef(partial_path, "当前已采集数据"))
            outcome.artifacts.append(ArtifactRef(spool_path, "可恢复临时数据"))
            return outcome

        checkpoint.add_output_path(spool_path)

        def _heartbeat_checkpoint() -> None:
            nonlocal last_checkpoint_heartbeat
            now = time.monotonic()
            if checkpoint is not None and now - last_checkpoint_heartbeat >= 60.0:
                checkpoint.heartbeat()
                last_checkpoint_heartbeat = now

        sheets = [SpoolSheet("视频信息", VIDEO_FIELDS)]
        if get_comments_bool:
            sheets.append(SpoolSheet("评论信息", COMMENT_FIELDS))
        spool = SqliteRowSpool(spool_path, sheets, metadata={"tool_id": VIDEO_METRICS_TOOL_ID, "scope": scope})
        log_line(log_callback, f"持久化缓存已就绪：{spool_path}")
        completed_video_items = spool.completed_items("video")
        completed_video_ids = set(completed_video_items)
        completed_comment_ids_at_start = spool.completed_keys("comment") if get_comments_bool else set()
        if get_comments_bool:
            missing_excluded_comments = [
                video_id
                for video_id, meta in completed_video_items.items()
                if meta.get("excluded") and video_id not in completed_comment_ids_at_start
            ]
            if missing_excluded_comments:
                for chunk_start in range(0, len(missing_excluded_comments), 1000):
                    spool.submit_items(
                        [
                            ("comment", video_id, {}, {"status": "excluded"})
                            for video_id in missing_excluded_comments[chunk_start:chunk_start + 1000]
                        ]
                    )
                completed_comment_ids_at_start.update(missing_excluded_comments)
        if feedback is not None:
            feedback.set_progress(
                len(completed_video_ids) + len(completed_comment_ids_at_start),
                total=progress_total,
                activity="正在读取断点进度",
                stop_reason="正在读取本地 SQLite 恢复状态。",
            )
        pending_entries = [entry for entry in entries if str(entry["视频ID"]) not in completed_video_ids]
        if completed_video_ids:
            log_line(log_callback, f"断点续跑：视频阶段已完成 {len(completed_video_ids)} 条，本轮待处理 {len(pending_entries)} 条。")

        metrics_pool_local = threading.local()

        def _fetch_metrics_batch(batch_entries: list[dict]) -> tuple[list[dict], dict[str, dict]]:
            if should_stop(stop_event) or wait_if_paused(pause_event, stop_event):
                return batch_entries, {}
            pool = getattr(metrics_pool_local, "pool", None)
            if pool is None:
                pool = YouTubeClientPool(api_keys)
                metrics_pool_local.pool = pool
            ids = [str(entry["视频ID"]) for entry in batch_entries]
            if feedback is not None:
                feedback.set_activity(
                    "正在请求视频详情",
                    stop_reason="仍有已发送的 YouTube API 请求正在等待响应。",
                )
            metrics = fetch_video_metrics(
                pool,
                ids,
                live_stream_policy,
                log_callback=None,
                stop_event=stop_event,
                pause_event=pause_event,
            )
            return batch_entries, metrics

        video_written = len(completed_video_ids)

        def _persist_metrics_batch(batch_entries: list[dict], metrics: dict[str, dict]) -> None:
            nonlocal video_written
            ids = [str(entry["视频ID"]) for entry in batch_entries]
            type_map = {}
            if check_type_bool and ids and not should_stop(stop_event):
                if feedback is not None:
                    feedback.set_activity(
                        "正在检测视频类型",
                        stop_reason="仍有视频类型检测网络请求正在等待响应。",
                    )
                type_map = check_video_type_bulk(
                    ids,
                    max_workers=video_type_workers,
                    log_callback=log_callback,
                    stop_event=stop_event,
                    pause_event=pause_event,
                )
            operations = []
            processed_count = 0
            for entry in batch_entries:
                if should_stop(stop_event) or wait_if_paused(pause_event, stop_event):
                    break
                video_index = int(entry["编号"])
                video_id = str(entry["视频ID"])
                v_info = metrics.get(video_id)
                if v_info and v_info.get("is_excluded"):
                    operations.append(("video", video_id, {}, {"excluded": True, "index": video_index}))
                    if get_comments_bool:
                        operations.append(("comment", video_id, {}, {"status": "excluded"}))
                    processed_count += 1
                    continue
                if not v_info:
                    v_info = {
                        "标题": "[已删除或不可用]",
                        "频道名称": "",
                        "频道ID": "",
                        "发布日期": "",
                        "直播状态": "",
                        "视频时长": "",
                        "视频简介": "",
                        "播放量": "",
                        "点赞数": "",
                        "评论数": "",
                    }
                    detected_type = "已删除"
                elif check_type_bool:
                    detected_type = type_map.get(video_id, "未知")
                else:
                    detected_type = str(entry.get("预测类型", "视频"))
                final_video_url = build_video_url(video_id, detected_type)
                channel_id = v_info.get("频道ID", "")
                channel_url = f"https://www.youtube.com/channel/{channel_id}" if channel_id else ""
                related_title, related_link = "", ""
                if fetch_shorts_related_bool and detected_type == "Shorts":
                    if interruptible_sleep(shorts_related_delay, stop_event):
                        break
                    from src.platforms.youtube.shorts import fetch_short_related_video

                    related_title, related_link = fetch_short_related_video(video_id)
                row_video = {
                    "编号": str(video_index),
                    "视频链接": final_video_url,
                    "博主主页链接": channel_url,
                    "标题": v_info.get("标题", ""),
                    "频道名称": v_info.get("频道名称", ""),
                    "发布日期": format_youtube_datetime(v_info.get("发布日期", "")),
                    "视频类型": detected_type,
                    "直播状态": v_info.get("直播状态", ""),
                    "关联视频标题": related_title,
                    "关联视频链接": related_link,
                    "视频时长": v_info.get("视频时长", ""),
                    "视频简介": v_info.get("视频简介", ""),
                    "播放量": v_info.get("播放量", ""),
                    "点赞数": v_info.get("点赞数", ""),
                    "评论数": v_info.get("评论数", ""),
                }
                operations.append(
                    (
                        "video",
                        video_id,
                        {"视频信息": [SpoolRow(video_id, row_video, video_index, 0)]},
                        {"index": video_index, "comment_count": row_video["评论数"]},
                    )
                )
                if get_comments_bool and has_zero_comment_count(row_video["评论数"]):
                    placeholder = empty_video_row(video_index, final_video_url)
                    operations.append(
                        (
                            "comment",
                            video_id,
                            {"评论信息": [SpoolRow(f"{video_id}:placeholder", placeholder, video_index, 0)]},
                            {"status": "zero"},
                        )
                    )
                processed_count += 1
            if operations:
                if feedback is not None:
                    feedback.set_activity(
                        "正在写入视频进度",
                        stop_reason="正在等待本地 SQLite 事务提交完成。",
                    )
                spool.submit_items(operations)
                if feedback is not None:
                    feedback.advance(
                        len(operations),
                        activity="视频进度已写入",
                        stop_reason="",
                    )
                video_written += processed_count
                log_line(log_callback, f"视频阶段持久化进度：{video_written}/{len(entries)}。")
            _heartbeat_checkpoint()

        batches = [pending_entries[index:index + 50] for index in range(0, len(pending_entries), 50)]
        if batches:
            log_line(log_callback, f"正在获取视频详情：{len(pending_entries)} 条，{max_parallel_tabs} 路并发，每批 50 条。")
            with ThreadPoolExecutor(max_workers=max_parallel_tabs) as executor:
                batch_iterator = iter(batches)
                futures = {}
                for _ in range(min(len(batches), max_parallel_tabs * 2)):
                    batch = next(batch_iterator, None)
                    if batch is not None:
                        futures[executor.submit(_fetch_metrics_batch, batch)] = batch
                while futures and not should_stop(stop_event):
                    done, _ = wait(tuple(futures), return_when=FIRST_COMPLETED)
                    for future in done:
                        futures.pop(future, None)
                        batch_entries, metrics = future.result()
                        _persist_metrics_batch(batch_entries, metrics)
                        next_batch = next(batch_iterator, None)
                        if next_batch is not None and not should_stop(stop_event):
                            futures[executor.submit(_fetch_metrics_batch, next_batch)] = next_batch
                if should_stop(stop_event):
                    for future in futures:
                        future.cancel()

        if should_stop(stop_event):
            if feedback is not None:
                feedback.set_activity("任务已停止", stop_reason="")
            outcome.status = RunStatus.CANCELLED
            outcome.stats.success_count = len(spool.completed_keys("video"))
            outcome.stats.extra["persisted_video_rows"] = spool.row_count("视频信息")
            outcome.artifacts.append(ArtifactRef(spool_path, "可恢复临时数据"))
            log_line(log_callback, "任务已停止，SQLite 进度已保留；再次启动相同任务可继续或导出。")
            return outcome

        quota_stopped = False
        comment_failures = 0
        if get_comments_bool:
            completed_comment_ids = spool.completed_keys("comment")

            def _iter_comment_tasks():
                for video_id, row, _item_order, _row_order in spool.iter_rows("视频信息"):
                    if video_id in completed_comment_ids or has_zero_comment_count(row.get("评论数")):
                        continue
                    yield CommentFetchTask(video_id, str(row.get("视频链接", "")), str(row.get("编号", "")))

            pending_comment_count = sum(1 for _ in _iter_comment_tasks())
            if completed_comment_ids:
                log_line(log_callback, f"断点续跑：评论阶段已完成 {len(completed_comment_ids)} 个视频，本轮待处理 {pending_comment_count} 个。")

            def _write_comment_result(task: CommentFetchTask, result: CommentFetchResult) -> bool:
                nonlocal quota_stopped, comment_failures
                if result.status == "stopped":
                    return False
                if result.status == "error" and is_comment_quota_error_message(result.error, result.http_status):
                    quota_stopped = True
                    log_error(log_callback, f"停止评论采集：API 配额/限流不可用 ({result.error})")
                    return False
                if result.status == "error":
                    comment_failures += 1
                    log_warn(log_callback, f"评论获取失败 ({task.video_id})：{result.error}，写入空占位行。")
                rows = build_comment_rows(task.index, task.video_url, result.comments, top_comment_limit)
                comments = result.comments[: len(rows)]
                if not rows:
                    rows = [empty_video_row(int(task.index or 0), task.video_url)]
                    spool_rows = [SpoolRow(f"{task.video_id}:placeholder", rows[0], int(task.index or 0), 0)]
                else:
                    spool_rows = [
                        SpoolRow(
                            _comment_spool_key(task.video_id, comments[index], index),
                            row,
                            int(task.index or 0),
                            index,
                        )
                        for index, row in enumerate(rows)
                    ]
                if feedback is not None:
                    feedback.set_activity(
                        "正在写入评论进度",
                        stop_reason="正在等待本地 SQLite 事务提交完成。",
                    )
                spool.submit_item(
                    "comment",
                    task.video_id,
                    {"评论信息": spool_rows},
                    {"status": result.status, "row_count": len(spool_rows)},
                )
                if feedback is not None:
                    feedback.advance(
                        1,
                        activity="正在获取评论",
                        stop_reason="仍有已发送的 YouTube 评论请求正在等待响应。",
                    )
                _heartbeat_checkpoint()
                return True

            if pending_comment_count:
                if feedback is not None:
                    feedback.set_activity(
                        "正在获取评论",
                        stop_reason="仍有已发送的 YouTube 评论请求正在等待响应。",
                    )
                fetch_top_comments_for_videos(
                    api_keys,
                    _iter_comment_tasks(),
                    max_scan_comments,
                    top_comment_limit,
                    comment_mode,
                    comment_workers,
                    log_callback,
                    stop_event,
                    pause_event,
                    api_page_size,
                    result_callback=_write_comment_result,
                    retain_results=False,
                    queue_capacity=16,
                    total_tasks=pending_comment_count,
                )

        if should_stop(stop_event):
            if feedback is not None:
                feedback.set_activity("任务已停止", stop_reason="")
            outcome.status = RunStatus.CANCELLED
            completed_stage = "comment" if get_comments_bool else "video"
            outcome.stats.success_count = len(spool.completed_keys(completed_stage))
            outcome.stats.extra["persisted_video_rows"] = spool.row_count("视频信息")
            if get_comments_bool:
                outcome.stats.extra["persisted_comment_rows"] = spool.row_count("评论信息")
            outcome.artifacts.append(ArtifactRef(spool_path, "可恢复临时数据"))
            log_line(log_callback, "任务已停止，SQLite 进度已保留；再次启动相同任务可继续或导出。")
            return outcome
        if quota_stopped:
            if feedback is not None:
                feedback.set_activity("评论采集已中止，进度已保留", stop_reason="")
            outcome.status = RunStatus.PARTIAL
            outcome.stats.success_count = len(spool.completed_keys("comment"))
            outcome.stats.extra["persisted_video_rows"] = spool.row_count("视频信息")
            outcome.stats.extra["persisted_comment_rows"] = spool.row_count("评论信息")
            outcome.errors.append(RunError("YOUTUBE_QUOTA_STOPPED", "API 配额或限流不可用，任务进度已保留。"))
            outcome.artifacts.append(ArtifactRef(spool_path, "可恢复临时数据"))
            return outcome

        spool.close()
        spool_closed = True
        if feedback is not None:
            feedback.set_activity(
                "正在导出 XLSX",
                stop_reason="正在写入 XLSX 并等待文件原子替换完成。",
            )
        export_stats = export_spool_to_xlsx(spool_path, output_path, lambda message: log_line(log_callback, message))
        if feedback is not None:
            feedback.advance(1, activity="导出完成", stop_reason="")
        outcome.status = RunStatus.PARTIAL if comment_failures else RunStatus.SUCCEEDED
        outcome.output_path = output_path
        outcome.stats.success_count = len(entries) - comment_failures
        outcome.stats.failed_count = comment_failures
        outcome.stats.extra.update(export_stats)
        outcome.artifacts.append(ArtifactRef(output_path, "YouTube 视频与评论数据"))
        spool_removed = remove_spool_files(spool_path)
        keep_spool = not spool_removed
        clear_checkpoint = spool_removed
        if not spool_removed:
            log_warn(log_callback, f"结果已导出，但临时 SQLite 暂时无法删除：{spool_path}")
            outcome.artifacts.append(ArtifactRef(spool_path, "待清理临时数据"))
        log_line(log_callback, f"完成，已保存：{output_path}")
        return outcome
    except Exception as exc:
        log_error(log_callback, f"运行失败：{exc}")
        recoverable = spool is not None and is_recoverable_youtube_run_error(exc)
        outcome.status = RunStatus.PARTIAL if recoverable else RunStatus.FAILED
        error_code = "YOUTUBE_RECOVERABLE_STOPPED" if recoverable else "VIDEO_METRICS_FAILED"
        outcome.errors.append(RunError(error_code, str(exc)))
        if spool is not None:
            outcome.artifacts.append(ArtifactRef(str(spool.path), "可恢复临时数据"))
        return outcome
    finally:
        if spool is not None and not spool_closed:
            try:
                if feedback is not None:
                    feedback.set_activity(
                        "正在安全关闭本地缓存",
                        stop_reason="正在等待已进入队列的 SQLite 写入完成并安全关闭数据库。",
                    )
                spool.close()
                if feedback is not None and should_stop(stop_event):
                    feedback.set_activity("任务已停止", stop_reason="")
            except Exception as exc:
                outcome.errors.append(RunError("SPOOL_CLOSE_FAILED", str(exc)))
        if not keep_spool and spool is not None:
            remove_spool_files(spool.path)
        if checkpoint is not None:
            checkpoint.close_run()
            if clear_checkpoint:
                checkpoint.delete_if_inactive()
        try:
            outcome.artifacts.append(ArtifactRef(report_path, "任务报告"))
            outcome.save_to_json(report_path)
        except Exception as exc:
            outcome.artifacts = [artifact for artifact in outcome.artifacts if artifact.path != report_path]
            outcome.errors.append(RunError("REPORT_WRITE_FAILED", str(exc)))
        if finish_callback:
            finish_callback(outcome)
