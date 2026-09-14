"""
TikTok 上下文作品采集模块。
该模块用于获取指定“目标视频”在发布时间轴上相邻的前 N 条和后 N 条作品（即上下文视频）。
支持两种运行模式：
1. API 高速路线：在目标视频详情页中抽取 secUid 等元数据，通过调用 TikTok 后台 post/item_list 接口拉取用户投稿列表，在投稿列表中直接切片获取前后相邻的作品。
2. 浏览器兜底路线：若 secUid 解析失败或接口被封，自动开启 Playwright 浏览器降级通道，跳转至博主主页进行滚动加载，在页面 DOM 网格中定位目标视频并采集其前后邻近的视频卡片。
"""

from __future__ import annotations

import html as html_lib
import json
import re
import time
import urllib.parse
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

from playwright.sync_api import sync_playwright

from src.core import (
    XlsxRowWriter,
    build_output_path,
    connect_existing_chromium,
    expand_compact_number,
    extract_tiktok_video_title,
    log_error,
    log_line,
    log_warn,
    resolve_tiktok_card_container,
    sanitize_csv_rows,
    should_stop,
    wait_if_paused,
)

# 采集目标视频前后作品的数量（单边 N=5，即上下文共 10 条）
CONTEXT_SIZE = 5
API_PAGE_SIZE = 35                 # 接口单页请求的投稿数
MAX_API_PAGES = 10                 # 接口最大翻页限制
MAX_PROFILE_SCROLLS = 80           # 兜底主页最大滚动轮数
MIN_PROFILE_SCROLLS_BEFORE_STABLE_STOP = 12 # 停止无增长判断的最小滚动次数
PROFILE_SCROLL_DELTA = 1500        # 每次鼠标滚动像素增量
PROFILE_SCROLL_PAUSE = 0.8         # 每次滚动后的休眠间隔（秒）

CSV_FIELDS = [
    "博主链接",
    "目标视频链接",
    "视频链接",
    "时间轴关系",
    "视频标题",
    "发布时间",
    "播放量",
    "点赞数",
    "收藏数",
    "分享数",
    "评论数",
]

# 在过滤 UI 点赞/分享等数字指标时需要排除的混淆文本列表
METRIC_LABEL_WORDS = {
    "Like",
    "Likes",
    "Favorite",
    "Favorites",
    "Favourite",
    "Favourites",
    "Share",
    "Shares",
    "Comment",
    "Comments",
    "赞",
    "点赞",
    "收藏",
    "分享",
    "评论",
    "評論",
}

def parse_input_pairs(txt_path: str) -> list[tuple[str, str]]:
    """
    解析输入的 TXT 文本，提取“目标视频链接”与“博主主页链接”的多行映射对。
    若未提供博主链接，则自动根据视频 URL 格式生成默认的 @博主 主页。
    """
    pairs: list[tuple[str, str]] = []
    with open(txt_path, "r", encoding="utf-8") as f:
        for line in f:
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            parts = [part.strip() for part in stripped.split("\t") if part.strip()] if "\t" in stripped else stripped.split()
            if not parts:
                continue
            target_url = clean_url(parts[0])
            if "/video/" not in target_url:
                continue
            profile_url = clean_url(parts[1]) if len(parts) >= 2 else extract_profile_url_from_video_url(target_url)
            pairs.append((target_url, profile_url))
    return pairs

def clean_url(url: str) -> str:
    """
    清洗链接格式，补齐协议头并丢弃问号后的参数与哈希值。
    """
    url = (url or "").strip()
    if not url:
        return ""
    if url.startswith("//"):
        url = "https:" + url
    if url.startswith("/"):
        url = "https://www.tiktok.com" + url
    if not url.startswith("http"):
        url = "https://" + url
    return url.split("?")[0].split("#")[0].rstrip("/")

def extract_tiktok_video_id(url: str) -> str:
    """
    从视频跳转地址中截取纯数字视频 ID。
    """
    match = re.search(r"/video/(\d+)", url or "")
    return match.group(1) if match else ""

def extract_profile_url_from_video_url(url: str) -> str:
    """
    通过正则从视频地址中提取作者的 @句柄，拼装默认的主页 URL。
    """
    match = re.search(r"tiktok\.com/(@[^/?#]+)/video/\d+", url or "")
    return f"https://www.tiktok.com/{match.group(1)}" if match else ""

def handle_from_profile_url(profile_url: str) -> str:
    """
    正则截取主页链接中的 @账号句柄。
    """
    match = re.search(r"tiktok\.com/@([^/?#]+)", profile_url or "")
    return match.group(1) if match else ""

def unique_urls(urls: list[str]) -> list[str]:
    """
    对输入的候选主页 URL 列表进行有序去重。
    """
    unique: list[str] = []
    seen = set()
    for url in urls:
        cleaned = clean_url(url)
        if cleaned and cleaned not in seen:
            unique.append(cleaned)
            seen.add(cleaned)
    return unique

def relation_for_index(target_index: int, current_index: int) -> str:
    """
    根据数组索引偏差，推导出该视频相较于目标视频的时间轴关系。
    - 索引越小，表示越新发布，即在目标视频之后发布。
    - 索引越大，表示越旧发布，即在目标视频之前发布。
    """
    if current_index < target_index:
        return f"目标后发布第{target_index - current_index}条"
    return f"目标前发布第{current_index - target_index}条"

def format_count(value) -> str:
    """
    数据行指标统一格式化转换，使用 expand_compact_number 归一化缩写。
    """
    if value is None:
        return ""
    if isinstance(value, bool):
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if isinstance(value, (dict, list, tuple)):
        return ""
    text = str(value).strip()
    if text.lower() in {"none", "null", "undefined", "nan"}:
        return ""
    return expand_compact_number(text)

def format_plain_text(value) -> str:
    """
    清理文本中的非空异常（如 NaN 等字符）。
    """
    if value is None or isinstance(value, bool):
        return ""
    if isinstance(value, (dict, list, tuple)):
        return ""
    text = str(value).strip()
    return "" if text.lower() in {"none", "null", "undefined", "nan"} else text

def clean_metric_text(text: str, removable_words=()) -> str:
    """
    清除指标数字中包含的标签字词（如 Likes、Shares），返回纯整型或已转换的缩写指标。
    """
    cleaned = format_count(text).replace("\r", "\n")
    if not cleaned:
        return ""
    for word in sorted(set(METRIC_LABEL_WORDS) | set(removable_words), key=lambda value: len(str(value)), reverse=True):
        cleaned = re.sub(re.escape(str(word)), "", cleaned, flags=re.I)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" :-·|")
    if not cleaned:
        return ""
    if cleaned.lower() in {word.lower() for word in METRIC_LABEL_WORDS}:
        return ""
    return expand_compact_number(cleaned) if re.search(r"\d", cleaned) else ""

def format_publish_time(value) -> str:
    """
    将 10 位时间戳规整为 YYYY-MM-DD HH:MM:SS 日期时间。
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
    深度优先遍历任意嵌套字典或列表，生成其中所有的 dict 子节点。
    用于在网页反序列化状态树中进行深度模糊查找。
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
    正则匹配 HTML 中的指定 script 标签，提取并反序列化其中的 JSON 状态树数据（如 SIGI_STATE 树）。
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
    从页面中提取结构化元数据源。
    - 优先尝试从 window 全局变量 window.SIGI_STATE / window.__UNIVERSAL_DATA_FOR_REHYDRATION__ 读取并强转。
    - 若前置读取为空，则通过 page.content() 获取源码，正则匹配 script 标签获取。
    """
    sources: list[dict] = []
    try:
        raw = page.evaluate(
            """() => JSON.stringify({
                sigi: window.SIGI_STATE || null,
                universal: window.__UNIVERSAL_DATA_FOR_REHYDRATION__ || null
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
        for script_id in ("SIGI_STATE", "__UNIVERSAL_DATA_FOR_REHYDRATION__"):
            data = parse_script_json(html, script_id)
            if isinstance(data, dict):
                sources.append(data)
    except Exception:
        pass

    return sources

def find_item_in_state(sources: list[dict], target_video_id: str) -> dict:
    """
    从候选的多个结构化元数据源中模糊检索 target_video_id 所对应的视频项详情字典。
    """
    for source in sources:
        for item_module_key in ("ItemModule", "itemModule"):
            item_module = source.get(item_module_key)
            if isinstance(item_module, dict):
                item = item_module.get(target_video_id)
                if isinstance(item, dict):
                    return item

        for node in iter_dicts(source):
            item_struct = node.get("itemStruct")
            if isinstance(item_struct, dict) and str(item_struct.get("id", "")) == target_video_id:
                return item_struct
            if str(node.get("id", "")) == target_video_id and ("stats" in node or "createTime" in node or "desc" in node):
                return node
    return {}

def find_author_info(sources: list[dict], item: dict, fallback_profile_url: str) -> dict[str, str]:
    """
    从状态树中挖掘作者的关键属性（如 secUid、唯一标识 unique_id、以及 user_id），为后续发起 API 投稿列表请求提供凭证。
    """
    author = item.get("author") if isinstance(item, dict) else None
    author_id = format_plain_text(item.get("authorId") or item.get("author_id")) if isinstance(item, dict) else ""
    unique_id = ""
    sec_uid = ""
    user_id = ""

    if isinstance(author, dict):
        unique_id = format_plain_text(author.get("uniqueId") or author.get("unique_id"))
        sec_uid = format_plain_text(author.get("secUid") or author.get("sec_uid"))
        user_id = format_plain_text(author.get("id") or author.get("uid"))
    elif isinstance(author, str):
        unique_id = author

    fallback_handle = handle_from_profile_url(fallback_profile_url)
    expected_ids = {value for value in (unique_id, fallback_handle, author_id, user_id) if value}

    for source in sources:
        users = source.get("UserModule", {}).get("users", {}) if isinstance(source.get("UserModule"), dict) else {}
        if isinstance(users, dict):
            for user in users.values():
                if not isinstance(user, dict):
                    continue
                user_unique_id = format_plain_text(user.get("uniqueId") or user.get("unique_id"))
                user_id_value = format_plain_text(user.get("id") or user.get("uid"))
                if expected_ids and not ({user_unique_id, user_id_value} & expected_ids):
                    continue
                unique_id = unique_id or user_unique_id
                sec_uid = sec_uid or format_plain_text(user.get("secUid") or user.get("sec_uid"))
                user_id = user_id or user_id_value
                break

        for node in iter_dicts(source):
            if "secUid" not in node and "sec_uid" not in node:
                continue
            node_unique_id = format_plain_text(node.get("uniqueId") or node.get("unique_id"))
            node_user_id = format_plain_text(node.get("id") or node.get("uid"))
            if expected_ids and not ({node_unique_id, node_user_id} & expected_ids):
                continue
            unique_id = unique_id or node_unique_id
            sec_uid = sec_uid or format_plain_text(node.get("secUid") or node.get("sec_uid"))
            user_id = user_id or node_user_id
            if sec_uid:
                break
        if sec_uid:
            break

    unique_id = unique_id or fallback_handle
    profile_url = f"https://www.tiktok.com/@{unique_id}" if unique_id else fallback_profile_url
    return {
        "sec_uid": sec_uid,
        "unique_id": unique_id,
        "user_id": user_id or author_id,
        "profile_url": profile_url,
    }

def extract_target_metadata(page, target_video_id: str, fallback_profile_url: str) -> dict:
    """
    提取目标视频的详情与作者元数据。
    """
    sources = page_state_sources(page)
    item = find_item_in_state(sources, target_video_id)
    author_info = find_author_info(sources, item, fallback_profile_url)
    return {
        "item": item,
        **author_info,
    }

def resolve_target_video_context(page, target_video_url: str) -> tuple[str, str, str]:
    """
    分析并定位目标视频页的真实 URL 状态与作者信息。
    - 进入视频 URL 并等待核心脚本元素载入；
    - 适配可能发生的目标重定向 URL；
    - 从页面中解析出视频真实 ID 以及博主 profile_url；
    - 若没有获得，兜底利用正则扫描页面内的博主 a 标签进行抽取。
    """
    final_video_url = clean_url(target_video_url)
    final_video_id = extract_tiktok_video_id(final_video_url)
    profile_url = extract_profile_url_from_video_url(final_video_url)

    try:
        page.goto(target_video_url, wait_until="domcontentloaded", timeout=35000)
        try:
            page.wait_for_selector("script#__UNIVERSAL_DATA_FOR_REHYDRATION__, script#SIGI_STATE, [data-e2e='like-count']", timeout=8000)
        except Exception:
            pass
        redirected_url = clean_url(page.url)
        redirected_video_id = extract_tiktok_video_id(redirected_url)
        if redirected_video_id:
            final_video_url = redirected_url
            final_video_id = redirected_video_id

        current_profile = extract_profile_url_from_video_url(clean_url(page.url))
        if current_profile:
            profile_url = current_profile

        metadata = extract_target_metadata(page, final_video_id, profile_url)
        if metadata.get("profile_url"):
            profile_url = metadata["profile_url"]

        if not profile_url:
            for element in page.locator("a[href*='tiktok.com/@'], a[href^='/@']").all():
                href = clean_url(element.get_attribute("href") or "")
                if "/video/" not in href and "/tag/" not in href and "/music/" not in href:
                    match = re.search(r"tiktok\.com/(@[^/?#]+)", href)
                    if match:
                        profile_url = f"https://www.tiktok.com/{match.group(1)}"
                        break
    except Exception:
        pass

    return final_video_url, final_video_id, profile_url

def build_author_items_api_url(sec_uid: str, cursor: str, api_page_size: int = API_PAGE_SIZE) -> str:
    """
    生成拉取博主投稿视频列表的 API 拼接请求 URL，带上 secUid 参数以定位该用户的所有历史作品。
    """
    params = {
        "WebIdLastTime": str(int(time.time())),
        "aid": "1988",
        "app_language": "zh-Hans",
        "app_name": "tiktok_web",
        "browser_language": "zh-CN",
        "browser_name": "Mozilla",
        "browser_online": "true",
        "browser_platform": "Win32",
        "channel": "tiktok_web",
        "cookie_enabled": "true",
        "count": str(api_page_size),
        "cursor": str(cursor or "0"),
        "device_platform": "web_pc",
        "focus_state": "true",
        "from_page": "user",
        "history_len": "2",
        "is_fullscreen": "false",
        "is_page_visible": "true",
        "language": "zh-Hans",
        "priority_region": "",
        "referer": "",
        "region": "US",
        "screen_height": "1080",
        "screen_width": "1920",
        "secUid": sec_uid,
        "tz_name": "Asia/Shanghai",
        "verifyFp": "",
    }
    return "https://www.tiktok.com/api/post/item_list/?" + urllib.parse.urlencode(params)

def fetch_json_via_page(page, url: str, timeout_ms: int = 12000) -> dict:
    """
    在 Playwright 页面上下文中，利用页面内的 fetch 运行并解析 JSON 数据。
    """
    result = page.evaluate(
        """async ({url, timeoutMs}) => {
            const controller = new AbortController();
            const timer = setTimeout(() => controller.abort(), timeoutMs);
            try {
                const response = await fetch(url, {
                    credentials: 'include',
                    signal: controller.signal,
                    headers: {accept: 'application/json, text/plain, */*'}
                });
                const text = await response.text();
                return {status: response.status, ok: response.ok, text};
            } catch (error) {
                return {status: 0, ok: false, text: String(error)};
            } finally {
                clearTimeout(timer);
            }
        }""",
        {"url": url, "timeoutMs": timeout_ms},
    )
    if not isinstance(result, dict) or not result.get("ok"):
        raise RuntimeError(f"API 请求失败：HTTP {result.get('status') if isinstance(result, dict) else 'unknown'}")
    return json.loads(result.get("text") or "{}")

def item_id(item: dict) -> str:
    return format_plain_text(item.get("id") or item.get("itemId") or item.get("aweme_id"))

def item_author_handle(item: dict, default_profile_url: str) -> str:
    author = item.get("author")
    if isinstance(author, dict):
        handle = format_plain_text(author.get("uniqueId") or author.get("unique_id"))
        if handle:
            return handle
    if isinstance(author, str):
        return author
    return handle_from_profile_url(default_profile_url)

def item_video_url(item: dict, default_profile_url: str) -> str:
    video_id = item_id(item)
    handle = item_author_handle(item, default_profile_url)
    if video_id and handle:
        return f"https://www.tiktok.com/@{handle}/video/{video_id}"
    return ""

def item_metrics(item: dict) -> dict[str, str]:
    """
    统一的接口统计属性数据匹配器。
    """
    stats = item.get("stats") if isinstance(item.get("stats"), dict) else {}
    stats_v2 = item.get("statsV2") if isinstance(item.get("statsV2"), dict) else {}
    stats_v2_alt = item.get("stats_v2") if isinstance(item.get("stats_v2"), dict) else {}
    statistics = item.get("statistics") if isinstance(item.get("statistics"), dict) else {}

    def stat(*keys) -> str:
        for source in (stats, stats_v2, stats_v2_alt, statistics, item):
            for key in keys:
                if key in source:
                    value = format_count(source.get(key))
                    if value != "":
                        return value
        return ""

    return {
        "视频标题": format_plain_text(item.get("desc") or item.get("description")),
        "发布时间": format_publish_time(item.get("createTime") or item.get("create_time")),
        "播放量": stat("playCount", "play_count", "viewCount", "view_count", "play_count_str"),
        "点赞数": stat("diggCount", "digg_count", "likeCount", "like_count", "likes"),
        "收藏数": stat("collectCount", "collect_count", "favoriteCount", "favouriteCount", "favorite_count", "favourite_count", "saveCount", "save_count"),
        "分享数": stat("shareCount", "share_count", "shares"),
        "评论数": stat("commentCount", "comment_count", "comments"),
    }

def collect_author_items_via_api(page, sec_uid: str, target_video_id: str, log_callback, context_size: int = CONTEXT_SIZE, api_page_size: int = API_PAGE_SIZE, max_api_pages: int = MAX_API_PAGES) -> tuple[list[dict], int]:
    """
    使用 API 极速通道收集投稿。
    - 循环翻页请求博主视频列表；
    - 当发现投稿列表中已包含目标视频 ID，并且后续已读完 context_size 规定的视频数时，提前终止翻页以节省带宽和降低风控几率。
    - 返回收集到的投稿列表及目标视频在该列表中的索引 index。
    """
    items: list[dict] = []
    seen_ids: set[str] = set()
    cursor = "0"
    target_index = -1

    for page_index in range(max_api_pages):
        data = fetch_json_via_page(page, build_author_items_api_url(sec_uid, cursor, api_page_size=api_page_size))
        item_list = data.get("itemList") or data.get("items") or []
        if not isinstance(item_list, list) or not item_list:
            break

        before_count = len(items)
        for item in item_list:
            if not isinstance(item, dict):
                continue
            current_id = item_id(item)
            if not current_id or current_id in seen_ids:
                continue
            items.append(item)
            seen_ids.add(current_id)

        for index, item in enumerate(items):
            if item_id(item) == target_video_id:
                target_index = index
                break

        log_line(log_callback, f"  API 已收集 {len(items)} 条投稿记录。")
        if target_index >= 0 and len(items) >= target_index + context_size + 1:
            break
        if len(items) == before_count or not data.get("hasMore"):
            break

        next_cursor = format_plain_text(data.get("cursor") or data.get("maxCursor") or data.get("max_cursor"))
        if not next_cursor or next_cursor == cursor:
            break
        cursor = next_cursor

    return items, target_index


def rows_from_api_items(items: list[dict], target_index: int, profile_url: str, target_video_url: str, context_size: int = CONTEXT_SIZE) -> list[dict[str, str]]:
    """
    根据投稿列表和目标视频索引，计算并切片提取出目标前 N 和目标后 N 的视频，
    并把它们组装成符合表格输出定义的列表字典。
    """
    selected_indices = list(range(max(0, target_index - context_size), target_index))
    selected_indices += list(range(target_index + 1, min(len(items), target_index + context_size + 1)))

    rows: list[dict[str, str]] = []
    for current_index in selected_indices:
        item = items[current_index]
        video_url = item_video_url(item, profile_url)
        if not video_url:
            continue
        rows.append({
            "博主链接": profile_url,
            "目标视频链接": target_video_url,
            "视频链接": video_url,
            "时间轴关系": relation_for_index(target_index, current_index),
            **item_metrics(item),
        })
    return rows

def extract_card_play_count(card_element) -> str:
    """
    DOM 模式：从主页视频网格的单张卡片节点中，利用 querySelector 解析播放量。
    """
    try:
        container = resolve_tiktok_card_container(card_element)
        for selector in ("[data-e2e='video-views']", "strong[data-e2e='video-views']"):
            node = container.query_selector(selector)
            if node:
                text = node.inner_text().strip()
                if text:
                    return expand_compact_number(text)
    except Exception:
        pass
    return ""

def collect_visible_profile_video_links(page) -> list[str]:
    """
    DOM 模式：获取当前博主主页网格中所有已渲染的视频卡片跳转超链接。
    """
    try:
        hrefs = page.evaluate(
            """() => Array.from(document.querySelectorAll("a[href*='/video/'], a[href*='video/']"))
                .map(node => node.href || node.getAttribute('href') || '')
                .filter(Boolean)"""
        )
    except Exception:
        hrefs = []

    links: list[str] = []
    seen = set()
    for href in hrefs if isinstance(hrefs, list) else []:
        cleaned = clean_url(str(href))
        if "/video/" in cleaned and cleaned not in seen:
            links.append(cleaned)
            seen.add(cleaned)
    return links

def collect_profile_video_links(page, profile_url: str, target_video_id: str, log_callback, stop_event=None, pause_event=None, context_size: int = CONTEXT_SIZE, max_profile_scrolls: int = MAX_PROFILE_SCROLLS, profile_scroll_pause: float = PROFILE_SCROLL_PAUSE) -> tuple[list[str], int]:
    """
    DOM 模式主页网格滚动逻辑。
    - 用 Playwright 打开博主主页，检测人机验证码；
    - 模拟鼠标中键不断向下滑动页面；
    - 不断读取 DOM 中的视频链接，当目标视频 ID 出现并且后续网格卡片加载数量大于 context_size 时，表明上下文的 DOM 节点均已渲染完毕，此时立刻主动退出滚动，避免多余的资源消耗与防范反爬虫风控。
    """
    try:
        page.goto(profile_url, wait_until="domcontentloaded", timeout=60000)
    except Exception as exc:
        if "interrupted by another navigation" not in str(exc):
            raise
        log_line(log_callback, "  主页导航被 TikTok 自动跳转打断，正在重置页面后重试。")
        try:
            page.goto("about:blank", wait_until="domcontentloaded", timeout=10000)
        except Exception:
            pass
        page.goto(profile_url, wait_until="domcontentloaded", timeout=60000)

    time.sleep(2.0)
    # 人机验证码拦截等待
    try:
        if "captcha" in page.url or page.locator("div[id^='captcha']").count() > 0:
            log_line(log_callback, "  发现 TikTok 验证页，等待 15 秒给你手动处理。")
            time.sleep(15)
    except Exception:
        pass

    try:
        page.wait_for_selector("a[href*='/video/'], a[href*='video/']", timeout=7000)
    except Exception:
        pass

    try:
        page.mouse.move(page.evaluate("window.innerWidth") / 2, page.evaluate("window.innerHeight") / 2)
    except Exception:
        pass

    all_links: list[str] = []
    target_index = -1
    no_growth_count = 0

    for scroll_index in range(max_profile_scrolls):
        if should_stop(stop_event):
            break
        if wait_if_paused(pause_event, stop_event):
            break
        previous_links = all_links
        current_links = collect_visible_profile_video_links(page)
        if current_links:
            all_links = current_links

        previous_count = len(previous_links)

        current_target_index = -1
        for index, link in enumerate(all_links):
            if target_video_id and target_video_id in link:
                current_target_index = index
                break
        target_index = current_target_index

        # 网格卡片渲染已完整覆盖上下文，提前停止滚动
        if target_index >= 0 and len(all_links) > target_index + context_size:
            log_line(log_callback, f"  主页网格命中目标视频，已加载 {len(all_links)} 个视频链接。")
            break

        page.mouse.wheel(delta_x=0, delta_y=PROFILE_SCROLL_DELTA)
        try:
            page.evaluate("window.scrollBy(0, Math.floor(window.innerHeight * 1.5))")
        except Exception:
            pass
        time.sleep(profile_scroll_pause if target_index < 0 else 0.45)

        if len(all_links) == previous_count or all_links == previous_links:
            no_growth_count += 1
            if no_growth_count >= 3 and all_links and (target_index >= 0 or scroll_index >= MIN_PROFILE_SCROLLS_BEFORE_STABLE_STOP):
                break
        else:
            no_growth_count = 0

        if scroll_index and scroll_index % 10 == 0:
            status = "已命中目标，继续补齐后续视频" if target_index >= 0 else "继续寻找目标"
            log_line(log_callback, f"  主页已加载 {len(all_links)} 个视频链接，{status}...")

    return all_links, target_index

def extract_selected_play_counts(page, selected_links: list[str]) -> dict[str, str]:
    """
    DOM 模式：针对已经筛选出来的上下文视频链接，从主页网格 DOM 中提取其可见卡片上呈现的播放量。
    """
    selected_set = set(selected_links)
    play_counts: dict[str, str] = {}
    if not selected_set:
        return play_counts

    for element in page.locator("a[href*='/video/'], a[href*='video/']").all():
        try:
            href = clean_url(element.get_attribute("href") or "")
        except Exception:
            href = ""
        if href not in selected_set or href in play_counts:
            continue
        play_count = extract_card_play_count(element)
        if play_count:
            play_counts[href] = play_count
    return play_counts

def extract_metric(page, data_e2e_candidates, removable_words=(), default=""):
    """
    DOM 模式：从视频详细页面中读取特定指标数据。
    """
    candidates = data_e2e_candidates if isinstance(data_e2e_candidates, (list, tuple)) else [data_e2e_candidates]
    for data_e2e in candidates:
        try:
            loc = page.locator(f"[data-e2e='{data_e2e}']").first
            if loc.count() <= 0:
                continue
            text = clean_metric_text(loc.inner_text(timeout=2500), removable_words)
            if text:
                return text
        except Exception:
            continue
    return default

def extract_publish_time(page) -> str:
    """
    DOM 模式：从视频详情页面中利用正则或选择器提取视频的发布时间。
    """
    try:
        html = page.content()
        match = re.search(r'"createTime":"?(\d{10})"?', html)
        if match:
            return format_publish_time(match.group(1))
    except Exception:
        pass
    for selector in ("span[data-e2e='browser-nickname'] + span + span", "span[data-e2e='video-create-time']", "time"):
        try:
            loc = page.locator(selector).first
            if loc.count() > 0:
                text = loc.inner_text(timeout=1500).strip()
                if text:
                    return text
        except Exception:
            continue
    return ""

def extract_video_metrics(page, video_url: str) -> dict:
    """
    DOM 模式：进入某个视频详情 URL 并提取其标题、发布时间与各项互动数据指标。
    """
    page.goto(video_url, wait_until="domcontentloaded", timeout=30000)
    try:
        page.wait_for_selector("[data-e2e='like-count'], [data-e2e='comment-count']", timeout=5000)
    except Exception:
        pass

    video_id = extract_tiktok_video_id(video_url)
    item = find_item_in_state(page_state_sources(page), video_id)
    metrics = item_metrics(item) if item else {
        "视频标题": "",
        "发布时间": "",
        "播放量": "",
        "点赞数": "",
        "收藏数": "",
        "分享数": "",
        "评论数": "",
    }

    ui_metrics = {
        "视频标题": extract_tiktok_video_title(page),
        "发布时间": extract_publish_time(page),
        "播放量": "",
        "点赞数": extract_metric(page, "like-count"),
        "收藏数": extract_metric(page, ["favorite-count", "undefined-count"]),
        "分享数": extract_metric(page, "share-count"),
        "评论数": extract_metric(page, "comment-count"),
    }
    for key, value in ui_metrics.items():
        if not metrics.get(key) and value:
            metrics[key] = value
    return metrics

def fallback_rows_from_profile(profile_page, detail_page, profile_candidates: list[str], target_video_id: str, target_video_url: str, log_callback, stop_event=None, pause_event=None, context_size: int = CONTEXT_SIZE, max_profile_scrolls: int = MAX_PROFILE_SCROLLS, profile_scroll_pause: float = PROFILE_SCROLL_PAUSE) -> list[dict[str, str]]:
    """
    兜底的博主主页网格滚动与卡片跳转指标拉取控制流程。
    - 遍历候选的博主主页，在博主主页进行滚动定位，捕获视频链接网络。
    - 确定目标视频所在主页网络的位置。
    - 提取目标前后共 2*context_size 条视频链接。
    - 依次用详情页面对象 detail_page 导航进入这 2*context_size 个视频，提取详细属性。
    """
    links, target_index = [], -1
    matched_profile_url = profile_candidates[0] if profile_candidates else ""

    for candidate_profile_url in profile_candidates:
        if should_stop(stop_event):
            return []
        if wait_if_paused(pause_event, stop_event):
            return []
        if not candidate_profile_url:
            continue
        log_line(log_callback, f"  兜底：尝试主页定位：{candidate_profile_url}")
        links, target_index = collect_profile_video_links(profile_page, candidate_profile_url, target_video_id, log_callback, stop_event, pause_event, context_size, max_profile_scrolls, profile_scroll_pause)
        log_line(log_callback, f"  该主页已捕获 {len(links)} 个视频链接。")
        if target_index >= 0:
            matched_profile_url = candidate_profile_url
            break

    if target_index < 0:
        if links:
            log_warn(log_callback, "  主页未命中目标视频，不再用底部视频冒充上下文。")
        return []

    selected_indices = list(range(max(0, target_index - context_size), target_index))
    selected_indices += list(range(target_index + 1, min(len(links), target_index + context_size + 1)))
    selected_links = [links[current_index] for current_index in selected_indices]
    play_counts = extract_selected_play_counts(profile_page, selected_links)

    rows: list[dict[str, str]] = []
    for current_index in selected_indices:
        if should_stop(stop_event):
            break
        if wait_if_paused(pause_event, stop_event):
            break
        video_url = links[current_index]
        log_line(log_callback, f"  提取 {relation_for_index(target_index, current_index)}：{video_url}")
        metrics = extract_video_metrics(detail_page, video_url)
        metrics["播放量"] = play_counts.get(video_url, metrics.get("播放量", ""))
        rows.append({
            "博主链接": matched_profile_url,
            "目标视频链接": target_video_url,
            "视频链接": video_url,
            "时间轴关系": relation_for_index(target_index, current_index),
            **metrics,
        })
    return rows

def write_rows(writer: XlsxRowWriter, rows: list[dict[str, str]]):
    writer.writerows(sanitize_csv_rows(rows))

def run_scraper(txt_path: str, cdp_port_or_url: str, log_callback, finish_callback, stop_event=None, pause_event=None, config=None):
    """
    TikTok 上下文作品采集任务的主入口调度器。
    - 并发对目标视频进行定位与其前后发布时间轴相邻的前 N 条 and 后 N 条视频（上下文作品）的抓取。
    """
    if config is None:
        config = {}
    context_size = int(config.get("context_size", CONTEXT_SIZE))
    api_page_size = int(config.get("api_page_size", API_PAGE_SIZE))
    max_api_pages = int(config.get("max_api_pages", MAX_API_PAGES))
    max_profile_scrolls = int(config.get("max_profile_scrolls", MAX_PROFILE_SCROLLS))
    profile_scroll_pause = float(config.get("scroll_interval", PROFILE_SCROLL_PAUSE))
    max_parallel_tabs = int(config.get("max_parallel_tabs", 3))

    output_path = None
    completed_path = None
    try:
        pairs = parse_input_pairs(txt_path)
        if not pairs:
            log_warn(log_callback, "TXT 中没有有效的“视频链接 博主链接”行。")
            return

        output_path = build_output_path("tiktok", f"tiktok_context_{time.strftime('%Y%m%d_%H%M%S')}.xlsx", channel="context")
        writer = XlsxRowWriter(output_path, CSV_FIELDS)
        writer_lock = threading.Lock()
        log_lock = threading.Lock()

        log_line(log_callback, "正在连接并拉起 Chrome...")
        from src.core import ensure_chrome_for_cdp
        ensure_chrome_for_cdp(cdp_port_or_url, log_callback=log_callback)

        def make_thread_log(base_log_callback, lock, prefix):
            def wrapped(msg):
                if base_log_callback:
                    with lock:
                        base_log_callback(f"[{prefix}] {msg}")
            return wrapped

        def worker(pair, index):
            if should_stop(stop_event):
                return
            if wait_if_paused(pause_event, stop_event):
                return

            target_video_url, profile_url = pair
            thread_log = make_thread_log(log_callback, log_lock, f"任务 {index}")
            thread_log(f"[{index}/{len(pairs)}] 定位 TikTok 目标视频：{target_video_url}")

            target_page = None
            profile_page = None
            detail_page = None
            try:
                with sync_playwright() as p:
                    _, context = connect_existing_chromium(p, cdp_port_or_url)
                    target_page = context.new_page()

                    resolved_video_url, target_video_id, resolved_profile_url = resolve_target_video_context(target_page, target_video_url)
                    if not target_video_id:
                        thread_log("[WARN] 跳过：无法解析视频 ID。")
                        return

                    metadata = extract_target_metadata(target_page, target_video_id, resolved_profile_url or profile_url)
                    target_profile_url = extract_profile_url_from_video_url(target_video_url)
                    resolved_target_profile_url = extract_profile_url_from_video_url(resolved_video_url)
                    matched_profile_url = target_profile_url or metadata.get("profile_url") or resolved_profile_url or profile_url
                    profile_candidates = unique_urls([
                        target_profile_url,
                        resolved_target_profile_url,
                        matched_profile_url,
                        resolved_profile_url,
                        profile_url,
                        metadata.get("profile_url", ""),
                    ])

                    rows: list[dict[str, str]] = []
                    sec_uid = metadata.get("sec_uid", "")
                    if sec_uid:
                        try:
                            thread_log("使用 API 快速定位投稿列表。")
                            items, target_index = collect_author_items_via_api(target_page, sec_uid, target_video_id, thread_log, context_size, api_page_size, max_api_pages)
                            if target_index >= 0:
                                rows = rows_from_api_items(items, target_index, matched_profile_url, resolved_video_url, context_size)
                                thread_log(f"API 命中目标视频，准备写入 {len(rows)} 条。")
                            else:
                                thread_log("[WARN] API 未命中目标视频，切换到主页兜底。")
                        except Exception as exc:
                            thread_log(f"[ERROR] API 路线失败，切换到主页兜底：{exc}")
                    else:
                        thread_log("未从目标视频页解析到 secUid，切换到主页兜底。")

                    # 极速 API 路线不可用或未命中时，无缝切换至主页滚动兜底路线
                    if not rows:
                        profile_page = context.new_page()
                        detail_page = context.new_page()
                        rows = fallback_rows_from_profile(profile_page, detail_page, profile_candidates, target_video_id, resolved_video_url, thread_log, stop_event, pause_event, context_size, max_profile_scrolls, profile_scroll_pause)

                    if not rows:
                        thread_log("[WARN] 跳过：API 和主页兜底都没有定位到目标视频。")
                        return

                    with writer_lock:
                        write_rows(writer, rows)
                    thread_log(f"完成：写入 {len(rows)} 条。")
            except Exception as exc:
                thread_log(f"[ERROR] 处理失败：{exc}")
            finally:
                for opened_page in (target_page, profile_page, detail_page):
                    if opened_page is not None and not opened_page.is_closed():
                        try:
                            opened_page.close()
                        except Exception:
                            pass

        with ThreadPoolExecutor(max_workers=max_parallel_tabs) as executor:
            futures = [executor.submit(worker, pair, idx) for idx, pair in enumerate(pairs, 1)]
            for future in as_completed(futures):
                if should_stop(stop_event):
                    for f in futures:
                        f.cancel()
                    break
                try:
                    future.result()
                except Exception as exc:
                    log_error(log_callback, f"线程执行异常: {exc}")

        completed_path = output_path
        with writer_lock:
            writer.save()
        log_line(log_callback, f"完成，已保存：{output_path}")
    finally:
        finish_callback(completed_path)

