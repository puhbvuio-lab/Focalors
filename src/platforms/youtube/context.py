# -*- coding: utf-8 -*-
"""YouTube 视频上下文获取与对比模块。

本模块根据输入的“目标视频 + 频道主页”配对数据，利用 YouTube API 自动加载该频道的
最新上传列表（uploads），并定位该目标视频在发布时间轴上的物理索引，进而提取出该视频
发布之前后各 N 个视频的相关元数据（标题、发布时间、点赞数、播放量等）。
"""

from __future__ import annotations

import re
import time
from urllib.parse import urlparse

from googleapiclient.errors import HttpError
from src.platforms.youtube.keyword import YouTubeClientPool, execute_with_retry
from src.platforms.youtube.comments import build_video_url
from src.platforms.youtube.video_type import UNKNOWN, check_video_type_bulk, normalize_video_type_workers

from src.core import XlsxRowWriter, build_output_path, feedback_from_config, log_error, log_line, log_warn, sanitize_csv_rows, should_stop, wait_if_paused

# 默认获取视频前后各 5 条作为上下文
CONTEXT_SIZE = 5
# 用于匹配 11 位 YouTube 视频 ID 的正则表达式
VIDEO_ID_RE = re.compile(r"(?:v=|youtu\.be/|/shorts/|/embed/)([0-9A-Za-z_-]{11})")


def parse_video_id(url: str) -> str:
    """从输入字串或链接中提取 YouTube 11 位视频 ID。"""
    match = VIDEO_ID_RE.search(url or "")
    if match:
        return match.group(1)
    if re.fullmatch(r"[0-9A-Za-z_-]{11}", (url or "").strip()):
        return url.strip()
    return ""


def parse_input_pairs(txt_path: str) -> list[tuple[str, str]]:
    """从 TXT 文本中分行读取“目标视频链接 + 频道主页链接”的配对，支持 Tab 或空格分隔。"""
    pairs: list[tuple[str, str]] = []
    with open(txt_path, "r", encoding="utf-8-sig") as f:
        for line in f:
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            # 优先采用 Tab 分隔符，其次使用空白符切割
            parts = [part.strip() for part in stripped.split("\t") if part.strip()] if "\t" in stripped else stripped.split()
            if len(parts) >= 2:
                pairs.append((parts[0], parts[1]))
    return pairs


def normalize_youtube_url(url: str) -> str:
    """补全并规整化 YouTube URL。"""
    url = (url or "").strip()
    if not url:
        return ""
    if url.startswith("//"):
        return "https:" + url
    if not url.startswith("http"):
        return "https://" + url
    return url


def relation_for_index(target_index: int, current_index: int) -> str:
    """根据目标索引和当前索引的差值，计算该视频在发布时间轴上的物理相对关系（前或后）。

    发布顺序是越新发布的视频越在列表前面（索引越小表示发布越新）。

    Args:
        target_index: 目标视频的物理索引。
        current_index: 当前被比对视频的物理索引。

    Returns:
        str: 时间轴关系的中文描述。
    """
    if current_index < target_index:
        return f"目标后发布第{target_index - current_index}条"
    return f"目标前发布第{current_index - target_index}条"


def extract_channel_hint(profile_url: str) -> tuple[str, str]:
    """解析 YouTube 博主主页 URL，确定其频道检索类型（id/username/handle/search）。"""
    normalized = normalize_youtube_url(profile_url)
    parsed = urlparse(normalized)
    path_parts = [part for part in parsed.path.split("/") if part]
    if not path_parts:
        return "", ""

    first = path_parts[0]
    if first == "channel" and len(path_parts) >= 2:
        return "id", path_parts[1]
    if first == "user" and len(path_parts) >= 2:
        return "username", path_parts[1]
    if first.startswith("@"):
        return "handle", first[1:]
    if first in {"c", "custom"} and len(path_parts) >= 2:
        return "search", path_parts[1]
    return "search", first.lstrip("@")


def resolve_channel(client_pool, profile_url: str, log_callback=None) -> dict:
    """调用 API 解析获取博主的频道信息（snippet 及 contentDetails 的上传播放列表 ID）。"""
    hint_type, hint_value = extract_channel_hint(profile_url)
    if not hint_value:
        return {}

    def _execute_req(build_req):
        while True:
            try:
                return execute_with_retry(build_req(), log_callback)
            except HttpError as e:
                if e.resp.status in [403, 429]:
                    if client_pool.next_client():
                        continue
                raise e

    try:
        if hint_type == "id":
            res = _execute_req(lambda: client_pool.client.channels().list(part="snippet,contentDetails", id=hint_value))
        elif hint_type == "username":
            res = _execute_req(lambda: client_pool.client.channels().list(part="snippet,contentDetails", forUsername=hint_value))
        elif hint_type == "handle":
            res = {"items": []}
            handle_variants = []
            clean_handle = hint_value.lstrip("@")
            handle_variants.append(f"@{clean_handle}")
            handle_variants.append(clean_handle)
            for handle in handle_variants:
                try:
                    res = _execute_req(lambda h=handle: client_pool.client.channels().list(part="snippet,contentDetails", forHandle=h))
                except TypeError:
                    res = {"items": []}
                if res.get("items"):
                    break
        else:
            res = {"items": []}
    except HttpError:
        raise
    except Exception:
        res = {"items": []}

    items = res.get("items", [])
    return items[0] if items else {}


def resolve_channel_from_video(client_pool, video_id: str, log_callback=None) -> dict:
    """兜底降级方法：当通过频道主页 URL 无法解析时，通过视频 ID 直接反查其所属频道并获取上传播放列表 ID。"""
    def _execute_req(build_req):
        while True:
            try:
                return execute_with_retry(build_req(), log_callback)
            except HttpError as e:
                if e.resp.status in [403, 429]:
                    if client_pool.next_client():
                        continue
                raise e

    res = _execute_req(lambda: client_pool.client.videos().list(part="snippet", id=video_id, maxResults=1))
    items = res.get("items", [])
    if not items:
        return {}
    channel_id = items[0].get("snippet", {}).get("channelId", "")
    if not channel_id:
        return {}
    channel_res = _execute_req(lambda: client_pool.client.channels().list(part="snippet,contentDetails", id=channel_id))
    channel_items = channel_res.get("items", [])
    return channel_items[0] if channel_items else {}


def find_context_video_ids(client_pool, uploads_playlist_id: str, target_video_id: str, stop_event=None, pause_event=None, max_pages: int = 200, context_size: int = CONTEXT_SIZE, log_callback=None) -> tuple[list[str], int, list[str]]:
    """分页拉取频道上传播放列表，定位目标视频，并过滤出前后各 N 个视频的 ID。

    Args:
        youtube: API 客户端。
        uploads_playlist_id: 上传列表（uploads）播放列表的唯一 ID。
        target_video_id: 目标视频唯一 ID。
        stop_event: 停止信号。
        pause_event: 暂停信号。
        max_pages: 最大拉取分页数。
        context_size: 视频前后截取区间大小。

    Returns:
        tuple: (选定的上下文视频 ID 列表, 目标视频在全列表的发布索引, 完整拉取的视频 ID 轴, 选定视频对应的物理索引列表)。
    """
    video_ids: list[str] = []
    next_page_token = None
    page_count = 0

    while page_count < max_pages:
        page_count += 1
        if should_stop(stop_event):
            return [], -1, video_ids, []
        if wait_if_paused(pause_event, stop_event):
            return [], -1, video_ids, []
        
        # 批量获取播放列表内容项
        while True:
            try:
                res = execute_with_retry(
                    client_pool.client.playlistItems().list(
                        part="contentDetails",
                        playlistId=uploads_playlist_id,
                        maxResults=50,
                        pageToken=next_page_token,
                    ), log_callback
                )
                break
            except HttpError as e:
                if e.resp.status in [403, 429]:
                    if client_pool.next_client():
                        continue
                raise e

        for item in res.get("items", []):
            vid = item.get("contentDetails", {}).get("videoId", "")
            if vid:
                video_ids.append(vid)

        # 一旦发现列表里包含了目标视频，且列表总数已足够覆盖目标右侧（后发布）的上下文，即可安全停止分页
        if target_video_id in video_ids:
            target_index = video_ids.index(target_video_id)
            if len(video_ids) >= target_index + context_size + 1:
                break

        next_page_token = res.get("nextPageToken")
        if not next_page_token:
            break

    if target_video_id not in video_ids:
        return [], -1, video_ids, []

    # 切分出前后各 N 个视频 ID
    target_index = video_ids.index(target_video_id)
    selected_indices = list(range(max(0, target_index - context_size), target_index))
    selected_indices += list(range(target_index + 1, min(len(video_ids), target_index + context_size + 1)))
    return [video_ids[idx] for idx in selected_indices], target_index, video_ids, selected_indices


def fetch_video_details(client_pool, video_ids: list[str], stop_event=None, pause_event=None, log_callback=None) -> dict[str, dict]:
    """批量获取指定视频列表的精细元数据。"""
    details: dict[str, dict] = {}
    from src.platforms.youtube.keyword import format_youtube_duration as kw_format
    
    for start in range(0, len(video_ids), 50):
        if should_stop(stop_event):
            break
        if wait_if_paused(pause_event, stop_event):
            break
        chunk = video_ids[start:start + 50]
        if not chunk:
            continue
        while True:
            try:
                res = execute_with_retry(
                    client_pool.client.videos().list(
                        part="snippet,statistics,contentDetails",
                        id=",".join(chunk),
                        maxResults=50,
                    ), log_callback
                )
                break
            except HttpError as e:
                if e.resp.status in [403, 429]:
                    if client_pool.next_client():
                        continue
                raise e
        for item in res.get("items", []):
            stats = item.get("statistics", {})
            snippet = item.get("snippet", {})
            content = item.get("contentDetails", {})
            desc = (snippet.get("description") or "").replace("\n", " | ").replace("\r", "")
            if len(desc) > 300:
                desc = desc[:300] + "..."
                
            details[item["id"]] = {
                "title": snippet.get("title", ""),
                "published_at": snippet.get("publishedAt", ""),
                "channel_title": snippet.get("channelTitle", ""),
                "channel_id": snippet.get("channelId", ""),
                "duration": kw_format(content.get("duration", "")),
                "description": desc,
                "view_count": stats.get("viewCount", ""),
                "like_count": stats.get("likeCount", ""),
                "comment_count": stats.get("commentCount", ""),
            }
    return details


# 导出 Excel 数据表字段定义
OUTPUT_FIELDS = [
    "博主链接",
    "目标视频链接",
    "视频链接",
    "博主主页链接",
    "时间轴关系",
    "标题",
    "视频标题",
    "频道名称",
    "发布日期",
    "发布时间",
    "视频类型",
    "视频时长",
    "视频简介",
    "播放量",
    "点赞数",
    "评论数",
    "视频ID",
]


def build_pair_rows(client_pool, target_video_url: str, profile_url: str, channel_cache: dict[str, dict], log_callback, stop_event=None, pause_event=None, context_size: int = CONTEXT_SIZE, max_upload_pages: int = 200, check_video_type_bool: bool = True, video_type_workers: int = 2) -> list[dict]:
    """针对单对“目标视频 + 博主主页”解析其前后关联的上下文数据行。"""
    rows: list[dict] = []
    target_video_id = parse_video_id(target_video_url)
    if not target_video_id:
        log_line(log_callback, "  跳过：无法解析视频 ID。")
        return rows

    # 通过内存字典缓存已解析的博主频道数据，减少重复 API 配额消耗
    channel = channel_cache.get(profile_url)
    if channel is None:
        channel = resolve_channel(client_pool, profile_url, log_callback)
        # 仅成功才写缓存。失败时不缓存空 dict，否则 `is None` 判断永久失效，
        # 导致后续相同 profile_url 的配对反复触发昂贵的视频反查。
        if channel:
            channel_cache[profile_url] = channel

    uploads_id = channel.get("contentDetails", {}).get("relatedPlaylists", {}).get("uploads", "")
    if not uploads_id:
        log_line(log_callback, "  博主主页解析失败，改用目标视频反查频道上传列表。")
        channel = resolve_channel_from_video(client_pool, target_video_id, log_callback)
        if channel:
            channel_cache[profile_url] = channel
        uploads_id = channel.get("contentDetails", {}).get("relatedPlaylists", {}).get("uploads", "")
        if not uploads_id:
            log_line(log_callback, "  跳过：无法解析上传列表。请检查博主主页链接是否为 YouTube 频道主页。")
            return rows

    selected_ids, target_index, timeline_ids, selected_indices = find_context_video_ids(client_pool, uploads_id, target_video_id, stop_event, pause_event, max_upload_pages, context_size, log_callback)
    if should_stop(stop_event):
        return rows
    if target_index < 0:
        log_line(log_callback, "  跳过：目标视频不在该博主公开上传列表中。")
        return rows

    # 批量解析抓取的上下文视频 ID 指标
    details = fetch_video_details(client_pool, selected_ids, stop_event, pause_event, log_callback)
    type_map = {}
    if check_video_type_bool and selected_ids:
        normalized_workers = normalize_video_type_workers(video_type_workers)
        log_line(log_callback, f"  使用统一 HEAD/重定向逻辑验证 {len(selected_ids)} 个上下文视频的长短类型，并发 {normalized_workers}...")
        type_map = check_video_type_bulk(selected_ids, max_workers=normalized_workers,
                                           log_callback=log_callback, stop_event=stop_event, pause_event=pause_event)
    from src.platforms.youtube.comments import format_youtube_datetime
    for vid, current_index in zip(selected_ids, selected_indices):
        item = details.get(vid, {})
        channel_id = item.get("channel_id", "")
        channel_url = f"https://www.youtube.com/channel/{channel_id}" if channel_id else ""
        
        final_pub_date = format_youtube_datetime(item.get("published_at", ""))
        
        rows.append({
            "博主链接": profile_url,
            "目标视频链接": target_video_url,
            "视频链接": build_video_url(vid, type_map.get(vid, UNKNOWN)),
            "博主主页链接": channel_url,
            "时间轴关系": relation_for_index(target_index, current_index),
            "标题": item.get("title", ""),
            "视频标题": item.get("title", ""),
            "频道名称": item.get("channel_title", ""),
            "发布日期": final_pub_date,
            "发布时间": final_pub_date,
            "视频类型": type_map.get(vid, ""),
            "视频时长": item.get("duration", ""),
            "视频简介": item.get("description", ""),
            "播放量": item.get("view_count", ""),
            "点赞数": item.get("like_count", ""),
            "评论数": item.get("comment_count", ""),
            "视频ID": vid,
        })
    return rows


def run_youtube_paired_context_spider(api_keys: list[str], txt_path: str, log_callback, finish_callback, stop_event=None, config=None, pause_event=None):
    """运行 YouTube 关联上下文视频挖掘主驱动函数。

    Args:
        api_key: API 密钥。
        txt_path: 视频 + 主页配对 TXT 路径。
        log_callback: 日志通知。
        finish_callback: 结束通知。
        stop_event: 中断信号。
        config: 特殊配置项。
        pause_event: 暂停信号。
    """
    if config is None:
        config = {}
    context_size = int(config.get("context_size", CONTEXT_SIZE))
    max_upload_pages = int(config.get("max_upload_pages", 200))
    check_video_type_bool = config.get("check_video_type", "是") == "是"
    video_type_workers = normalize_video_type_workers(config.get("youtube_video_type_workers"))
    feedback = feedback_from_config(config)

    output_path = None
    try:
        pairs = parse_input_pairs(txt_path)
        if not pairs:
            log_warn(log_callback, "TXT 中没有有效的'视频链接 + 博主主页链接'行。")
            return
        if feedback is not None:
            feedback.set_total(len(pairs) + 1, phase="视频上下文采集")
            feedback.set_activity(
                "正在准备上下文任务",
                stop_reason="正在初始化 YouTube API 客户端。",
            )
        if should_stop(stop_event):
            log_line(log_callback, "任务已停止。")
            return
        output_path = build_output_path("youtube", f"youtube_context_{time.strftime('%Y%m%d_%H%M%S')}.xlsx", channel="context")
        writer = XlsxRowWriter(output_path, OUTPUT_FIELDS)
        client_pool = YouTubeClientPool(api_keys)
        
        max_parallel_tabs = int(config.get("max_parallel_tabs", 3)) if config else 3
        import threading
        from concurrent.futures import ThreadPoolExecutor, as_completed

        class ThreadSafeDict(dict):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self._lock = threading.Lock()
            def get(self, key, default=None):
                with self._lock:
                    return super().get(key, default)
            def __setitem__(self, key, value):
                with self._lock:
                    super().__setitem__(key, value)
            def __getitem__(self, key):
                with self._lock:
                    return super().__getitem__(key)

        channel_cache = ThreadSafeDict()
        writer_lock = threading.Lock()
        log_lock = threading.Lock()
        written_count = [0]

        def make_thread_log(base_log_callback, lock, prefix):
            def wrapped(msg):
                if base_log_callback:
                    with lock:
                        base_log_callback(f"[{prefix}] {msg}")
            return wrapped

        def worker(index, target_video_url, profile_url):
            if should_stop(stop_event):
                return
            if wait_if_paused(pause_event, stop_event):
                return

            thread_log = make_thread_log(log_callback, log_lock, f"对 {index}")
            thread_log(f"定位 YouTube 目标视频: {target_video_url}")
            if feedback is not None:
                feedback.set_activity(
                    f"正在处理视频对 {index}/{len(pairs)}",
                    stop_reason="仍有已发送的 YouTube API 请求正在等待响应。",
                )
            try:
                rows = build_pair_rows(client_pool, target_video_url, profile_url, channel_cache, thread_log, stop_event, pause_event, context_size, max_upload_pages, check_video_type_bool, video_type_workers)
                if rows:
                    with writer_lock:
                        if feedback is not None:
                            feedback.set_activity(
                                f"正在写入视频对 {index} 的结果",
                                stop_reason="正在等待 XLSX 数据写入完成。",
                            )
                        writer.writerows(sanitize_csv_rows(rows))
                        written_count[0] += len(rows)
                    thread_log(f"完成：写入 {len(rows)} 条前后视频，当前累计 {written_count[0]} 条。")
                else:
                    thread_log("未获取到前后视频数据。")
            except HttpError as e:
                thread_log(f"[ERROR] YouTube API 错误：{e}")
            except Exception as e:
                thread_log(f"[ERROR] 处理失败：{e}")

        with ThreadPoolExecutor(max_workers=max_parallel_tabs) as executor:
            futures = [executor.submit(worker, idx, url, p_url) for idx, (url, p_url) in enumerate(pairs, 1)]
            for future in as_completed(futures):
                if should_stop(stop_event):
                    for f in futures:
                        f.cancel()
                    break
                try:
                    future.result()
                except Exception as exc:
                    log_error(log_callback, f"线程执行异常: {exc}")
                if feedback is not None:
                    feedback.advance(1, activity="已完成一个视频对处理单元", stop_reason="")

        with writer_lock:
            if feedback is not None:
                feedback.set_activity(
                    "正在保存上下文 XLSX",
                    stop_reason="正在等待工作簿保存完成。",
                )
            writer.save()
        if feedback is not None:
            feedback.advance(1, activity="视频上下文导出完成", stop_reason="")

        if written_count[0] <= 0:
            log_warn(log_callback, "没有提取到数据。")
        log_line(log_callback, f"完成，已保存：{output_path}")
    except Exception as exc:
        log_error(log_callback, f"运行失败：{exc}")
        output_path = None
    finally:
        if feedback is not None and should_stop(stop_event):
            feedback.set_activity("任务已停止", stop_reason="")
        finish_callback(output_path)
