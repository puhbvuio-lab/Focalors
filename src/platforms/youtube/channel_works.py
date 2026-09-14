"""
YouTube 频道作品采集模块。
该模块包含以下核心功能：
1. API 模式采集：在提供有效的 API Key 时，优先使用 YouTube Data API v3 接口，通过频道的 uploads 播放列表批量、快速、低额度消耗地提取视频/Shorts作品。
2. 浏览器 Fallback 模式：当 API Key 无效、超限或不可用时，降级使用 Playwright 浏览器接管本地 Chrome 并滚动页面解析 DOM 元素。
3. 帖子（Posts）采集：因为 API 的上传列表中不含帖子，故统一使用 Playwright 模拟滚动和 DOM 抓取。
4. 评论信息抓取：若用户选择采集评论，可在作品拉取完成后，利用 API 对所有提取到的视频/Shorts 批量拉取一级评论并按点赞量降序保存。
"""

from __future__ import annotations

import hashlib
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse

# 尝试导入 Google API Client，如果环境未安装则标记为 None（后续降级到浏览器提取）
try:
    from googleapiclient.discovery import build
    from src.platforms.youtube.keyword import YouTubeClientPool, execute_with_retry
    from googleapiclient.errors import HttpError
except ModuleNotFoundError:
    build = None

# 尝试导入 Playwright 同步 API，如未安装则设置 Fallback
try:
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
    from playwright.sync_api import sync_playwright
except ModuleNotFoundError:
    PlaywrightTimeoutError = TimeoutError
    sync_playwright = None

from src.core import (
    DEFAULT_X_CDP_URL,
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
    connect_existing_chromium,
    export_spool_to_xlsx,
    feedback_from_config,
    find_latest_spool_path,
    generate_run_id,
    interruptible_sleep,
    log_error,
    log_line,
    log_warn,
    remove_spool_files,
    sanitize_csv_cell,
    should_stop,
    wait_if_paused,
)
from src.core.task_checkpoint import TaskCheckpoint, open_task_checkpoint, task_fingerprint
from src.platforms.youtube.comments import (
    COMMENT_MODE_FAST,
    DEFAULT_COMMENT_WORKERS,
    CommentFetchTask,
    fetch_top_comments_for_videos,
    has_zero_comment_count,
    is_comment_quota_error_message,
    is_recoverable_youtube_run_error,
    normalize_comment_mode,
    normalize_comment_workers,
)
from src.platforms.youtube.keyword import parse_date_range
from src.platforms.youtube.video_type import NORMAL_VIDEO, UNKNOWN, check_video_type_bulk, normalize_video_type_workers

# 导出到 Excel 中的表格头部字段定义
CSV_FIELDS = [
    "序号",
    "编号",
    "视频链接",
    "作品链接",
    "博主主页链接",
    "作者主页链接",
    "标题",
    "作品内容",
    "频道名称",
    "发布日期",
    "视频类型",
    "直播状态",
    "关联视频标题",
    "关联视频链接",
    "视频时长",
    "视频简介",
    "播放量",
    "浏览量",
    "点赞数",
    "评论数",
]

# 浏览器爬虫相关的延迟与限制常量
PAGE_LOAD_TIMEOUT = 45000       # 页面最大加载超时（毫秒）
INITIAL_LOAD_DELAY = 1.8        # 页面加载后初次等待渲染延迟（秒）
POST_SCROLL_DELAY = 0.8         # 每次滚动后的页面稳定等待时间（秒）
POST_SCROLL_PX = 2800           # 每次滚动的垂直像素高度
NO_NEW_POST_LIMIT = 6           # 连续多少次滚动无新增内容则视作到底部并停止
DEFAULT_MAX_POST_SCROLLS = 120  # 默认最大滚动次数
DEFAULT_MAX_VIDEO_ITEMS = 500   # 默认 API/浏览器提取视频的最大条数限制
CHANNEL_WORKS_TOOL_ID = "youtube_channel_works"
CHANNEL_WORKS_STORAGE_VERSION = 2


def clean_channel_url(url: str) -> str:
    """
    清理并规范化 YouTube 频道主页 URL。
    支持补全协议头、自动处理以斜杠开头的路径，
    并剔除 URL 尾部的 /videos、/shorts、/posts 等子路径或查询参数，返回干净的主页根链接。
    """
    value = (url or "").strip()
    if not value:
        return ""
    if value.startswith("//"):
        value = "https:" + value
    if value.startswith("/"):
        value = "https://www.youtube.com" + value
    if not value.startswith("http"):
        value = "https://" + value
    value = value.split("?")[0].split("#")[0].rstrip("/")
    # 正则去除尾部常见的标签页路径
    value = re.sub(r"/(videos|shorts|posts|community|featured)$", "", value, flags=re.I)
    return value


def parse_channel_urls(text: str) -> list[str]:
    """
    从文本框输入的字符串中解析出所有合法的 YouTube 频道链接。
    过滤掉以 # 开头的注释行、空行，对链接进行净化后进行去重。
    """
    urls: list[str] = []
    seen = set()
    for line in (text or "").splitlines():
        stripped = line.strip()
        # 跳过空行和以 # 开头的注释行
        if not stripped or stripped.startswith("#"):
            continue
        url = clean_channel_url(stripped.split()[0])
        if "youtube.com/" in url and url not in seen:
            urls.append(url)
            seen.add(url)
    return urls


def parse_channel_url(url: str) -> tuple[str, str]:
    """
    分析 YouTube 频道链接的路径结构，提取识别频道的 hint 类型及值。
    - /channel/ID -> 返回 ("id", ID)
    - /user/Name -> 返回 ("username", Name)
    - 以 @ 开头的 Handle -> 返回 ("handle", @Handle)
    - 其它（如 /c/ 或 /custom/） -> 返回 ("search", Name) 以进行 API 检索
    """
    normalized = clean_channel_url(url)
    parsed = urlparse(normalized)
    parts = [part for part in parsed.path.split("/") if part]
    if not parts:
        return "", ""

    first = parts[0]
    if first == "channel" and len(parts) >= 2:
        return "id", parts[1]
    if first == "user" and len(parts) >= 2:
        return "username", parts[1]
    if first.startswith("@"):
        return "handle", first
    if first in {"c", "custom"} and len(parts) >= 2:
        return "search", parts[1]
    return "search", first.lstrip("@")


def posts_url(channel_url: str) -> str:
    """
    拼接频道的社区帖子（Posts）标签页完整链接。
    """
    return f"{clean_channel_url(channel_url)}/posts"


def normalize_youtube_href(href: str) -> str:
    """
    标准化视频或 Shorts 的跳转 URL。
    去除推荐参数 &pp= 等，并统一提取成标准的 watch?v= 视频地址或 /shorts/ 视频地址。
    """
    value = (href or "").strip()
    if not value:
        return ""
    if value.startswith("//"):
        value = "https:" + value
    if value.startswith("/"):
        value = "https://www.youtube.com" + value
    value = value.split("&pp=")[0].split("?pp=")[0]
    watch_match = re.search(r"(https://www\.youtube\.com/watch\?v=[\w-]+)", value)
    if watch_match:
        return watch_match.group(1)
    shorts_match = re.search(r"(https://www\.youtube\.com/shorts/[\w-]+)", value)
    if shorts_match:
        return shorts_match.group(1)
    return value


def normalize_metric_text(text: str) -> str:
    """
    提取统计数据（如播放量、点赞量等）文本中的数字及数量单位（如 K, M, 万, 亿 等）。
    """
    value = re.sub(r"\s+", " ", text or "").strip()
    if not value:
        return ""
    match = re.search(r"(\d[\d,.]*(?:\.\d+)?\s*(?:K|M|B|万|萬|亿|億)?)", value, flags=re.I)
    return match.group(1).strip() if match else ""


def tab_url(channel_url: str, tab: str) -> str:
    """
    拼接指定标签页（如 videos、shorts）的完整链接。
    """
    return f"{clean_channel_url(channel_url)}/{tab}"


def chunked(values: list[str], size: int) -> list[list[str]]:
    """
    辅助函数：按指定大小 size 将列表 values 分割为多个子列表。
    """
    return [values[index:index + size] for index in range(0, len(values), size)]



def fetch_channel_item(client_pool, channel_url: str) -> dict:
    """
    使用 YouTube Data API v3 获取频道的详情信息。
    根据 parse_channel_url 返回的定位类型，依次调用 channels().list，
    提取频道关联的 contentDetails (包含 uploads 上传播放列表 ID)。
    若需要搜索定位，则调用 search().list 找出频道 ID 后再查询详情。
    """
    hint_type, hint_value = parse_channel_url(channel_url)
    if not hint_value:
        return {}

    def _execute_req(build_req):
        while True:
            try:
                return execute_with_retry(build_req(), None)
            except HttpError as e:
                if e.resp.status in [403, 429]:
                    if client_pool.next_client():
                        continue
                raise e

    if hint_type == "id":
        response = _execute_req(lambda: client_pool.client.channels().list(part="snippet,contentDetails", id=hint_value))
    elif hint_type == "username":
        response = _execute_req(lambda: client_pool.client.channels().list(part="snippet,contentDetails", forUsername=hint_value))
    elif hint_type == "handle":
        try:
            response = _execute_req(lambda: client_pool.client.channels().list(part="snippet,contentDetails", forHandle=hint_value))
        except TypeError:
            # 旧版 google-api-python-client 不支持 forHandle 关键字
            response = {"items": []}
    else:
        # 自定义名或未定结构采用搜索接口进行模糊查找
        search_response = _execute_req(lambda: client_pool.client.search().list(part="id", q=hint_value, type="channel", maxResults=1))
        items = search_response.get("items", [])
        channel_id = items[0].get("id", {}).get("channelId", "") if items else ""
        if not channel_id:
            return {}
        response = _execute_req(lambda: client_pool.client.channels().list(part="snippet,contentDetails", id=channel_id))

    items = response.get("items", [])
    return items[0] if items else {}



def collect_upload_video_ids(client_pool, uploads_playlist_id: str, max_video_items: int, limit_time_bool: bool, start_dt, end_dt, log_callback, stop_event=None, pause_event=None) -> list[str]:
    """
    从指定的 uploads 播放列表中分页拉取所有视频的 videoId。
    - limit_time_bool: 为真时，若视频发布日期早于 start_dt，自动触发 date 拦截，停止向后加载更多历史页面；若晚于 end_dt，则跳过此项视频继续往下。
    - 支持事件暂停 pause_event 与停止 stop_event。
    """
    video_ids: list[str] = []
    seen = set()
    page_token = None
    max_video_items = max(1, int(max_video_items if max_video_items is not None else DEFAULT_MAX_VIDEO_ITEMS))

    while len(video_ids) < max_video_items:
        if should_stop(stop_event):
            break
        if wait_if_paused(pause_event, stop_event):
            break
        while True:
            try:
                response = execute_with_retry(
                    client_pool.client.playlistItems().list(
                        part="contentDetails",
                        playlistId=uploads_playlist_id,
                        maxResults=min(50, max_video_items - len(video_ids)),
                        pageToken=page_token,
                    ), log_callback
                )
                break
            except HttpError as e:
                if e.resp.status in [403, 429]:
                    if client_pool.next_client():
                        continue
                raise e
        
        stopped_by_date = False
        for item in response.get("items", []):
            pub_time = item.get("contentDetails", {}).get("videoPublishedAt", "")
            # 时间过滤逻辑：拦截早于开始日期的列表
            if limit_time_bool and pub_time:
                from datetime import datetime
                try:
                    pub_dt = datetime.strptime(pub_time.split("T")[0], "%Y-%m-%d")
                    if pub_dt.date() < start_dt.date():
                        stopped_by_date = True
                        break
                    if pub_dt.date() > end_dt.date():
                        continue
                except Exception:
                    pass
                    
            video_id = item.get("contentDetails", {}).get("videoId", "")
            if video_id and video_id not in seen:
                seen.add(video_id)
                video_ids.append(video_id)

        log_line(log_callback, f"  API 已读取视频类作品 {len(video_ids)} 条。")
        if stopped_by_date:
            log_line(log_callback, "  API 已读取到早于开始日期的视频，停止加载更多。")
            break
            
        page_token = response.get("nextPageToken")
        if not page_token:
            break

    return video_ids


def video_rows_from_api(client_pool, video_ids: list[str], stop_event=None, pause_event=None, log_callback=None, live_stream_policy: str = "不处理") -> list[dict[str, str]]:
    """
    根据视频 ID 列表，分页批量抓取视频详情元数据。
    调用 videos().list 获取 snippet (标题、描述、发布时间、频道)、statistics (播放、点赞、评论数) 和 contentDetails (时长)。
    如果 live_stream_policy 需要判断直播，则额外请求 liveStreamingDetails。
    """
    rows: list[dict[str, str]] = []
    from src.platforms.youtube.comments import format_youtube_datetime, build_video_url
    from src.platforms.youtube.keyword import format_youtube_duration as kw_format

    api_part = "snippet,statistics,contentDetails"
    if live_stream_policy in ("保留并标记", "直接排除"):
        api_part += ",liveStreamingDetails"

    # API 限制单次请求最大 50 条
    for batch in chunked(video_ids, 50):
        if should_stop(stop_event):
            break
        if wait_if_paused(pause_event, stop_event):
            break
        while True:
            try:
                response = execute_with_retry(
                    client_pool.client.videos().list(part=api_part, id=",".join(batch), maxResults=50),
                    log_callback
                )
                break
            except HttpError as e:
                if e.resp.status in [403, 429]:
                    if client_pool.next_client():
                        continue
                raise e
        for item in response.get("items", []):
            if should_stop(stop_event):
                break
            snippet = item.get("snippet", {})
            stats = item.get("statistics", {})
            content = item.get("contentDetails", {})
            video_id = item.get("id", "")
            title = (snippet.get("title") or "").strip()
            if not video_id or not title:
                continue
            
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
                    continue
            
            raw_duration = content.get("duration", "")
            duration = kw_format(raw_duration)
            final_link = build_video_url(video_id, NORMAL_VIDEO)
            
            pub_date = format_youtube_datetime(snippet.get("publishedAt", ""))
            desc = (snippet.get("description") or "").replace("\n", " | ").replace("\r", "")
            if len(desc) > 300:
                desc = desc[:300] + "..."
                
            rows.append(
                {
                    "link": final_link,
                    "content": f"{title}[视频]",
                    "views": stats.get("viewCount", ""),
                    "comments": stats.get("commentCount", ""),
                    "likes": stats.get("likeCount", ""),
                    "source": "api",
                    "title": title,
                    "channel_title": snippet.get("channelTitle", ""),
                    "channel_id": snippet.get("channelId", ""),
                    "published_at": pub_date,
                    "video_type": UNKNOWN,
                    "live_status": live_status if live_stream_policy != "不处理" else "",
                    "duration": duration,
                    "description": desc,
                }
            )
    return rows


def extract_video_id_from_work(work: dict[str, str]) -> str:
    """Extract a YouTube video ID from a normalized work row."""
    link = work.get("link", "") or ""
    return _extract_video_id_from_href(link)


def apply_unified_video_type_check(works: list[dict[str, str]], log_callback=None, video_type_workers: int = 2,
                                   stop_event=None, pause_event=None) -> None:
    """Fill video_type and canonical link for all video/Shorts work rows."""
    from src.platforms.youtube.comments import build_video_url

    video_works = [work for work in works if work.get("video_type") != "帖子"]
    video_ids = [extract_video_id_from_work(work) for work in video_works]
    video_ids = [vid for vid in video_ids if vid]
    if not video_ids:
        return

    normalized_workers = normalize_video_type_workers(video_type_workers)
    log_line(log_callback, f"  使用统一 HEAD/重定向逻辑验证 {len(set(video_ids))} 个视频的长短类型，并发 {normalized_workers}...")
    type_map = check_video_type_bulk(video_ids, max_workers=normalized_workers,
                                       log_callback=log_callback, stop_event=stop_event, pause_event=pause_event)
    for work in video_works:
        video_id = extract_video_id_from_work(work)
        if not video_id:
            continue
        video_type = type_map.get(video_id, UNKNOWN)
        work["video_type"] = video_type
        work["link"] = build_video_url(video_id, video_type)


def collect_video_works_with_api(client_pool, channel_url: str, max_video_items: int, limit_time_bool: bool, start_dt, end_dt, log_callback, stop_event=None, pause_event=None, live_stream_policy: str = "不处理") -> list[dict[str, str]]:
    """
    完整的 API 模式博主视频采集。
    获取频道的 uploads 播放列表 -> 获取列表下符合时间条件的视频 ID -> 批量拉取视频统计详情。
    """
    channel_item = fetch_channel_item(client_pool, channel_url)
    if not channel_item:
        log_warn(log_callback, "  API 未找到频道信息。")
        return []

    uploads_playlist_id = (
        channel_item.get("contentDetails", {})
        .get("relatedPlaylists", {})
        .get("uploads", "")
    )
    if not uploads_playlist_id:
        log_warn(log_callback, "  API 未找到 uploads 播放列表。")
        return []

    title = channel_item.get("snippet", {}).get("title", "")
    if title:
        log_line(log_callback, f"  API 识别频道：{title}")
    video_ids = collect_upload_video_ids(client_pool, uploads_playlist_id, max_video_items, limit_time_bool, start_dt, end_dt, log_callback, stop_event, pause_event)
    rows = video_rows_from_api(client_pool, video_ids, stop_event, pause_event, log_callback, live_stream_policy)
    log_line(log_callback, f"  API 视频类作品完成：{len(rows)} 条。")
    return rows



def extract_visible_video_cards(page, tab: str) -> list[dict[str, str]]:
    """
    使用 Playwright 在浏览器上下文内执行 JavaScript。
    抓取当前页面 DOM 中可见的视频卡片信息。
    - 针对 videos 标签页使用选择器 'a[href*="/watch?v="]'，针对 shorts 标签页使用 'a[href*="/shorts/"]'。
    - 适配多种卡片容器选择器（如 ytd-rich-item-renderer、ytd-video-renderer 等），提取链接、标题、播放量文本。
    - 标题提取支持多种候选属性与子节点，并过滤清洗多余字符。
    """
    return page.evaluate(
        """({ tab }) => {
            const absUrl = href => {
                if (!href) return '';
                try {
                    const value = new URL(href, location.origin).href.split('&pp=')[0].split('?pp=')[0];
                    const watchMatch = value.match(/https:\\/\\/www\\.youtube\\.com\\/watch\\?v=[\\w-]+/);
                    if (watchMatch) return watchMatch[0];
                    const shortsMatch = value.match(/https:\\/\\/www\\.youtube\\.com\\/shorts\\/[\\w-]+/);
                    if (shortsMatch) return shortsMatch[0];
                    return value;
                }
                catch (error) { return ''; }
            };
            const cleanTitle = text => {
                let value = (text || '').replace(/\\s+/g, ' ').trim();
                if (!value) return '';
                value = value.replace(/\\s+-\\s+play short$/i, '').replace(/\\s+-\\s+播放 Shorts?$/i, '').trim();
                value = value.replace(/\\s+by\\s+.+?\\s+\\d[\\d,.]*\\s+views?.*$/i, '').trim();
                value = value.replace(/\\s+作者：.+?\\s+\\d[\\d,.]*\\s*次观看.*$/i, '').trim();
                value = value.replace(/\\s+作成者:.+?\\s+\\d[\\d,.]*\\s*回視聴.*$/i, '').trim();
                return value;
            };
            const nodeText = node => (node ? (node.innerText || node.textContent || '').trim() : '');
            const titleFrom = (card, link) => {
                const candidates = [
                    link.getAttribute('title'),
                    nodeText(link),
                    nodeText(card.querySelector('#video-title')),
                    nodeText(card.querySelector('a#video-title-link')),
                    nodeText(card.querySelector('yt-lockup-metadata-view-model h3')),
                    nodeText(card.querySelector('h3')),
                    link.getAttribute('aria-label'),
                ];
                for (const candidate of candidates) {
                    const title = cleanTitle(candidate);
                    if (title) return title;
                }
                return '';
            };
            const metricLine = root => {
                const lines = (root.innerText || '').split('\\n').map(line => line.trim()).filter(Boolean);
                return lines.find(line => /views|观看|次观看|回視聴|再生/i.test(line)) || '';
            };

            const linkSelector = tab === 'videos'
                ? 'a[href*="/watch?v="]'
                : 'a[href*="/shorts/"]';
            const cardSelector = [
                'ytd-rich-item-renderer',
                'ytd-video-renderer',
                'ytd-grid-video-renderer',
                'ytd-reel-item-renderer',
                'ytd-rich-grid-media',
                'yt-lockup-view-model',
                'ytm-shorts-lockup-view-model',
                'ytm-video-with-context-renderer',
            ].join(',');

            const results = [];
            const seen = new Set();
            for (const link of document.querySelectorAll(linkSelector)) {
                const href = absUrl(link.getAttribute('href') || link.href || '');
                if (!href) continue;
                if (seen.has(href)) continue;
                seen.add(href);
                const card = link.closest(cardSelector) || link;
                const title = titleFrom(card, link);
                if (!title) continue;
                results.push({
                    link: href,
                    content: `${title}[视频]`,
                    views: metricLine(card),
                    comments: '',
                    likes: '',
                });
            }
            return results;
        }""",
        {"tab": tab},
    )


def collect_video_tab_with_playwright(page, channel_url: str, tab: str, max_scrolls: int, log_callback, stop_event=None, pause_event=None,
                                      page_timeout=None, scroll_delay=None, no_new_limit=None, scroll_px=None,
                                      initial_load_delay=None) -> list[dict[str, str]]:
    """
    使用 Playwright 模拟用户滚动获取视频（Videos）或短视频（Shorts）列表。
    - 拼接标签页 URL 自动跳转，并等待首个视频卡片加载。
    - 循环执行模拟页面滚动，获取当前 DOM 树中的可见卡片进行去重去空累加。
    - 若连续滚动 no_new_limit 次未发现任何新作品，认为已加载至列表最底端，自动退出。
    """
    if page_timeout is None:
        page_timeout = PAGE_LOAD_TIMEOUT
    if scroll_delay is None:
        scroll_delay = POST_SCROLL_DELAY
    if no_new_limit is None:
        no_new_limit = NO_NEW_POST_LIMIT
    if scroll_px is None:
        scroll_px = POST_SCROLL_PX
    if initial_load_delay is None:
        initial_load_delay = INITIAL_LOAD_DELAY

    url = tab_url(channel_url, tab)
    label = "Videos" if tab == "videos" else "Shorts"
    log_line(log_callback, f"  Playwright 读取 {label}：{url}")
    page.goto(url, wait_until="domcontentloaded", timeout=page_timeout)
    if interruptible_sleep(initial_load_delay, stop_event):
        return []
    wait_selector = 'a[href*="/watch?v="]' if tab == "videos" else 'a[href*="/shorts/"]'
    try:
        page.wait_for_selector(wait_selector, timeout=12000)
    except PlaywrightTimeoutError:
        log_line(log_callback, f"  未等到 {label} 链接，继续滚动尝试。")

    works: list[dict[str, str]] = []
    seen_links = set()
    no_new_count = 0
    max_scrolls = max(1, int(max_scrolls if max_scrolls is not None else DEFAULT_MAX_POST_SCROLLS))

    for scroll_index in range(max_scrolls):
        if should_stop(stop_event):
            break
        if wait_if_paused(pause_event, stop_event):
            break

        added = 0
        for item in extract_visible_video_cards(page, tab):
            link = normalize_youtube_href(item.get("link", ""))
            content = str(item.get("content") or "").strip()
            if not link or not content or link in seen_links:
                continue
            seen_links.add(link)
            
            from src.platforms.youtube.comments import build_video_url
            video_id = ""
            if "watch?v=" in link:
                video_id = link.split("v=")[1].split("&")[0]
            elif "shorts/" in link:
                video_id = link.split("shorts/")[1].split("?")[0]
            
            v_type = "普通视频" if tab == "videos" else "Shorts"
            final_link = build_video_url(video_id, v_type) if video_id else link
            
            works.append(
                {
                    "link": sanitize_csv_cell(final_link),
                    "content": sanitize_csv_cell(content),
                    "views": sanitize_csv_cell(normalize_metric_text(item.get("views", ""))),
                    "comments": "",
                    "likes": "",
                    "source": "playwright",
                    "title": content.replace("[视频]", ""),
                    "channel_title": "",
                    "channel_id": "",
                    "published_at": "",
                    "video_type": v_type,
                    "duration": "",
                    "description": "",
                }
            )
            added += 1

        if added:
            log_line(log_callback, f"    {label} 滚动 {scroll_index + 1}/{max_scrolls}：新增 {added} 条，累计 {len(works)} 条。")
            no_new_count = 0
        else:
            no_new_count += 1
            if no_new_count >= no_new_limit:
                log_warn(log_callback, f"    连续 {no_new_limit} 次没有新增，停止 {label}。")
                break

        page.evaluate(f"window.scrollBy(0, {scroll_px})")
        if interruptible_sleep(scroll_delay, stop_event):
            break

    return works


def _extract_video_id_from_href(href: str) -> str:
    """
    从 YouTube 链接中提取 video ID。
    支持 watch?v= 和 /shorts/ 两种格式。
    """
    match = re.search(r'(?:watch\?v=|shorts/)([a-zA-Z0-9_-]{11})', href or '')
    return match.group(1) if match else ''


def extract_visible_posts(page) -> list[dict[str, str]]:
    """
    使用 Playwright 在浏览器上下文内执行 JavaScript。
    抓取当前页面 DOM 中可见的社区帖子（Posts）。
    - 针对帖子卡片容器使用选择器（如 ytd-backstage-post-thread-renderer 等）。
    - 提取帖子正文内容及可能包含的图片附件标识（`[图片]`）。
    - 提取点赞数、评论数、浏览数。为应对不同界面的渲染差异，支持从 HTML 节点的 outerHTML 属性中用正则尝试还原包含 `viewCount`/`commentCount`/`likeCount` 的 JSON API 端点信息。
    - 寻找帖子专属详情跳转链接。
    """
    return page.evaluate(
        """() => {
            const absUrl = href => {
                if (!href) return '';
                try { return new URL(href, location.origin).href.split('&pp=')[0].split('?pp=')[0]; }
                catch (error) { return ''; }
            };
            const uniqueTextLines = text => {
                const seen = new Set();
                const lines = [];
                for (const line of (text || '').split('\\n')) {
                    const clean = line.trim();
                    if (!clean || seen.has(clean)) continue;
                    seen.add(clean);
                    lines.push(clean);
                }
                return lines;
            };
            const textFrom = (root, selectors) => {
                for (const selector of selectors) {
                    const node = root.querySelector(selector);
                    const text = node ? (node.innerText || node.textContent || '').trim() : '';
                    if (text) return text;
                }
                return '';
            };
            const metricLine = (root, patterns) => {
                const lines = [
                    ...uniqueTextLines(root.innerText || ''),
                    ...Array.from(root.querySelectorAll('[aria-label], [title]')).flatMap(node => [
                        node.getAttribute('aria-label') || '',
                        node.getAttribute('title') || '',
                    ]).map(line => line.trim()).filter(Boolean),
                ];
                return lines.find(line => patterns.some(pattern => pattern.test(line))) || '';
            };
            const countFromEndpoint = (root, names) => {
                const html = root.outerHTML || '';
                for (const name of names) {
                    const patterns = [
                        new RegExp(`"${name}"\\\\s*:\\\\s*"?(\\\\d[\\\\d,]*)"?`, 'i'),
                        new RegExp(`"${name}"\\\\s*:\\\\s*\\\\{[^{}]*"simpleText"\\\\s*:\\\\s*"([^"]+)"`, 'i'),
                        new RegExp(`"${name}"\\\\s*:\\\\s*\\\\{[^{}]*"text"\\\\s*:\\\\s*"([^"]+)"`, 'i'),
                    ];
                    for (const pattern of patterns) {
                        const match = html.match(pattern);
                        if (match && match[1]) return match[1];
                    }
                }
                return '';
            };
            const extractMetric = (root, type) => {
                if (type === 'views') {
                    return (
                        metricLine(root, [/views?/i, /观看/, /次观看/, /回視聴/, /再生/]) ||
                        countFromEndpoint(root, ['viewCount', 'views'])
                    );
                }
                if (type === 'comments') {
                    return (
                        metricLine(root, [/comments?/i, /评论/, /留言/, /則留言/, /件のコメント/]) ||
                        countFromEndpoint(root, ['commentCount', 'commentsCount'])
                    );
                }
                return (
                    metricLine(root, [/likes?/i, /赞/, /讚/, /高評価/, /件の高評価/]) ||
                    countFromEndpoint(root, ['likeCount', 'likesCount'])
                );
            };
            const findPostLink = root => {
                for (const node of root.querySelectorAll('a[href*="/post/"], a[href*="/channel/"][href*="/community?lb="]')) {
                    const href = absUrl(node.getAttribute('href') || node.href || '');
                    if (href) return href;
                }
                return location.href;
            };
            const nonAvatarImages = root => {
                return Array.from(root.querySelectorAll('img')).filter(img => {
                    const src = img.getAttribute('src') || '';
                    const width = Number(img.naturalWidth || img.width || 0);
                    const height = Number(img.naturalHeight || img.height || 0);
                    if (src.includes('yt3.ggpht.com') && width <= 160 && height <= 160) return false;
                    return width > 80 || height > 80 || src.includes('ytimg.com');
                });
            };

            const results = [];
            const postSelectors = [
                'ytd-backstage-post-thread-renderer',
                'ytd-post-renderer',
                'ytd-rich-item-renderer:has(ytd-backstage-post-thread-renderer)',
            ].join(',');
            for (const post of document.querySelectorAll(postSelectors)) {
                const text = textFrom(post, [
                    '#content-text',
                    'yt-formatted-string#content-text',
                    'yt-attributed-string#content-text',
                    '[id="content-text"]',
                ]);
                const parts = [];
                if (text) parts.push(text);
                if (nonAvatarImages(post).length) parts.push('[图片]');
                const content = parts.join('\\n').trim();
                if (!content) continue;
                results.push({
                    link: findPostLink(post),
                    content,
                    views: extractMetric(post, 'views'),
                    comments: extractMetric(post, 'comments'),
                    likes: extractMetric(post, 'likes'),
                });
            }
            return results;
        }"""
    )


def collect_posts_with_playwright(page, channel_url: str, max_post_scrolls: int, log_callback, stop_event=None, pause_event=None,
                                   page_timeout=None, scroll_delay=None, no_new_limit=None, scroll_px=None,
                                   initial_load_delay=None) -> list[dict[str, str]]:
    """
    使用 Playwright 模拟用户滚动获取社区帖子（Posts）列表。
    - 自动打开 Posts 标签页，并获取当前 DOM 中已加载的帖子并清洗。
    - 循环执行模拟页面滚动，获取当前 DOM 树中的可见帖子并累加。
    - 若连续滚动 no_new_limit 次未发现任何新帖子，认为已加载至列表最底端，自动退出。
    """
    if page_timeout is None:
        page_timeout = PAGE_LOAD_TIMEOUT
    if scroll_delay is None:
        scroll_delay = POST_SCROLL_DELAY
    if no_new_limit is None:
        no_new_limit = NO_NEW_POST_LIMIT
    if scroll_px is None:
        scroll_px = POST_SCROLL_PX
    if initial_load_delay is None:
        initial_load_delay = INITIAL_LOAD_DELAY

    url = posts_url(channel_url)
    page.goto(url, wait_until="domcontentloaded", timeout=page_timeout)
    if interruptible_sleep(initial_load_delay, stop_event):
        return []

    posts: list[dict[str, str]] = []
    seen_links = set()
    no_new_count = 0
    max_post_scrolls = max(1, int(max_post_scrolls if max_post_scrolls is not None else DEFAULT_MAX_POST_SCROLLS))
    log_line(log_callback, f"  Playwright 读取 Posts：{url}")

    for scroll_index in range(max_post_scrolls):
        if should_stop(stop_event):
            break
        if wait_if_paused(pause_event, stop_event):
            break

        added = 0
        for item in extract_visible_posts(page):
            link = normalize_youtube_href(item.get("link", ""))
            content = str(item.get("content") or "").strip()
            if not link or not content or link in seen_links:
                continue
            seen_links.add(link)
            posts.append(
                {
                    "link": sanitize_csv_cell(link),
                    "content": sanitize_csv_cell(content),
                    "views": sanitize_csv_cell(normalize_metric_text(item.get("views", ""))),
                    "comments": sanitize_csv_cell(normalize_metric_text(item.get("comments", ""))),
                    "likes": sanitize_csv_cell(normalize_metric_text(item.get("likes", ""))),
                    "source": "playwright",
                    "title": content[:50] + "..." if len(content) > 50 else content,
                    "channel_title": "",
                    "channel_id": "",
                    "published_at": "",
                    "video_type": "帖子",
                    "duration": "",
                    "description": content,
                }
            )
            added += 1

        if added:
            log_line(log_callback, f"    Posts 滚动 {scroll_index + 1}/{max_post_scrolls}：新增 {added} 条，累计 {len(posts)} 条。")
            no_new_count = 0
        else:
            no_new_count += 1
            if no_new_count >= no_new_limit:
                log_warn(log_callback, f"    连续 {no_new_limit} 次没有新增，停止 Posts。")
                break

        page.evaluate(f"window.scrollBy(0, {scroll_px})")
        if interruptible_sleep(scroll_delay, stop_event):
            break

    return posts


def row_from_work(index: int, work: dict[str, str], channel_url: str = "") -> dict[str, str]:
    """
    数据行格式化工具。
    将内部采集得到的 dict 格式作品字段转换为最终写入 Excel 的列对应的统一 KV。
    """
    ch_id = work.get("channel_id", "")
    ch_url = f"https://www.youtube.com/channel/{ch_id}" if ch_id else channel_url
    
    return {
        "序号": str(index),
        "编号": str(index),
        "视频链接": work.get("link", ""),
        "作品链接": work.get("link", ""),
        "博主主页链接": ch_url,
        "作者主页链接": channel_url,
        "标题": work.get("title", ""),
        "作品内容": work.get("content", ""),
        "频道名称": work.get("channel_title", ""),
        "发布日期": work.get("published_at", ""),
        "视频类型": work.get("video_type", ""),
        "直播状态": work.get("live_status", ""),
        "关联视频标题": work.get("related_title", ""),
        "关联视频链接": work.get("related_link", ""),
        "视频时长": work.get("duration", ""),
        "视频简介": work.get("description", ""),
        "播放量": work.get("views", ""),
        "浏览量": work.get("views", ""),
        "点赞数": work.get("likes", ""),
        "评论数": work.get("comments", ""),
    }


def build_channel_works_scope(
    channel_urls: list[str],
    collect_target: str,
    max_video_items: int,
    max_post_scrolls: int,
    fetch_shorts_related: str,
    live_stream_policy: str,
    limit_time_str: str,
    start_date: str,
    end_date: str,
    get_comments_str: str,
    max_comments: int,
    config: dict | None = None,
) -> dict:
    settings = config or {}
    return {
        "storage_version": CHANNEL_WORKS_STORAGE_VERSION,
        "channel_urls": list(channel_urls),
        "collect_target": collect_target,
        "max_video_items": int(max_video_items),
        "max_post_scrolls": int(max_post_scrolls),
        "fetch_shorts_related": fetch_shorts_related,
        "live_stream_policy": live_stream_policy,
        "limit_time": limit_time_str,
        "start_date": start_date if limit_time_str == "是" else "",
        "end_date": end_date if limit_time_str == "是" else "",
        "get_comments": get_comments_str,
        "max_comments": int(max_comments),
        "comment_mode": normalize_comment_mode(settings.get("youtube_comment_mode", COMMENT_MODE_FAST)),
        "comment_scan_limit": int(settings.get("youtube_comment_scan_limit", 500)),
        "verify_video_type": settings.get("verify_video_type", "是"),
        "page_load_timeout": int(settings.get("page_load_timeout", PAGE_LOAD_TIMEOUT)),
        "scroll_interval": float(settings.get("scroll_interval", POST_SCROLL_DELAY)),
        "no_new_scroll_limit": int(settings.get("no_new_scroll_limit", NO_NEW_POST_LIMIT)),
        "scroll_px": int(settings.get("scroll_px", POST_SCROLL_PX)),
        "initial_load_delay": float(settings.get("initial_load_delay", INITIAL_LOAD_DELAY)),
    }


def channel_works_spool_path(scope: dict) -> str:
    fingerprint = task_fingerprint(CHANNEL_WORKS_TOOL_ID, scope)
    return str(find_latest_spool_path(CHANNEL_WORKS_TOOL_ID, fingerprint))


def _channel_comment_row_key(video_id: str, comment: dict, rank: int) -> str:
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


def run_youtube_channel_works_spider(
    api_keys: list[str],
    channel_urls_text: str,
    collect_target: str = "全部",
    max_video_items: int = DEFAULT_MAX_VIDEO_ITEMS,
    max_post_scrolls: int = DEFAULT_MAX_POST_SCROLLS,
    fetch_shorts_related: str = "否",
    live_stream_policy: str = "不处理",
    limit_time_str: str = "否",
    start_date: str = "",
    end_date: str = "",
    get_comments_str: str = "否",
    max_comments: int = 100,
    log_callback=None,
    finish_callback=None,
    stop_event=None,
    config: dict | None = None,
    pause_event=None,
):
    """Collect channel works into SQLite and perform one final streaming XLSX export."""

    config = dict(config or {})
    page_timeout = int(config.get("page_load_timeout", PAGE_LOAD_TIMEOUT))
    scroll_interval_val = float(config.get("scroll_interval", POST_SCROLL_DELAY))
    no_new_limit = int(config.get("no_new_scroll_limit", NO_NEW_POST_LIMIT))
    scroll_px_val = int(config.get("scroll_px", POST_SCROLL_PX))
    max_post_scrolls = int(config.get("max_post_scrolls", max_post_scrolls))
    initial_load_delay_val = float(config.get("initial_load_delay", INITIAL_LOAD_DELAY))
    verify_video_type_bool = config.get("verify_video_type", "是") == "是"
    video_type_workers = normalize_video_type_workers(config.get("youtube_video_type_workers"))
    comment_mode = normalize_comment_mode(config.get("youtube_comment_mode", COMMENT_MODE_FAST))
    comment_workers = normalize_comment_workers(config.get("youtube_comment_workers", DEFAULT_COMMENT_WORKERS))
    comment_scan_limit = int(config.get("youtube_comment_scan_limit", 500))
    shorts_related_delay = float(config.get("youtube_shorts_related_delay", 1.0))

    max_parallel_tabs = max(1, int(config.get("max_parallel_tabs", 3)))
    resume_action = str(config.get("_resume_action", "continue") or "continue")
    feedback = feedback_from_config(config)
    run_id = generate_run_id()
    output_path = build_output_path("youtube", f"youtube_channel_works_{time.strftime('%Y%m%d_%H%M%S')}.xlsx", channel="channel_works")
    report_path = str(output_path).replace(".xlsx", ".report.json")
    outcome = RunOutcome(run_id, CHANNEL_WORKS_TOOL_ID, RunStatus.FAILED, stats=RunStats())
    spool: SqliteRowSpool | None = None
    checkpoint: TaskCheckpoint | None = None
    spool_closed = False
    keep_spool = True
    clear_checkpoint = False
    last_checkpoint_heartbeat = 0.0
    try:
        channel_urls = parse_channel_urls(channel_urls_text)
        outcome.stats.input_count = len(channel_urls)
        if not channel_urls:
            log_line(log_callback, "未读取到有效的 YouTube 作者主页链接。")
            outcome.errors.append(RunError("NO_VALID_INPUT", "未读取到有效的 YouTube 作者主页链接。"))
            return outcome
        progress_total = len(channel_urls) + 1
        if feedback is not None:
            feedback.set_total(progress_total, phase="频道作品采集")
            feedback.set_activity(
                "正在准备频道任务",
                stop_reason="正在初始化本地恢复状态，请等待当前步骤完成。",
            )

        scope = build_channel_works_scope(
            channel_urls,
            collect_target,
            max_video_items,
            max_post_scrolls,
            fetch_shorts_related,
            live_stream_policy,
            limit_time_str,
            start_date,
            end_date,
            get_comments_str,
            max_comments,
            config,
        )
        existing_spool_path = channel_works_spool_path(scope)
        checkpoint = open_task_checkpoint(CHANNEL_WORKS_TOOL_ID, scope, log_callback)
        fingerprint = checkpoint.fingerprint
        concurrent_run = checkpoint.has_other_active_runs()
        if resume_action == "export":
            spool_path = existing_spool_path
        elif concurrent_run:
            spool_path = str(build_spool_path(CHANNEL_WORKS_TOOL_ID, fingerprint, checkpoint.run_id[-8:]))
            log_line(log_callback, f"检测到相同任务正在其他窗口运行，本窗口使用独立缓存：{spool_path}")
        else:
            spool_path = existing_spool_path
        if resume_action == "restart":
            remove_spool_files(spool_path)
        if resume_action == "export" and os.path.exists(spool_path):
            partial_path = build_output_path(
                "youtube",
                f"youtube_channel_works_partial_{time.strftime('%Y%m%d_%H%M%S')}.xlsx",
                channel="channel_works",
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
                int(export_stats.get("stage_counts", {}).get("channel", 0)),
            )
            outcome.artifacts.extend(
                [ArtifactRef(partial_path, "当前已采集数据"), ArtifactRef(spool_path, "可恢复临时数据")]
            )
            return outcome

        checkpoint.add_output_path(spool_path)

        def _heartbeat_checkpoint() -> None:
            nonlocal last_checkpoint_heartbeat
            now = time.monotonic()
            if checkpoint is not None and now - last_checkpoint_heartbeat >= 60.0:
                checkpoint.heartbeat()
                last_checkpoint_heartbeat = now

        get_comments_bool = get_comments_str == "是"
        comment_fields = ["序号", "编号", "视频链接", "作品链接", "评论的点赞量", "评论内容", "发布时间", "评论发布时间"]
        sheets = [SpoolSheet("作品信息", CSV_FIELDS)]
        if get_comments_bool:
            sheets.append(SpoolSheet("评论信息", comment_fields))
        spool = SqliteRowSpool(spool_path, sheets, metadata={"tool_id": CHANNEL_WORKS_TOOL_ID, "scope": scope})
        completed_channel_items = spool.completed_items("channel")
        completed_channels = set(completed_channel_items)
        if feedback is not None:
            feedback.set_progress(
                len(completed_channels),
                total=progress_total,
                activity="正在读取断点进度",
                stop_reason="正在读取本地 SQLite 恢复状态。",
            )
        if completed_channels:
            log_line(log_callback, f"断点续跑：已完成 {len(completed_channels)} 个频道。")
        log_line(log_callback, f"持久化缓存已就绪：{spool_path}")

        client_pool = None
        if build is None:
            log_line(log_callback, "缺少依赖：google-api-python-client。Videos/Shorts 将尝试浏览器 fallback。")
        else:
            try:
                client_pool = YouTubeClientPool(api_keys)
            except Exception as exc:
                log_warn(log_callback, f"YouTube API 初始化失败，Videos/Shorts 将尝试浏览器 fallback：{exc}")

        limit_time_bool = limit_time_str == "是"
        fetch_shorts_related_bool = fetch_shorts_related == "是"
        start_dt, end_dt = None, None
        if limit_time_bool:
            start_dt, end_dt = parse_date_range(start_date, end_date)
            
        log_lock = threading.Lock()
        stats_lock = threading.Lock()
        order_condition = threading.Condition()
        quota_stop = threading.Event()
        channel_failures = [0]
        comment_failures = [0]
        channel_work_counts: dict[int, int] = {}
        for channel_index, channel_url in enumerate(channel_urls, 1):
            if channel_url not in completed_channel_items:
                continue
            try:
                channel_work_counts[channel_index] = max(
                    0,
                    int(completed_channel_items[channel_url].get("work_count", 0) or 0),
                )
            except (TypeError, ValueError):
                channel_work_counts[channel_index] = 0

        def _announce_channel_work_count(channel_index: int, work_count: int) -> None:
            with order_condition:
                channel_work_counts.setdefault(channel_index, max(0, int(work_count)))
                order_condition.notify_all()

        def _ordered_serial_start(channel_index: int, work_count: int) -> int | None:
            _announce_channel_work_count(channel_index, work_count)
            with order_condition:
                while any(index not in channel_work_counts for index in range(1, channel_index)):
                    if should_stop(stop_event) or quota_stop.is_set():
                        return None
                    order_condition.wait(timeout=0.2)
                return 1 + sum(channel_work_counts[index] for index in range(1, channel_index))

        def make_thread_log(base_log_callback, lock, prefix):
            def wrapped(msg):
                if base_log_callback:
                    with lock:
                        base_log_callback(f"[{prefix}] {msg}")
            return wrapped

        def worker(channel_index, channel_url):
            if channel_url in completed_channels:
                return
            if should_stop(stop_event) or quota_stop.is_set():
                return
            if wait_if_paused(pause_event, stop_event):
                return

            thread_log = make_thread_log(log_callback, log_lock, f"频道 {channel_index}")
            thread_log(f"开始读取作者主页：{channel_url}")
            if feedback is not None:
                feedback.set_activity(
                    f"正在采集频道 {channel_index}/{len(channel_urls)}",
                    stop_reason="仍有 YouTube API 或浏览器页面请求正在等待响应。",
                )

            works: list[dict[str, str]] = []
            should_fallback_video_tabs = False
            do_videos = collect_target in ("全部", "仅视频与Shorts")
            do_posts = collect_target in ("全部", "仅帖子 (Posts)")

            if do_videos:
                if client_pool is None:
                    should_fallback_video_tabs = True
                    thread_log("YouTube API 不可用，尝试用浏览器读取 Videos/Shorts。")
                else:
                    try:
                        video_works = collect_video_works_with_api(client_pool, channel_url, max_video_items, limit_time_bool, start_dt, end_dt, thread_log, stop_event, pause_event, live_stream_policy)
                        if not video_works:
                            should_fallback_video_tabs = True
                            thread_log("API 未返回 Videos/Shorts，尝试用浏览器读取。")
                        else:
                            works.extend(video_works)
                    except Exception as exc:
                        should_fallback_video_tabs = True
                        thread_log(f"[WARN] YouTube API 读取失败，尝试用浏览器读取 Videos/Shorts：{exc}")

            thread_playwright = None
            thread_browser = None
            thread_page = None
            try:
                if sync_playwright is not None:
                    if (should_fallback_video_tabs and do_videos) or do_posts:
                        thread_log("开始拉起 Playwright 抓取主页...")
                        thread_playwright = sync_playwright().start()
                        thread_browser, context = connect_existing_chromium(thread_playwright, DEFAULT_X_CDP_URL, log_callback=thread_log)
                        thread_page = context.new_page()

                    if do_videos and should_fallback_video_tabs and thread_page and not should_stop(stop_event):
                        try:
                            works.extend(collect_video_tab_with_playwright(thread_page, channel_url, "videos", max_post_scrolls, thread_log, stop_event, pause_event, page_timeout, scroll_interval_val, no_new_limit, scroll_px_val, initial_load_delay_val))
                            if not should_stop(stop_event):
                                works.extend(collect_video_tab_with_playwright(thread_page, channel_url, "shorts", max_post_scrolls, thread_log, stop_event, pause_event, page_timeout, scroll_interval_val, no_new_limit, scroll_px_val, initial_load_delay_val))
                        except PlaywrightTimeoutError:
                            thread_log("[WARN] 跳过浏览器 Videos/Shorts：页面加载超时。")
                        except Exception as exc:
                            thread_log(f"[WARN] 跳过浏览器 Videos/Shorts：{exc}")

                    if do_posts and thread_page and not should_stop(stop_event):
                        try:
                            works.extend(collect_posts_with_playwright(thread_page, channel_url, max_post_scrolls, thread_log, stop_event, pause_event, page_timeout, scroll_interval_val, no_new_limit, scroll_px_val, initial_load_delay_val))
                        except PlaywrightTimeoutError:
                            thread_log("[WARN] 跳过 Posts：页面加载超时。")
                        except Exception as exc:
                            thread_log(f"[WARN] 跳过 Posts：{exc}")
                else:
                    if should_fallback_video_tabs and do_videos:
                        thread_log("[ERROR] 缺少依赖：playwright。无法浏览器 fallback Videos/Shorts。")
                    if do_posts:
                        thread_log("[ERROR] 缺少依赖：playwright。跳过 Posts。")
            finally:
                try:
                    if thread_page and not thread_page.is_closed():
                        thread_page.close()
                except Exception:
                    pass
                try:
                    if thread_browser and thread_browser.is_connected():
                        thread_browser.close()
                except Exception:
                    pass
                try:
                    if thread_playwright:
                        thread_playwright.stop()
                except Exception:
                    pass

            if not works:
                thread_log("未获取到任何作品数据。")
                _announce_channel_work_count(channel_index, 0)
                with stats_lock:
                    channel_failures[0] += 1
                return

            if verify_video_type_bool and not should_stop(stop_event):
                try:
                    apply_unified_video_type_check(works, thread_log, video_type_workers, stop_event=stop_event, pause_event=pause_event)
                except Exception as exc:
                    thread_log(f"[WARN] 统一视频类型验证失败，保留原始类型：{exc}")

            serial_start = _ordered_serial_start(channel_index, len(works))
            if serial_start is None:
                return
            indexed_works = []
            for work_order, work in enumerate(works):
                if should_stop(stop_event):
                    break
                if fetch_shorts_related_bool and work.get("video_type") == "Shorts":
                    vid = extract_video_id_from_work(work)
                    if vid:
                        if interruptible_sleep(shorts_related_delay, stop_event):
                            break
                        thread_log(f"获取 Shorts 关联长视频：{vid}")
                        from src.platforms.youtube.shorts import fetch_short_related_video
                        rt, rl = fetch_short_related_video(vid)
                        work["related_title"] = rt
                        work["related_link"] = rl

                serial_number = serial_start + work_order
                row = row_from_work(serial_number, work, channel_url)
                indexed_works.append((serial_number, work_order, work, row))

            completed_comment_ids = spool.completed_keys("comment") if get_comments_bool else set()
            comment_tasks = []
            comment_context: dict[str, tuple[int, str, int]] = {}
            work_operations = []
            zero_comment_operations = []
            for serial, work_order, work, row in indexed_works:
                if should_stop(stop_event) or quota_stop.is_set():
                    break
                work_link = str(work.get("link", ""))
                video_id = _extract_video_id_from_href(work_link)
                work_key = video_id or work_link or hashlib.sha1(str(row).encode("utf-8")).hexdigest()
                work_operations.append(
                    (
                        "work",
                        work_key,
                        {"作品信息": [SpoolRow(work_key, row, channel_index, work_order)]},
                        {"channel_url": channel_url, "serial": serial},
                    )
                )
                if get_comments_bool and video_id and video_id not in completed_comment_ids:
                    if has_zero_comment_count(work.get("comments")):
                        zero_comment_operations.append(("comment", video_id, {}, {"status": "zero"}))
                        continue
                    comment_context[video_id] = (serial, work_link, work_order)
                    comment_tasks.append(CommentFetchTask(video_id=video_id, video_url=work_link, index=str(serial)))

            for chunk_start in range(0, len(work_operations), 1000):
                if feedback is not None:
                    feedback.set_activity(
                        f"正在写入频道 {channel_index} 的作品进度",
                        stop_reason="正在等待本地 SQLite 事务提交完成。",
                    )
                spool.submit_items(work_operations[chunk_start:chunk_start + 1000])
                _heartbeat_checkpoint()
            for chunk_start in range(0, len(zero_comment_operations), 1000):
                if feedback is not None:
                    feedback.set_activity(
                        f"正在写入频道 {channel_index} 的评论状态",
                        stop_reason="正在等待本地 SQLite 事务提交完成。",
                    )
                spool.submit_items(zero_comment_operations[chunk_start:chunk_start + 1000])
                _heartbeat_checkpoint()

            def _write_comment_result(task, result):
                if result.status == "stopped":
                    return False
                if result.status == "error" and is_comment_quota_error_message(result.error, result.http_status):
                    quota_stop.set()
                    thread_log(f"[ERROR] 评论配额/限流不可用：{result.error}")
                    return False
                if result.status == "error":
                    with stats_lock:
                        comment_failures[0] += 1
                    thread_log(f"[WARN] 评论获取失败 ({task.video_id})：{result.error}")
                serial_number, link, work_order = comment_context[task.video_id]
                spool_rows = []
                for rank, comment in enumerate(result.comments[:max_comments]):
                    comment_row = {
                        "序号": str(serial_number),
                        "编号": str(serial_number),
                        "视频链接": link,
                        "作品链接": link,
                        "评论的点赞量": str(comment["like_count"]),
                        "评论内容": comment["text"],
                        "发布时间": comment.get("published_at", ""),
                        "评论发布时间": comment.get("published_at", ""),
                    }
                    spool_rows.append(
                        SpoolRow(
                            _channel_comment_row_key(task.video_id, comment, rank),
                            comment_row,
                            channel_index,
                            work_order * max(1, max_comments) + rank,
                        )
                    )
                for chunk_index in range(0, len(spool_rows), 1000):
                    chunk = spool_rows[chunk_index:chunk_index + 1000]
                    if feedback is not None:
                        feedback.set_activity(
                            f"正在写入频道 {channel_index} 的评论进度",
                            stop_reason="正在等待本地 SQLite 事务提交完成。",
                        )
                    spool.submit_item(
                        "comment_chunk",
                        f"{task.video_id}:{chunk_index // 1000}",
                        {"评论信息": chunk},
                        {"video_id": task.video_id},
                    )
                spool.submit_item("comment", task.video_id, {}, {"status": result.status, "row_count": len(spool_rows)})
                if feedback is not None:
                    feedback.set_activity(
                        f"正在获取频道 {channel_index} 的评论",
                        stop_reason="仍有已发送的 YouTube 评论请求正在等待响应。",
                    )
                _heartbeat_checkpoint()
                return True

            for chunk_start in range(0, len(comment_tasks), 50):
                if should_stop(stop_event) or quota_stop.is_set():
                    return
                chunk_tasks = comment_tasks[chunk_start:chunk_start + 50]
                if chunk_tasks:
                    if feedback is not None:
                        feedback.set_activity(
                            f"正在获取频道 {channel_index} 的评论",
                            stop_reason="仍有已发送的 YouTube 评论请求正在等待响应。",
                        )
                    fetch_top_comments_for_videos(
                        api_keys,
                        chunk_tasks,
                        comment_scan_limit,
                        max_comments,
                        comment_mode,
                        comment_workers,
                        thread_log,
                        stop_event,
                        pause_event,
                        result_callback=_write_comment_result,
                        retain_results=False,
                        queue_capacity=16,
                        total_tasks=len(chunk_tasks),
                    )

            if should_stop(stop_event) or quota_stop.is_set():
                return
            if feedback is not None:
                feedback.set_activity(
                    f"正在提交频道 {channel_index} 的完成状态",
                    stop_reason="正在等待本地 SQLite 事务提交完成。",
                )
            spool.submit_item("channel", channel_url, {}, {"work_count": len(indexed_works)})
            thread_log(f"作者主页完成：持久化 {len(indexed_works)} 条作品。")

        progress_counted_urls = set(completed_channels)
        with ThreadPoolExecutor(max_workers=max_parallel_tabs) as executor:
            futures = {
                executor.submit(worker, idx, url): (idx, url)
                for idx, url in enumerate(channel_urls, 1)
            }
            for future in as_completed(futures):
                if should_stop(stop_event):
                    for f in futures:
                        f.cancel()
                    break
                try:
                    future.result()
                except Exception as exc:
                    channel_index, channel_url = futures[future]
                    _announce_channel_work_count(channel_index, 0)
                    with stats_lock:
                        channel_failures[0] += 1
                    log_error(log_callback, f"频道 {channel_index} 执行异常 ({channel_url}): {exc}")
                channel_url = futures[future][1]
                if feedback is not None and channel_url not in progress_counted_urls:
                    progress_counted_urls.add(channel_url)
                    feedback.advance(
                        1,
                        activity="已完成一个频道处理单元",
                        stop_reason="",
                    )

        if should_stop(stop_event):
            if feedback is not None:
                feedback.set_activity("任务已停止", stop_reason="")
            outcome.status = RunStatus.CANCELLED
            outcome.stats.success_count = len(spool.completed_keys("channel"))
            outcome.stats.extra["persisted_work_rows"] = spool.row_count("作品信息")
            if get_comments_bool:
                outcome.stats.extra["persisted_comment_rows"] = spool.row_count("评论信息")
            outcome.artifacts.append(ArtifactRef(spool_path, "可恢复临时数据"))
            log_line(log_callback, "任务已停止，SQLite 进度已保留；再次启动相同任务可继续或导出。")
            return outcome
        if quota_stop.is_set():
            if feedback is not None:
                feedback.set_activity("评论采集已中止，进度已保留", stop_reason="")
            outcome.status = RunStatus.PARTIAL
            outcome.stats.success_count = len(spool.completed_keys("channel"))
            outcome.stats.extra["persisted_work_rows"] = spool.row_count("作品信息")
            outcome.stats.extra["persisted_comment_rows"] = spool.row_count("评论信息")
            outcome.errors.append(RunError("YOUTUBE_QUOTA_STOPPED", "评论配额或限流不可用，任务进度已保留。"))
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
        outcome.status = RunStatus.PARTIAL if channel_failures[0] or comment_failures[0] else RunStatus.SUCCEEDED
        outcome.output_path = output_path
        outcome.stats.success_count = len(channel_urls) - channel_failures[0]
        outcome.stats.failed_count = channel_failures[0]
        outcome.stats.extra["comment_failed_count"] = comment_failures[0]
        outcome.stats.extra.update(export_stats)
        outcome.artifacts.append(ArtifactRef(output_path, "YouTube 频道作品数据"))
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
        error_code = "YOUTUBE_RECOVERABLE_STOPPED" if recoverable else "CHANNEL_WORKS_FAILED"
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

