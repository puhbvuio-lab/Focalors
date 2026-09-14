from __future__ import annotations

import html as html_lib
import json
import random
import re
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

try:
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
    from playwright.sync_api import sync_playwright
except ModuleNotFoundError:
    PlaywrightTimeoutError = TimeoutError
    sync_playwright = None

from src.core import (
    XlsxRowWriter,
    MultiSheetXlsxWriter,
    build_output_path,
    connect_existing_chromium,
    interruptible_sleep,
    log_error,
    log_line,
    log_warn,
    sanitize_csv_row,
    should_stop,
    wait_if_paused,
)
from src.core import expand_compact_number, extract_tiktok_video_title
from src.platforms.tiktok.comments import collect_video_comments
from src.platforms.tiktok.keyword import parse_publish_date as parse_keyword_publish_date


CSV_FIELDS = ["序号", "视频链接", "发布日期", "视频简介", "点赞数", "评论数", "收藏量", "分享数"]
PAGE_LOAD_TIMEOUT = 45000
DETAIL_LOAD_TIMEOUT = 30000
SCROLL_INTERVAL_SECONDS = 2.5
DETAIL_DELAY_MIN_SECONDS = 2.0
DETAIL_DELAY_MAX_SECONDS = 5.0
LINK_BATCH_SIZE = 50
SAVE_BATCH_SIZE = 10
BATCH_WAIT_MIN_SECONDS = 1.5
BATCH_WAIT_MAX_SECONDS = 3.0
NO_NEW_SCROLL_LIMIT = 10
DEFAULT_MAX_SCROLLS = 500
SCROLL_PX = 3600
MIN_GUARANTEED_VIDEOS = 5


def parse_date_range(start_date: str, end_date: str) -> tuple[datetime, datetime]:
    """
    解析并校验日期范围字符串，返回对应的 datetime 对象元组。
    """
    start_dt = datetime.strptime(start_date.strip(), "%Y-%m-%d")
    end_dt = datetime.strptime(end_date.strip(), "%Y-%m-%d")
    if start_dt > end_dt:
        raise ValueError("开始日期不能晚于结束日期。")
    return start_dt, end_dt


def parse_publish_date(value: str) -> datetime | None:
    """
    解析发布日期字符串，尝试匹配并提取 'YYYY-MM-DD' 格式，返回 datetime 对象。
    """
    return parse_keyword_publish_date(value)


def in_date_range(publish_time: str, start_dt: datetime, end_dt: datetime) -> bool:
    """
    判断发布时间是否处于给定的开始日期和结束日期范围之内。
    """
    publish_dt = parse_publish_date(publish_time)
    if not publish_dt:
        return False
    return start_dt.date() <= publish_dt.date() <= end_dt.date()


def format_plain_text(value) -> str:
    """
    格式化纯文本内容，过滤掉空值或常见的 JS 空值占位符（如 'none', 'null', 'undefined', 'nan'）。
    """
    if value is None or isinstance(value, bool):
        return ""
    if isinstance(value, (dict, list, tuple)):
        return ""
    text = str(value).strip()
    return "" if text.lower() in {"none", "null", "undefined", "nan"} else text


def format_count(value) -> str:
    """
    格式化数值型或文本型统计数据，过滤空值占位符，并将形如 '1.2K', '3.4M' 的数值统一归一化为完整整数。
    """
    if value is None or isinstance(value, bool):
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if isinstance(value, (dict, list, tuple)):
        return ""
    text = str(value).strip()
    if text.lower() in {"none", "null", "undefined", "nan"}:
        return ""
    return expand_compact_number(text)


def format_publish_time(value) -> str:
    """
    格式化发布时间。若传入为 Unix 时间戳则转换为 'YYYY-MM-DD HH:MM:SS'，否则转为清洗后的纯文本。
    """
    try:
        timestamp = int(value)
        if timestamp > 0:
            return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(timestamp))
    except Exception:
        pass
    return format_plain_text(value)


def iter_dicts(value):
    """
    递归生成器：遍历嵌套的 dict 和 list 结构，产出其中所有的字典对象，用于解析页面复杂状态。
    """
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from iter_dicts(child)
    elif isinstance(value, list):
        for child in value:
            yield from iter_dicts(child)


def parse_script_json(html: str, script_id: str):
    """
    使用正则表达式匹配并提取 HTML 页面中指定 ID 的 <script> 标签内的 JSON 内容，并进行反转义解析。
    """
    pattern = rf'<script[^>]+id=["\']{re.escape(script_id)}["\'][^>]*>(.*?)</script>'
    match = re.search(pattern, html, re.S)
    if not match:
        return None
    try:
        return json.loads(html_lib.unescape(match.group(1)).strip())
    except Exception:
        return None


def page_state_sources(page) -> list[dict]:
    """
    获取页面渲染状态源。分别从 Playwright window 对象和 HTML script 脚本节点中提取并反序列化 TikTok 数据状态。
    """
    sources: list[dict] = []
    try:
        raw = page.evaluate(
            """() => JSON.stringify({
                sigi: window.SIGI_STATE || null,
                universal: window.__UNIVERSAL_DATA_FOR_REHYDRATION__ || null,
                render: window.RENDER_DATA || null,
                tiktok_br: window.__TIKTOK_BR_EXPORTS__ || null
            })"""
        )
        if raw:
            data = json.loads(raw)
            if isinstance(data, dict):
                sources.append(data)
    except Exception:
        pass

    try:
        html = page.content()
        for script_id in ("SIGI_STATE", "__UNIVERSAL_DATA_FOR_REHYDRATION__", "RENDER_DATA", "__TIKTOK_BR_EXPORTS__"):
            data = parse_script_json(html, script_id)
            if isinstance(data, dict):
                sources.append(data)
    except Exception:
        pass
    return sources


def find_item_in_state(sources: list[dict], video_id: str) -> dict:
    """
    在多份页面状态源中深度检索匹配指定 video_id 的视频详情字典（支持 ItemModule、itemStruct 等多种嵌套布局）。
    """
    if not video_id:
        return {}
    for source in sources:
        for item_module_key in ("ItemModule", "itemModule"):
            item_module = source.get(item_module_key)
            if isinstance(item_module, dict):
                item = item_module.get(video_id)
                if isinstance(item, dict):
                    return item
        for node in iter_dicts(source):
            item_struct = node.get("itemStruct")
            if isinstance(item_struct, dict) and str(item_struct.get("id", "")) == video_id:
                return item_struct
            if str(node.get("id", "")) == video_id and ("stats" in node or "createTime" in node or "desc" in node):
                return node
    return {}


def item_metric(item: dict, *keys: str) -> str:
    """
    从提取出的视频详情字典中，依次匹配候选的统计字段键（例如 diggCount, stats.diggCount 等），并归一化输出。
    """
    stats_sources = []
    for key in ("stats", "statsV2", "stats_v2", "statistics"):
        value = item.get(key)
        if isinstance(value, dict):
            stats_sources.append(value)
    stats_sources.append(item)
    for source in stats_sources:
        for key in keys:
            if key in source:
                value = format_count(source.get(key))
                if value:
                    return value
    return ""


def extract_metric(page, data_e2e_candidates, removable_words=(), default=""):
    """
    DOM 降级兜底方案：从页面中定位指定 data-e2e 候选元素并解析提取其文本统计值。
    """
    candidates = data_e2e_candidates if isinstance(data_e2e_candidates, (list, tuple)) else [data_e2e_candidates]
    for data_e2e in candidates:
        try:
            loc = page.locator(f"[data-e2e='{data_e2e}']").first
            if loc.count() <= 0:
                continue
            text = loc.inner_text(timeout=2500).strip()
            for word in removable_words:
                text = text.replace(word, "")
            text = text.strip()
            if text:
                return expand_compact_number(text)
        except Exception:
            continue
    return default


def clean_url(url: str) -> str:
    """
    对 URL 进行基础清洗，剥离查询参数与哈希锚点，补全协议头。
    """
    value = (url or "").strip()
    if not value:
        return ""
    if value.startswith("//"):
        value = "https:" + value
    if value.startswith("/"):
        value = "https://www.tiktok.com" + value
    if not value.startswith("http"):
        value = "https://" + value
    return value.split("?")[0].split("#")[0].rstrip("/")


def normalize_profile_url(url: str) -> str:
    """
    将博主主页链接归一化为标准的 https://www.tiktok.com/@username 格式。
    """
    cleaned = clean_url(url)
    match = re.search(r"tiktok\.com/(@[^/?#]+)", cleaned)
    return f"https://www.tiktok.com/{match.group(1)}" if match else ""


def parse_profile_urls(txt_path: str) -> list[str]:
    """
    从 TXT 文件中读取并清洗出所有不重复的 TikTok 博主主页 URL。
    """
    urls: list[str] = []
    seen = set()
    with open(txt_path, "r", encoding="utf-8-sig") as file:
        for line in file:
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            for part in re.split(r"\s+", stripped):
                profile_url = normalize_profile_url(part)
                if profile_url and profile_url not in seen:
                    urls.append(profile_url)
                    seen.add(profile_url)
                    break
    return urls


def parse_video_id(video_url: str) -> str:
    """
    从视频 URL 中提取唯一的纯数字视频 ID。
    """
    match = re.search(r"/video/(\d+)", video_url or "")
    return match.group(1) if match else ""


def normalize_video_url(url: str) -> str:
    """
    验证并归一化视频 URL 为标准格式，不合法的返回空字符串。
    """
    value = (url or "").strip()
    if value.startswith("//"):
        value = "https:" + value
    if value.startswith("/"):
        value = "https://www.tiktok.com" + value
    if not value.startswith("http"):
        return ""
    value = value.split("?")[0].split("#")[0].rstrip("/")
    if not re.search(r"tiktok\.com/@[^/?#]+/video/\d+", value):
        return ""
    return value


def trigger_profile_lazy_load(page, scroll_px=None) -> None:
    """
    触发博主主页视频列表的下拉懒加载：
    - 垂直滚动指定高度；
    - 对所有带有滚动条的 overflow 容器派发滚动事件，唤醒 TikTok 的列表渲染机制；
    - 结合 mouse.wheel 进行平滑向下滚动兜底。
    """
    if scroll_px is None:
        scroll_px = SCROLL_PX
    try:
        page.evaluate(
            f"""() => {{
                const scrolling = document.scrollingElement || document.documentElement || document.body;
                scrolling.scrollBy(0, {scroll_px});
                const scrollable = Array.from(document.querySelectorAll('body, main, section, div'))
                    .filter(el => {{
                        const style = getComputedStyle(el);
                        return el.scrollHeight > el.clientHeight + 80 &&
                            ['auto', 'scroll', 'overlay'].includes(style.overflowY);
                    }})
                    .sort((a, b) => (b.scrollHeight - b.clientHeight) - (a.scrollHeight - a.clientHeight));
                for (const el of scrollable.slice(0, 6)) {{
                    el.scrollBy(0, {scroll_px});
                    el.dispatchEvent(new Event('scroll', {{ bubbles: true }}));
                }}
                window.dispatchEvent(new Event('scroll'));
            }}"""
        )
    except Exception:
        pass
    try:
        page.mouse.wheel(0, scroll_px)
    except Exception:
        pass


def profile_handle_from_url(profile_url: str) -> str:
    match = re.search(r"tiktok\.com/@([^/?#]+)", profile_url or "", re.I)
    return match.group(1).strip().lstrip("@") if match else ""


def collect_visible_video_links(page, seen: set[str]) -> list[str]:
    """
    搜集当前主页视口中所有可见的视频链接。对抓取到的 URL 进行归一化并利用已见集合 seen 进行去重。
    """
    links: list[str] = []
    try:
        hrefs = page.evaluate(
            """() => Array.from(document.querySelectorAll("a[href*='/video/'], a[href*='video/']"))
                .map(node => node.href || node.getAttribute('href') || '')
                .filter(Boolean)"""
        )
    except Exception:
        hrefs = []

    for href in hrefs if isinstance(hrefs, list) else []:
        try:
            normalized = normalize_video_url(str(href))
        except Exception:
            normalized = ""
        if normalized and normalized not in seen:
            seen.add(normalized)
            links.append(normalized)

    default_handle = profile_handle_from_url(getattr(page, "url", ""))
    try:
        sources = page_state_sources(page)
    except Exception:
        sources = []
    for source in sources:
        for node in iter_dicts(source):
            normalized = _state_item_video_url(node, default_handle)
            if normalized and normalized not in seen:
                seen.add(normalized)
                links.append(normalized)
    return links



def item_detail_from_state(page, video_url: str) -> dict:
    """
    从页面提取出 video_url 对应视频 ID 的 JSON 数据对象。
    """
    video_id = parse_video_id(video_url)
    return find_item_in_state(page_state_sources(page), video_id)


def extract_video_detail(page, video_url: str, detail_load_timeout=None) -> dict[str, str]:
    """
    打开视频详情页，提取并清洗视频关键指标（点赞量、评论量、收藏量、分享量、发布时间、视频描述描述等）。
    支持重试加载 JSON Rehydration 状态，并对 DOM 进行兜底解析以规避 JS 反爬风控带来的数据缺失。
    """
    if detail_load_timeout is None:
        detail_load_timeout = DETAIL_LOAD_TIMEOUT
    page.goto(video_url, wait_until="domcontentloaded", timeout=detail_load_timeout)
    try:
        page.wait_for_selector(
            "script#__UNIVERSAL_DATA_FOR_REHYDRATION__, script#SIGI_STATE, script#RENDER_DATA, [data-e2e='like-count'], [data-e2e='browser-nickname']",
            timeout=8000,
        )
    except Exception:
        pass

    item = None
    for _ in range(4):
        item = item_detail_from_state(page, video_url)
        if item and (item.get("createTime") or item.get("create_time")):
            break
        page.wait_for_timeout(1000)

    desc = format_plain_text(item.get("desc") or item.get("description")) if item else ""
    publish_time = format_publish_time(item.get("createTime") or item.get("create_time")) if item else ""
    
    if not publish_time:
        vid = parse_video_id(video_url)
        if vid and vid.isdigit():
            try:
                unix_ts = int(vid) >> 32
                if unix_ts > 1500000000:
                    publish_time = format_publish_time(unix_ts)
            except Exception:
                pass
    likes = item_metric(item, "diggCount", "digg_count", "digg_count_str", "likeCount", "like_count", "like_count_str") if item else ""
    comments = item_metric(item, "commentCount", "comment_count", "comments") if item else ""
    collects = item_metric(
        item,
        "collectCount",
        "collect_count",
        "favoriteCount",
        "favouriteCount",
        "favorite_count",
        "favourite_count",
        "saveCount",
        "save_count",
    ) if item else ""
    shares = item_metric(
        item,
        "shareCount",
        "share_count",
        "share_count_str",
        "shares",
    ) if item else ""

    if not desc:
        desc = extract_tiktok_video_title(page)
    if not likes:
        likes = extract_metric(page, "like-count", ["Likes", "Like", "赞", " "])
    if not comments:
        comments = extract_metric(page, "comment-count", ["Comments", "Comment", "评论", " "])
    if not collects:
        collects = extract_metric(
            page,
            ["favorite-count", "undefined-count"],
            ["Favorites", "Favorite", "Favourites", "Favourite", "收藏", " "],
        )
    if not shares:
        shares = extract_metric(
            page,
            "share-count",
            ["Shares", "Share", "分享", " "],
        )

    return {
        "video_url": video_url,
        "desc": desc,
        "published_at": publish_time,
        "likes": format_count(likes),
        "comments": format_count(comments),
        "collects": format_count(collects),
        "shares": format_count(shares),
    }


def row_from_detail(index: int, detail: dict[str, str], play_count: str = "") -> dict[str, str]:
    """
    根据提取的详情字典以及额外的播放量指标拼装成与 Excel 列对应的结构化数据字典。
    """
    row = {
        "序号": str(index),
        "视频链接": detail.get("video_url", ""),
    }
    if play_count or "播放量" in detail:
        row["播放量"] = str(play_count or detail.get("播放量", ""))
    row.update({
        "发布日期": detail.get("published_at", ""),
        "视频简介": detail.get("desc", ""),
        "点赞数": detail.get("likes", ""),
        "评论数": detail.get("comments", ""),
        "收藏量": detail.get("collects", ""),
        "分享数": detail.get("shares", ""),
    })
    return row


def wait_after_detail(log_callback, stop_event=None, pause_event=None,
                      detail_delay_min=None, detail_delay_max=None) -> bool:
    """
    爬取每条视频后的冷却时间，以防短时间内频繁请求被 TikTok 拦截。
    """
    if detail_delay_min is None:
        detail_delay_min = DETAIL_DELAY_MIN_SECONDS
    if detail_delay_max is None:
        detail_delay_max = DETAIL_DELAY_MAX_SECONDS
    if wait_if_paused(pause_event, stop_event):
        return True
    seconds = random.uniform(detail_delay_min, detail_delay_max)
    return interruptible_sleep(seconds, stop_event)


def process_video_batch(
    detail_page,
    video_links: list[str],
    start_dt: datetime | None,
    end_dt: datetime | None,
    limit_time_bool: bool,
    get_video_info_bool: bool,
    get_comments_bool: bool,
    max_comments: int,
    writer,
    serial_number: int,
    written_count: int,
    log_callback,
    stop_event=None,
    pause_event=None,
    save_batch_size: int = SAVE_BATCH_SIZE,
    batch_wait_min: float = BATCH_WAIT_MIN_SECONDS,
    batch_wait_max: float = BATCH_WAIT_MAX_SECONDS,
    processed_count: int = 0,
    play_counts_map: dict[str, int] = None,
    fetch_play_counts_bool: bool = False,
    detail_load_timeout=None,
    detail_delay_min=None,
    detail_delay_max=None,
    writer_lock=None,
    global_state=None,
) -> tuple[int, int, bool, int]:
    """
    批量爬取当前收集的视频链接：
    - 根据各项 boolean 参数抓取详情、播放量、或主楼评论；
    - 执行严格的发布时间范围限流校验：如果在指定时间段外，且当前已抓取数大于 MIN_GUARANTEED_VIDEOS 保底要求，将立即触发当前主页的停止动作；
    - 针对分批写入实施随机等待降温冷却，降低 IP 被封锁概率。
    """
    stop_profile = False
    batch_written = 0
    log_line(log_callback, f"  开始爬取本批 {len(video_links)} 条视频。")

    for batch_index, video_url in enumerate(video_links, 1):
        if should_stop(stop_event):
            break
        if wait_if_paused(pause_event, stop_event):
            break
        try:
            log_line(log_callback, f"    [{batch_index}/{len(video_links)}] 读取视频：{video_url}")

            detail = {"video_url": video_url}
            if get_video_info_bool or get_comments_bool or limit_time_bool:
                detail = extract_video_detail(detail_page, video_url, detail_load_timeout=detail_load_timeout)
                published_at = detail.get("published_at", "")

                # 发布日期范围校验
                if limit_time_bool and start_dt and end_dt:
                    publish_dt = parse_publish_date(published_at)
                    if publish_dt and publish_dt.date() < start_dt.date():
                        # 超出日期下限，若已满足保底抓取视频条数，则中断该主页的进一步抓取
                        if processed_count >= MIN_GUARANTEED_VIDEOS:
                            log_line(log_callback, f"      停止当前主页：视频发布时间早于开始日期（{published_at}）。")
                            stop_profile = True
                            wait_after_detail(log_callback, stop_event, pause_event=pause_event, detail_delay_min=detail_delay_min, detail_delay_max=detail_delay_max)
                            break
                        else:
                            log_warn(log_callback, f"      跳过：发布时间超出范围（{published_at}），当前在保底前 {MIN_GUARANTEED_VIDEOS} 条内，不终止。")
                            processed_count += 1
                            if wait_after_detail(log_callback, stop_event, pause_event=pause_event, detail_delay_min=detail_delay_min, detail_delay_max=detail_delay_max):
                                break
                            continue

                    if not published_at:
                        log_warn(log_callback, "      警告：视频信息加载不完全，无法获取发布时间，为防止误杀予以放行。")
                    elif not in_date_range(published_at, start_dt, end_dt):
                        log_warn(log_callback, f"      跳过：发布时间不在范围内（{published_at}）。")
                        processed_count += 1
                        if wait_after_detail(log_callback, stop_event, pause_event=pause_event, detail_delay_min=detail_delay_min, detail_delay_max=detail_delay_max):
                            break
                        continue

            processed_count += 1
            vid = parse_video_id(video_url)
            play_count = ""
            # 获取拦截 API 拿到的播放量映射值
            if fetch_play_counts_bool and play_counts_map and vid in play_counts_map:
                play_count = str(play_counts_map[vid])
                
            if global_state is not None and writer_lock is not None:
                with writer_lock:
                    current_serial = global_state["serial_number"]
                    global_state["serial_number"] += 1
            else:
                current_serial = serial_number
                serial_number += 1

            row_base = row_from_detail(current_serial, detail, play_count) if get_video_info_bool else {"序号": str(current_serial), "视频链接": video_url}
            if fetch_play_counts_bool and not get_video_info_bool:
                row_base["播放量"] = play_count

            # 抓取视频的主楼评论
            if get_comments_bool:
                comments = collect_video_comments(detail_page, video_url, max_comments, log_callback, stop_event, pause_event=pause_event)
                if writer_lock is not None:
                    with writer_lock:
                        writer.writerow("视频信息", sanitize_csv_row(row_base))
                        for comment in comments:
                            comment_row = {
                                "序号": str(current_serial),
                                "视频链接": video_url,
                                "评论的点赞量": comment.get("like_count", ""),
                                "评论内容": comment.get("text", ""),
                                "发布时间": comment.get("create_time", "")
                            }
                            writer.writerow("评论信息", sanitize_csv_row(comment_row))
                else:
                    writer.writerow("视频信息", sanitize_csv_row(row_base))
                    for comment in comments:
                        comment_row = {
                            "序号": str(current_serial),
                            "视频链接": video_url,
                            "评论的点赞量": comment.get("like_count", ""),
                            "评论内容": comment.get("text", ""),
                            "发布时间": comment.get("create_time", "")
                        }
                        writer.writerow("评论信息", sanitize_csv_row(comment_row))
                        
                if global_state is not None and writer_lock is not None:
                    with writer_lock:
                        global_state["written_count"] += 1
                else:
                    written_count += 1
                batch_written += 1
                log_line(
                    log_callback,
                    f"      写入：点赞 {detail.get('likes') or '空'}，评论 {detail.get('comments') or '空'}，收藏 {detail.get('collects') or '空'}，分享 {detail.get('shares') or '空'}，抓取到主楼评论 {len(comments)} 条。",
                )
            else:
                if writer_lock is not None:
                    with writer_lock:
                        writer.writerow(sanitize_csv_row(row_base))
                else:
                    writer.writerow(sanitize_csv_row(row_base))
                if global_state is not None and writer_lock is not None:
                    with writer_lock:
                        global_state["written_count"] += 1
                else:
                    written_count += 1
                batch_written += 1
                if get_video_info_bool:
                    log_line(
                        log_callback,
                        f"      写入：点赞 {detail.get('likes') or '空'}，评论 {detail.get('comments') or '空'}，收藏 {detail.get('collects') or '空'}，分享 {detail.get('shares') or '空'}。",
                    )
                else:
                    log_line(log_callback, f"      写入视频链接：{video_url}")
            # 当写入条数到达分批尺寸，进行较长时间的冷却降温
            if batch_written >= save_batch_size:
                if wait_if_paused(pause_event, stop_event):
                    break
                seconds = random.uniform(batch_wait_min, batch_wait_max)
                log_line(log_callback, f"    已写入 {written_count} 条，随机等待 {seconds:.1f} 秒。")
                if interruptible_sleep(seconds, stop_event):
                    break
                batch_written = 0
        except Exception as exc:
            log_warn(log_callback, f"      跳过：{exc}")

        if wait_after_detail(log_callback, stop_event, pause_event=pause_event, detail_delay_min=detail_delay_min, detail_delay_max=detail_delay_max):
            break

    return serial_number, written_count, stop_profile, processed_count


def collect_profile_video_details(
    profile_page,
    detail_page,
    profile_url: str,
    start_dt,
    end_dt,
    limit_time_bool: bool,
    log_callback,
    stop_event=None,
    pause_event=None,
    max_scrolls: int = DEFAULT_MAX_SCROLLS,
    max_collect: int = 200,
    page_load_timeout: int = PAGE_LOAD_TIMEOUT,
    scroll_interval: float = SCROLL_INTERVAL_SECONDS,
    no_new_scroll_limit: int = NO_NEW_SCROLL_LIMIT,
    scroll_px: int = SCROLL_PX,
    detail_load_timeout: int = DETAIL_LOAD_TIMEOUT,
    detail_delay_min: float = DETAIL_DELAY_MIN_SECONDS,
    detail_delay_max: float = DETAIL_DELAY_MAX_SECONDS,
) -> list[dict[str, str]]:
    """
    访问单个 TikTok 博主主页，滚动采集视频链接并逐条提取详情，返回结果列表。
    作为 helper 供关键词作者作品工具和话题作者作品工具复用。
    """
    normalized_profile_url = normalize_profile_url(profile_url)
    if not normalized_profile_url:
        raise ValueError(f"无效的 TikTok 博主主页链接：{profile_url}")

    details: list[dict[str, str]] = []
    seen_links: set[str] = set()
    no_new_count = 0
    processed_count = 0

    profile_page.goto(normalized_profile_url, wait_until="domcontentloaded", timeout=page_load_timeout)
    try:
        profile_page.wait_for_selector(
            "a[href*='/video/'], a[href*='video/'], script#__UNIVERSAL_DATA_FOR_REHYDRATION__, script#SIGI_STATE, script#RENDER_DATA",
            timeout=min(int(page_load_timeout), 15000),
        )
    except Exception:
        pass
    interruptible_sleep(2.0, stop_event)

    for scroll_index in range(max(1, int(max_scrolls or DEFAULT_MAX_SCROLLS))):
        if should_stop(stop_event):
            break
        if wait_if_paused(pause_event, stop_event):
            break
        if len(details) >= max_collect:
            break

        new_links = collect_visible_video_links(profile_page, seen_links)
        if new_links:
            no_new_count = 0
            log_line(log_callback, f"  滚动第 {scroll_index + 1}/{max_scrolls} 轮：发现 {len(new_links)} 条新视频链接。")
        else:
            no_new_count += 1

        for video_url in new_links:
            if should_stop(stop_event) or len(details) >= max_collect:
                break
            if wait_if_paused(pause_event, stop_event):
                break
            try:
                detail = extract_video_detail(detail_page, video_url, detail_load_timeout=detail_load_timeout)
                published_at = detail.get("published_at", "")
                if limit_time_bool and start_dt and end_dt:
                    publish_dt = parse_publish_date(published_at)
                    if publish_dt and publish_dt.date() < start_dt.date() and processed_count >= MIN_GUARANTEED_VIDEOS:
                        log_line(log_callback, f"  发布时间早于范围起点（{published_at}），停止当前主页采集。")
                        return details
                    if published_at and not in_date_range(published_at, start_dt, end_dt):
                        processed_count += 1
                        if wait_after_detail(log_callback, stop_event, pause_event=pause_event, detail_delay_min=detail_delay_min, detail_delay_max=detail_delay_max):
                            return details
                        continue
                details.append(detail)
                processed_count += 1
                log_line(log_callback, f"    已采集 {len(details)}/{max_collect}: {video_url}")
                if wait_after_detail(log_callback, stop_event, pause_event=pause_event, detail_delay_min=detail_delay_min, detail_delay_max=detail_delay_max):
                    return details
            except Exception as exc:
                log_warn(log_callback, f"    提取详情失败：{exc}")

        if no_new_count >= no_new_scroll_limit:
            log_warn(log_callback, "  连续多次未发现新视频链接，结束当前主页采集。")
            break

        trigger_profile_lazy_load(profile_page, scroll_px=scroll_px)
        if interruptible_sleep(scroll_interval, stop_event):
            break

    return details


def run_tiktok_profile_videos_spider(
    txt_path: str,
    start_date: str,
    end_date: str,
    limit_time_str: str,
    max_scrolls: int,
    get_video_info_str: str,
    get_comments_str: str,
    max_comments: int,
    fetch_play_counts_str: str,
    cdp_port_or_url: str,
    log_callback,
    finish_callback,
    stop_event=None,
    pause_event=None,
    config=None,
):
    """
    TikTok 博主主页视频抓取爬虫入口函数。
    """
    if config is None:
        config = {}
    page_load_timeout = int(config.get("page_load_timeout", PAGE_LOAD_TIMEOUT))
    scroll_interval = float(config.get("scroll_interval", SCROLL_INTERVAL_SECONDS))
    no_new_scroll_limit = int(config.get("no_new_scroll_limit", NO_NEW_SCROLL_LIMIT))
    max_scrolls = int(config.get("max_scrolls", max_scrolls))
    link_batch_size = int(config.get("link_batch_size", LINK_BATCH_SIZE))
    save_batch_size = int(config.get("save_batch_size", SAVE_BATCH_SIZE))
    batch_wait_min = float(config.get("cooldown_min", BATCH_WAIT_MIN_SECONDS))
    batch_wait_max = float(config.get("cooldown_max", BATCH_WAIT_MAX_SECONDS))
    detail_load_timeout_val = int(config.get("detail_load_timeout", DETAIL_LOAD_TIMEOUT))
    detail_delay_min_val = float(config.get("detail_delay_min", DETAIL_DELAY_MIN_SECONDS))
    detail_delay_max_val = float(config.get("detail_delay_max", DETAIL_DELAY_MAX_SECONDS))
    scroll_px_val = int(config.get("scroll_px", SCROLL_PX))
    max_parallel_tabs = int(config.get("max_parallel_tabs", 3))

    output_path = None
    completed_path = None
    try:
        if sync_playwright is None:
            log_line(log_callback, "缺少依赖：playwright。请先安装 requirements.txt 中的依赖。")
            return

        profile_urls = parse_profile_urls(txt_path)
        if not profile_urls:
            log_warn(log_callback, "TXT 中没有找到有效的 TikTok 博主主页链接。")
            return

        limit_time_bool = (limit_time_str == "是")
        get_video_info_bool = (get_video_info_str == "是")
        get_comments_bool = (get_comments_str == "是")

        start_dt = None
        end_dt = None
        if limit_time_bool:
            start_dt, end_dt = parse_date_range(start_date, end_date)

        fetch_play_counts_bool = (fetch_play_counts_str == "是")

        video_fields = ["序号", "视频链接"]
        if fetch_play_counts_bool:
            video_fields.append("播放量")
        if get_video_info_bool:
            video_fields.extend(["发布日期", "视频简介", "点赞数", "评论数", "收藏量", "分享数"])

        output_path = build_output_path("tiktok", f"tiktok_profile_videos_{time.strftime('%Y%m%d_%H%M%S')}.xlsx")
        if get_comments_bool:
            comment_fields = ["序号", "视频链接", "评论的点赞量", "评论内容", "发布时间"]
            writer = MultiSheetXlsxWriter(output_path, {"视频信息": video_fields, "评论信息": comment_fields})
        else:
            writer = XlsxRowWriter(output_path, video_fields)

        writer_lock = threading.Lock()
        log_lock = threading.Lock()
        global_state = {"serial_number": 1, "written_count": 0}
        
        actual_max_scrolls = max_scrolls if max_scrolls > 0 else 999999
        no_new_limit = 5 if not limit_time_bool else no_new_scroll_limit

        log_line(log_callback, "正在连接并拉起 Chrome...")
        from src.core import ensure_chrome_for_cdp
        ensure_chrome_for_cdp(cdp_port_or_url, log_callback=log_callback)

        def make_thread_log(base_log_callback, lock, prefix):
            def wrapped(msg):
                if base_log_callback:
                    with lock:
                        base_log_callback(f"[{prefix}] {msg}")
            return wrapped

        def worker(raw_profile_url, index):
            if should_stop(stop_event):
                return
            profile_url = normalize_profile_url(raw_profile_url)
            if not profile_url:
                with log_lock:
                    log_warn(log_callback, f"[{index}/{len(profile_urls)}] 跳过无效主页：{raw_profile_url}")
                return

            match = re.search(r"tiktok\.com/(@[^/?#]+)", profile_url)
            username = match.group(1) if match else f"user_{index}"
            thread_log = make_thread_log(log_callback, log_lock, username)
            thread_log(f"[{index}/{len(profile_urls)}] 开始读取主页：{profile_url}")
            
            play_counts_map = {}
            def handle_response(response):
                if "/api/post/item_list" in response.url and "secUid" in response.url:
                    try:
                        text = response.text()
                        if text.strip():
                            body = json.loads(text)
                            for item in body.get("itemList", []):
                                vid = item.get("id", "")
                                if vid:
                                    stats = item.get("stats", {})
                                    play_counts_map[vid] = stats.get("playCount", 0)
                    except Exception:
                        pass

            browser = None
            profile_page = None
            detail_page = None
            try:
                with sync_playwright() as playwright:
                    browser, context = connect_existing_chromium(playwright, cdp_port_or_url, log_callback=thread_log)
                    profile_page = context.new_page()
                    detail_page = context.new_page()

                    if fetch_play_counts_bool:
                        profile_page.on("response", handle_response)
                    
                    profile_page.goto(profile_url, wait_until="domcontentloaded", timeout=page_load_timeout)
                    interruptible_sleep(2.5, stop_event)

                    seen_links: set[str] = set()
                    pending_links: list[str] = []
                    no_new_count = 0
                    stop_profile = False
                    processed_count = 0

                    for scroll_index in range(actual_max_scrolls):
                        if should_stop(stop_event):
                            break
                        if wait_if_paused(pause_event, stop_event):
                            break

                        new_links = collect_visible_video_links(profile_page, seen_links)
                        if new_links:
                            no_new_count = 0
                            thread_log(f"  滚动 {scroll_index + 1}/{actual_max_scrolls}：发现 {len(new_links)} 条新视频链接。")
                            pending_links.extend(new_links)
                        else:
                            no_new_count += 1

                        while len(pending_links) >= link_batch_size and not stop_profile and not should_stop(stop_event):
                            batch = pending_links[:link_batch_size]
                            del pending_links[:link_batch_size]
                            _, _, stop_profile, processed_count = process_video_batch(
                                detail_page,
                                batch,
                                start_dt,
                                end_dt,
                                limit_time_bool,
                                get_video_info_bool,
                                get_comments_bool,
                                max_comments,
                                writer,
                                0,
                                0,
                                thread_log,
                                stop_event,
                                pause_event=pause_event,
                                save_batch_size=save_batch_size,
                                batch_wait_min=batch_wait_min,
                                batch_wait_max=batch_wait_max,
                                processed_count=processed_count,
                                play_counts_map=play_counts_map,
                                fetch_play_counts_bool=fetch_play_counts_bool,
                                detail_load_timeout=detail_load_timeout_val,
                                detail_delay_min=detail_delay_min_val,
                                detail_delay_max=detail_delay_max_val,
                                writer_lock=writer_lock,
                                global_state=global_state,
                            )
                        if stop_profile:
                            break

                        if no_new_count >= no_new_limit:
                            if pending_links and not should_stop(stop_event):
                                _, _, stop_profile, processed_count = process_video_batch(
                                    detail_page,
                                    pending_links,
                                    start_dt,
                                    end_dt,
                                    limit_time_bool,
                                    get_video_info_bool,
                                    get_comments_bool,
                                    max_comments,
                                    writer,
                                    0,
                                    0,
                                    thread_log,
                                    stop_event,
                                    pause_event=pause_event,
                                    save_batch_size=save_batch_size,
                                    batch_wait_min=batch_wait_min,
                                    batch_wait_max=batch_wait_max,
                                    processed_count=processed_count,
                                    play_counts_map=play_counts_map,
                                    fetch_play_counts_bool=fetch_play_counts_bool,
                                    detail_load_timeout=detail_load_timeout_val,
                                    detail_delay_min=detail_delay_min_val,
                                    detail_delay_max=detail_delay_max_val,
                                    writer_lock=writer_lock,
                                    global_state=global_state,
                                )
                                pending_links = []
                            thread_log("  连续多次没有新视频链接，结束当前主页。")
                            break

                        trigger_profile_lazy_load(profile_page, scroll_px=scroll_px_val)
                        if interruptible_sleep(scroll_interval, stop_event):
                            break

                    if pending_links and not stop_profile and not should_stop(stop_event):
                        _, _, stop_profile, processed_count = process_video_batch(
                            detail_page,
                            pending_links,
                            start_dt,
                            end_dt,
                            limit_time_bool,
                            get_video_info_bool,
                            get_comments_bool,
                            max_comments,
                            writer,
                            0,
                            0,
                            thread_log,
                            stop_event,
                            pause_event=pause_event,
                            save_batch_size=save_batch_size,
                            batch_wait_min=batch_wait_min,
                            batch_wait_max=batch_wait_max,
                            processed_count=processed_count,
                            play_counts_map=play_counts_map,
                            fetch_play_counts_bool=fetch_play_counts_bool,
                            detail_load_timeout=detail_load_timeout_val,
                            detail_delay_min=detail_delay_min_val,
                            detail_delay_max=detail_delay_max_val,
                            writer_lock=writer_lock,
                            global_state=global_state,
                        )
            except Exception as exc:
                thread_log(f"  发生异常: {exc}")
            finally:
                if fetch_play_counts_bool and profile_page:
                    try:
                        profile_page.remove_listener("response", handle_response)
                    except Exception:
                        pass
                for opened_page in (profile_page, detail_page):
                    if opened_page and not opened_page.is_closed():
                        try:
                            opened_page.close()
                        except Exception:
                            pass
                try:
                    if browser:
                        browser.close()
                except Exception:
                    pass

        with ThreadPoolExecutor(max_workers=max_parallel_tabs) as executor:
            futures = [executor.submit(worker, url, idx) for idx, url in enumerate(profile_urls, 1)]
            for future in as_completed(futures):
                if should_stop(stop_event):
                    for f in futures:
                        f.cancel()
                    break
                try:
                    future.result()
                except Exception as exc:
                    log_error(log_callback, f"线程执行异常: {exc}")

        with writer_lock:
            writer.save()
        completed_path = output_path
        log_line(log_callback, f"完成：写入 {global_state['written_count']} 条，已保存：{output_path}")
    finally:
        finish_callback(completed_path)


def _state_item_author_handle(item: dict) -> str:
    author = item.get("author") if isinstance(item, dict) else None
    if isinstance(author, dict):
        return format_plain_text(author.get("uniqueId") or author.get("unique_id") or author.get("username")).lstrip("@")
    if isinstance(author, str):
        return format_plain_text(author).lstrip("@")
    return ""


def _state_item_video_url(item: dict, default_handle: str) -> str:
    if not isinstance(item, dict):
        return ""
    video_id = format_plain_text(item.get("id") or item.get("itemId") or item.get("aweme_id"))
    if not video_id or not video_id.isdigit() or len(video_id) < 10:
        return ""
    if not (item.get("desc") or item.get("description") or item.get("createTime") or item.get("create_time") or item.get("video") or item.get("stats")):
        return ""
    author_handle = _state_item_author_handle(item)
    if default_handle and author_handle and author_handle.lower() != default_handle.lower():
        return ""
    handle = author_handle or default_handle
    if not handle:
        return ""
    return normalize_video_url(f"https://www.tiktok.com/@{handle}/video/{video_id}")

