from __future__ import annotations

from datetime import datetime, timedelta, timezone
import re
import time
import urllib.parse

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError, Error as PlaywrightError

from src.core import (
    XlsxRowWriter,
    build_output_path,
    connect_existing_chromium,
    expand_compact_number,
    interruptible_sleep,
    log_error,
    log_line,
    log_warn,
    sanitize_csv_rows,
    should_stop,
    wait_if_paused,
)

CONTEXT_SIZE = 5
MAX_PROFILE_SCROLLS = 45
PROFILE_SCROLL_PAUSE = 3.8
PAGE_LOAD_TIMEOUT = 45000
MAX_SEARCH_SCROLLS = 35
TWITTER_EPOCH_MS = 1288834974657
STATUS_RE = re.compile(r"/([^/?#]+)/status/(\d+)")
NUMBER_RE = re.compile(r"(\d[\d,.]*(?:\.\d+)?\s*(?:[KkMmBb]|千|万|萬|亿|億)?)")

OUTPUT_FIELDS = [
    "博主主页链接",
    "目标推文链接",
    "推文链接",
    "时间轴关系",
    "发布时间",
    "推文内容",
    "点赞数",
    "转发量",
    "评论量",
    "浏览量",
]

def normalize_x_url(url: str) -> str:
    if not url:
        return ""
    normalized = url.strip().replace("twitter.com", "x.com")
    normalized = normalized.split("?")[0].split("#")[0]
    if normalized.startswith("//"):
        normalized = "https:" + normalized
    if normalized.startswith("/"):
        normalized = "https://x.com" + normalized
    if normalized and not normalized.startswith("http"):
        normalized = "https://" + normalized
    return normalized.rstrip("/")

def parse_input_pairs(txt_path: str) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    with open(txt_path, "r", encoding="utf-8-sig") as f:
        for line in f:
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            parts = [part.strip() for part in stripped.split("\t") if part.strip()] if "\t" in stripped else stripped.split()
            if len(parts) < 2:
                continue
            tweet_url = normalize_x_url(parts[0])
            profile_url = normalize_x_url(parts[1])
            if "/status/" in tweet_url and profile_url:
                pairs.append((tweet_url, profile_url))
    return pairs

def extract_status_id(url: str) -> str:
    match = STATUS_RE.search(url or "")
    return match.group(2) if match else ""

def datetime_from_status_id(status_id: str) -> datetime | None:
    try:
        timestamp_ms = (int(status_id) >> 22) + TWITTER_EPOCH_MS
        return datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc)
    except (ValueError, TypeError, OverflowError):
        return None

def extract_profile_url_from_tweet_url(url: str) -> str:
    match = STATUS_RE.search(normalize_x_url(url))
    if not match:
        return ""
    return f"https://x.com/{match.group(1).strip('@')}"

def extract_profile_handle(profile_url: str) -> str:
    normalized = normalize_x_url(profile_url)
    match = re.search(r"x\.com/([^/?#]+)", normalized)
    if not match:
        return ""
    handle = match.group(1).strip("@")
    if handle.lower() in {"home", "explore", "search", "i", "notifications", "messages", "settings", "signup", "login"}:
        return ""
    return handle

def append_with_replies(profile_url: str) -> str:
    normalized = normalize_x_url(profile_url)
    if not normalized:
        return ""
    if normalized.endswith("/media"):
        normalized = normalized.rsplit("/media", 1)[0]
    if normalized.endswith("/with_replies"):
        return normalized
    return f"{normalized}/with_replies"

def append_media(profile_url: str) -> str:
    normalized = normalize_x_url(profile_url)
    if not normalized:
        return ""
    if normalized.endswith("/with_replies"):
        normalized = normalized.rsplit("/with_replies", 1)[0]
    if normalized.endswith("/media"):
        return normalized
    return f"{normalized}/media"

def expand_profile_candidates(profile_urls: list[str]) -> list[str]:
    expanded: list[str] = []
    for profile_url in profile_urls:
        normalized = normalize_x_url(profile_url)
        if not normalized:
            continue
        expanded.extend([normalized, append_media(normalized), append_with_replies(normalized)])
    return unique_urls(expanded)

def unique_urls(urls: list[str]) -> list[str]:
    unique: list[str] = []
    seen = set()
    for url in urls:
        normalized = normalize_x_url(url)
        if normalized and normalized not in seen:
            unique.append(normalized)
            seen.add(normalized)
    return unique

def relation_for_index(target_index: int, current_index: int) -> str:
    if current_index < target_index:
        return f"目标之后发布第{target_index - current_index}条"
    return f"目标之前发布第{current_index - target_index}条"

def safe_text(locator, default: str = "") -> str:
    try:
        if locator.count() <= 0:
            return default
        return locator.first.inner_text(timeout=1800).strip() or default
    except (PlaywrightTimeoutError, PlaywrightError, ValueError):
        return default

def safe_attr(locator, attr: str, default: str = "") -> str:
    try:
        if locator.count() <= 0:
            return default
        return locator.first.get_attribute(attr, timeout=1800) or default
    except (PlaywrightTimeoutError, PlaywrightError, ValueError, KeyError):
        return default

def normalize_metric_text(text: str) -> str:
    if not text:
        return ""
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return ""
    match = NUMBER_RE.search(text)
    return expand_compact_number(match.group(1).strip()) if match else ""

def extract_metric_from_selectors(article, selectors: list[str], normalizer=normalize_metric_text) -> str:
    for selector in selectors:
        try:
            nodes = article.locator(selector).all()
        except Exception:
            continue
        for node in nodes:
            try:
                raw_text = node.inner_text(timeout=1200).strip()
            except Exception:
                raw_text = ""
            value = normalizer(raw_text)
            if value:
                return value

            try:
                aria = node.get_attribute("aria-label", timeout=1200) or ""
            except Exception:
                aria = ""
            value = normalizer(aria)
            if value:
                return value
    return ""

def get_tweet_time(article) -> str:
    raw_time = safe_attr(article.locator("time"), "datetime", default="")
    if not raw_time:
        return ""
    try:
        dt_obj = datetime.fromisoformat(raw_time.replace("Z", "+00:00"))
        return dt_obj.strftime("%Y-%m-%d %H:%M:%S")
    except (ValueError, AttributeError):
        return raw_time[:19] if raw_time else ""

def get_tweet_datetime(article) -> datetime | None:
    raw_time = safe_attr(article.locator("time"), "datetime", default="")
    if not raw_time:
        return None
    try:
        return datetime.fromisoformat(raw_time.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None

def get_tweet_text(article) -> str:
    try:
        revert_locator = article.locator("text='view original', text='查看原文', text='原文を表示', text='show original', text='原文を見る'").first
        revert_locator.click(timeout=500)
    except (PlaywrightTimeoutError, PlaywrightError):
        pass

    try:
        expand_locator = article.locator("text='show more', text='show more...', text='もっと見る', text='더 보기', text='显示更多'").first
        expand_locator.click(timeout=500)
    except (PlaywrightTimeoutError, PlaywrightError):
        pass

    time.sleep(0.3)
    return safe_text(article.locator('[data-testid="tweetText"]'))

def collect_status_urls(article, profile_handle: str = "") -> list[str]:
    urls: list[str] = []
    seen = set()
    try:
        anchors = article.locator('a[href*="/status/"]').all()
    except Exception:
        return urls

    profile_handle = (profile_handle or "").lower()
    preferred: list[str] = []
    fallback: list[str] = []

    for anchor in anchors:
        try:
            href = anchor.get_attribute("href") or ""
        except Exception:
            continue
        match = STATUS_RE.search(href)
        if not match:
            continue
        handle = match.group(1).strip("@")
        normalized = normalize_x_url(f"https://x.com/{handle}/status/{match.group(2)}")
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        if profile_handle and handle.lower() == profile_handle:
            preferred.append(normalized)
        else:
            fallback.append(normalized)

    urls.extend(preferred)
    urls.extend(fallback)
    return urls

def get_article_tweet_url(article, profile_handle: str = "") -> str:
    urls = collect_status_urls(article, profile_handle)
    return urls[0] if urls else ""

def article_status_urls(article) -> list[str]:
    urls: list[str] = []
    try:
        anchors = article.locator('a[href*="/status/"]').all()
    except Exception:
        return urls

    for anchor in anchors:
        try:
            href = anchor.get_attribute("href") or ""
        except Exception:
            continue
        match = STATUS_RE.search(href)
        if not match:
            continue
        urls.append(normalize_x_url(f"https://x.com/{match.group(1)}/status/{match.group(2)}"))
    return unique_urls(urls)

def article_contains_status_id(article, status_id: str) -> bool:
    if not status_id:
        return False
    return any(status_id in url for url in article_status_urls(article))

def target_url_from_article(article, target_status_id: str, fallback_url: str) -> str:
    for url in article_status_urls(article):
        if target_status_id and target_status_id in url:
            return url
    return normalize_x_url(fallback_url)

def find_target_article(page, target_status_id: str):
    try:
        page.wait_for_selector('article[data-testid="tweet"]', timeout=20000)
    except Exception:
        return None

    for article in page.locator('article[data-testid="tweet"]').all():
        try:
            if article_contains_status_id(article, target_status_id):
                return article
        except Exception:
            continue
    return None

def resolve_profile_url_from_tweet_page(page, target_tweet_url: str, target_status_id: str, page_timeout=None, stop_event=None) -> str:
    if page_timeout is None:
        page_timeout = PAGE_LOAD_TIMEOUT
    try:
        page.goto(target_tweet_url, wait_until="domcontentloaded", timeout=page_timeout)
        interruptible_sleep(2.5, stop_event)
    except Exception:
        return ""

    article = find_target_article(page, target_status_id)
    if article is None:
        return ""

    try:
        user_block = article.locator('div[data-testid="User-Name"]').first
        for link in user_block.locator('a[role="link"]').all():
            href = link.get_attribute("href") or ""
            normalized = normalize_x_url(href)
            if not normalized or "/status/" in normalized:
                continue
            handle = extract_profile_handle(normalized)
            if handle:
                return f"https://x.com/{handle}"
    except Exception:
        pass

    for tweet_url in collect_status_urls(article):
        handle = extract_profile_handle(tweet_url)
        if handle:
            return f"https://x.com/{handle}"
    return ""

def extract_metrics_from_article(article) -> dict:
    return {
        "发布时间": get_tweet_time(article),
        "推文内容": get_tweet_text(article),
        "点赞数": extract_metric_from_selectors(article, ['[data-testid="like"]', '[data-testid="unlike"]']),
        "转发量": extract_metric_from_selectors(article, ['[data-testid="retweet"]', '[data-testid="unretweet"]']),
        "评论量": extract_metric_from_selectors(article, ['[data-testid="reply"]']),
        "浏览量": extract_metric_from_selectors(
            article,
            [
                'a[href*="/analytics"]',
                'div[data-testid="postViewCount"]',
                '[aria-label*="Views"]',
                '[aria-label*="views"]',
                '[aria-label*="浏览"]',
            ],
        ),
    }

def collect_profile_timeline(page, profile_url: str, target_status_id: str, log_callback, page_timeout=None, max_scrolls=None, scroll_pause=None, context_size=CONTEXT_SIZE, pause_event=None, stop_event=None) -> tuple[list[str], int]:
    if page_timeout is None:
        page_timeout = PAGE_LOAD_TIMEOUT
    if max_scrolls is None:
        max_scrolls = MAX_PROFILE_SCROLLS
    if scroll_pause is None:
        scroll_pause = PROFILE_SCROLL_PAUSE

    profile_url = normalize_x_url(profile_url)
    profile_handle = extract_profile_handle(profile_url)
    timeline_urls: list[str] = []
    target_index = -1
    no_growth_count = 0

    page.goto(profile_url, wait_until="domcontentloaded", timeout=page_timeout)
    interruptible_sleep(1.2, stop_event)
    try:
        page.wait_for_selector('article[data-testid="tweet"]', timeout=15000)
    except Exception:
        pass

    for scroll_idx in range(max_scrolls):
        if should_stop(stop_event):
            break
        if wait_if_paused(pause_event, stop_event):
            break
        previous_count = len(timeline_urls)
        try:
            articles = page.locator('article[data-testid="tweet"]').all()
        except Exception:
            articles = []

        for article in articles:
            is_target_article = article_contains_status_id(article, target_status_id)
            tweet_url = (
                target_url_from_article(article, target_status_id, f"https://x.com/{profile_handle}/status/{target_status_id}")
                if is_target_article
                else get_article_tweet_url(article, profile_handle)
            )
            if not tweet_url:
                continue
            if tweet_url not in timeline_urls:
                timeline_urls.append(tweet_url)
            if is_target_article:
                target_index = timeline_urls.index(tweet_url)

        if target_index < 0:
            for idx, tweet_url in enumerate(timeline_urls):
                if target_status_id and target_status_id in tweet_url:
                    target_index = idx
                    break

        if target_index >= 0 and len(timeline_urls) >= target_index + context_size + 1:
            break

        if len(timeline_urls) == previous_count:
            no_growth_count += 1
            if no_growth_count >= 8 and target_index >= 0:
                break
            if no_growth_count >= 12:
                break
        else:
            no_growth_count = 0

        if scroll_idx and scroll_idx % 10 == 0:
            log_line(log_callback, f"  主页已收集 {len(timeline_urls)} 条推文链接...")

        try:
            retry_btn = page.locator(
                "button:has-text('Retry'), button:has-text('重试'), button:has-text('再试一次')"
            ).first
            if retry_btn.count() > 0:
                retry_btn.click(force=True)
                interruptible_sleep(2.5, stop_event)
        except Exception:
            pass

        page.mouse.wheel(0, 3200)
        try:
            page.evaluate("window.scrollBy(0, Math.floor(window.innerHeight * 2.6))")
        except Exception:
            pass
        interruptible_sleep(scroll_pause, stop_event)

    return timeline_urls, target_index

def build_author_search_url(handle: str, center_dt: datetime, window_days: int) -> str:
    since = (center_dt - timedelta(days=window_days)).date().isoformat()
    until = (center_dt + timedelta(days=window_days + 1)).date().isoformat()
    query = f"from:{handle} since:{since} until:{until}"
    return f"https://x.com/search?q={urllib.parse.quote(query)}&src=typed_query&f=live"

def collect_author_search_timeline(
    page,
    handle: str,
    target_status_id: str,
    log_callback,
    page_timeout=None,
    context_size=None,
    pause_event=None,
    stop_event=None,
) -> tuple[list[str], int]:
    if page_timeout is None:
        page_timeout = PAGE_LOAD_TIMEOUT
    if context_size is None:
        context_size = CONTEXT_SIZE

    center_dt = datetime_from_status_id(target_status_id)
    if center_dt is None or not handle:
        return [], -1

    for window_days in (1, 3, 7, 14, 30):
        search_url = build_author_search_url(handle, center_dt, window_days)
        log_line(log_callback, f"  尝试作者搜索窗口：前后 {window_days} 天")
        try:
            page.goto(search_url, wait_until="domcontentloaded", timeout=page_timeout)
        except Exception as exc:
            log_error(log_callback, f"  搜索页打开失败，继续下一个窗口：{exc}")
            continue

        interruptible_sleep(1.5, stop_event)
        rows_by_url: dict[str, tuple[datetime | None, int]] = {}
        seen_order = 0
        target_url = ""
        no_growth_count = 0

        for _ in range(MAX_SEARCH_SCROLLS):
            if should_stop(stop_event):
                break
            if wait_if_paused(pause_event, stop_event):
                break
            previous_count = len(rows_by_url)
            try:
                articles = page.locator('article[data-testid="tweet"]').all()
            except Exception:
                articles = []

            for article in articles:
                is_target_article = article_contains_status_id(article, target_status_id)
                tweet_url = (
                    target_url_from_article(article, target_status_id, f"https://x.com/{handle}/status/{target_status_id}")
                    if is_target_article
                    else get_article_tweet_url(article, handle)
                )
                if not tweet_url:
                    continue

                url_handle = extract_profile_handle(tweet_url).lower()
                if url_handle and url_handle != handle.lower():
                    continue

                if tweet_url not in rows_by_url:
                    rows_by_url[tweet_url] = (get_tweet_datetime(article), seen_order)
                    seen_order += 1
                if is_target_article:
                    target_url = tweet_url

            if target_url and len(rows_by_url) >= context_size * 2 + 1:
                break

            if len(rows_by_url) == previous_count:
                no_growth_count += 1
                if no_growth_count >= 5:
                    break
            else:
                no_growth_count = 0

            page.mouse.wheel(0, 3000)
            try:
                page.evaluate("window.scrollBy(0, Math.floor(window.innerHeight * 2.4))")
            except Exception:
                pass
            interruptible_sleep(0.7, stop_event)

        if not rows_by_url:
            continue

        target_url = target_url or next((url for url in rows_by_url if target_status_id in url), "")
        if not target_url:
            log_warn(log_callback, f"  搜索窗口前后 {window_days} 天未命中目标推文。")
            continue

        target_dt = rows_by_url.get(target_url, (None, 0))[0] or center_dt

        def sort_key(item):
            url, (dt_value, order) = item
            return (dt_value or datetime.min.replace(tzinfo=timezone.utc), -order)

        sorted_items = sorted(rows_by_url.items(), key=sort_key, reverse=True)
        timeline_urls = [url for url, _ in sorted_items]

        if target_url not in timeline_urls:
            timeline_urls.append(target_url)

        target_index = timeline_urls.index(target_url)
        log_line(log_callback, 
            f"  作者搜索命中目标，目标时间约 {target_dt.strftime('%Y-%m-%d %H:%M:%S')} UTC，收集 {len(timeline_urls)} 条。"
        )
        return timeline_urls, target_index

    return [], -1

def extract_detail_metrics(page, tweet_url: str, fallback: dict, log_callback, page_timeout=None, stop_event=None) -> dict:
    if page_timeout is None:
        page_timeout = PAGE_LOAD_TIMEOUT
    if should_stop(stop_event):
        return dict(fallback or {})
    target_status_id = extract_status_id(tweet_url)
    metrics = dict(fallback or {})
    try:
        page.goto(tweet_url, wait_until="domcontentloaded", timeout=page_timeout)
        if interruptible_sleep(2.2, stop_event):
            return metrics
        article = find_target_article(page, target_status_id)
        if article is None:
            return metrics
        detail_metrics = extract_metrics_from_article(article)
        for key, value in detail_metrics.items():
            if value:
                metrics[key] = value
    except Exception as exc:
        log_warn(log_callback, f"    详情页补充失败，保留主页已提取指标：{exc}")
    return metrics

def selected_context_indices(timeline_urls: list[str], target_index: int, context_size=None) -> list[int]:
    if context_size is None:
        context_size = CONTEXT_SIZE
    indices = list(range(max(0, target_index - context_size), target_index))
    indices += list(range(target_index + 1, min(len(timeline_urls), target_index + context_size + 1)))
    return indices

import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

def _scrape_single_context_task(
    index: int,
    target_tweet_url: str,
    profile_url: str,
    cdp_port_or_url: str,
    context_size_val: int,
    max_profile_scrolls_val: int,
    profile_scroll_pause_val: float,
    page_load_timeout_val: int,
    log_callback,
    stop_event,
    pause_event,
    writer,
    writer_lock,
    total_pairs: int,
):
    from src.platforms.x_twitter.context import (
        extract_status_id,
        extract_profile_url_from_tweet_url,
        normalize_x_url,
        extract_profile_handle,
        collect_author_search_timeline,
        expand_profile_candidates,
        collect_profile_timeline,
        resolve_profile_url_from_tweet_page,
        selected_context_indices,
        relation_for_index,
        extract_detail_metrics,
        OUTPUT_FIELDS,
    )

    if should_stop(stop_event):
        return
    if wait_if_paused(pause_event, stop_event):
        return

    target_status_id = extract_status_id(target_tweet_url)
    log_line(log_callback, f"[{index}/{total_pairs}] 定位目标推文：{target_tweet_url}")
    if not target_status_id:
        log_warn(log_callback, f"[{index}/{total_pairs}] 跳过：无法解析推文 ID。")
        return

    playwright = None
    browser = None
    page = None
    try:
        playwright = sync_playwright().start()
        try:
            browser, context = connect_existing_chromium(playwright, cdp_port_or_url)
        except Exception as e:
            log_error(log_callback, f"[{index}/{total_pairs}] 连接失败：{e}")
            return

        page = context.new_page()
        tweet_prefix_profile_url = extract_profile_url_from_tweet_url(target_tweet_url)
        if tweet_prefix_profile_url and normalize_x_url(tweet_prefix_profile_url) != normalize_x_url(profile_url):
            log_line(log_callback, f"[{index}/{total_pairs}] 从目标推文链接前缀识别作者主页：{tweet_prefix_profile_url}")

        timeline_urls: list[str] = []
        target_index = -1
        matched_profile_url = normalize_x_url(tweet_prefix_profile_url or profile_url)
        search_handle = extract_profile_handle(tweet_prefix_profile_url or profile_url)

        if search_handle:
            timeline_urls, target_index = collect_author_search_timeline(
                page,
                search_handle,
                target_status_id,
                log_callback,
                page_timeout=page_load_timeout_val,
                context_size=context_size_val,
                pause_event=pause_event,
                stop_event=stop_event,
            )
            if target_index >= 0:
                matched_profile_url = f"https://x.com/{search_handle}"

        profile_candidates = expand_profile_candidates(
            [
                tweet_prefix_profile_url,
                profile_url,
            ]
        )

        if target_index < 0:
            for candidate_profile_url in profile_candidates:
                log_line(log_callback, f"[{index}/{total_pairs}] 尝试主页时间线：{candidate_profile_url}")
                timeline_urls, target_index = collect_profile_timeline(
                    page,
                    candidate_profile_url,
                    target_status_id,
                    log_callback,
                    page_timeout=page_load_timeout_val,
                    max_scrolls=max_profile_scrolls_val,
                    scroll_pause=profile_scroll_pause_val,
                    context_size=context_size_val,
                    pause_event=pause_event,
                    stop_event=stop_event,
                )
                log_line(log_callback, f"[{index}/{total_pairs}] 当前时间线共收集 {len(timeline_urls)} 条推文链接。")
                if target_index >= 0:
                    matched_profile_url = candidate_profile_url
                    break

        if target_index < 0:
            log_warn(log_callback, f"[{index}/{total_pairs}] 快速候选页未命中，打开目标推文详情页反查作者后再补扫一次。")
            resolved_profile_url = resolve_profile_url_from_tweet_page(page, target_tweet_url, target_status_id, page_timeout=page_load_timeout_val, stop_event=stop_event)
            if resolved_profile_url:
                if normalize_x_url(resolved_profile_url) != normalize_x_url(profile_url):
                    log_line(log_callback, f"[{index}/{total_pairs}] 从目标推文详情页反查到作者主页：{resolved_profile_url}")
                for candidate_profile_url in expand_profile_candidates([resolved_profile_url]):
                    if candidate_profile_url in profile_candidates:
                        continue
                    log_line(log_callback, f"[{index}/{total_pairs}] 补扫主页时间线：{candidate_profile_url}")
                    timeline_urls, target_index = collect_profile_timeline(
                        page,
                        candidate_profile_url,
                        target_status_id,
                        log_callback,
                        page_timeout=page_load_timeout_val,
                        max_scrolls=max_profile_scrolls_val,
                        scroll_pause=profile_scroll_pause_val,
                        context_size=context_size_val,
                        pause_event=pause_event,
                        stop_event=stop_event,
                    )
                    log_line(log_callback, f"[{index}/{total_pairs}] 当前时间线共收集 {len(timeline_urls)} 条推文链接。")
                    if target_index >= 0:
                        matched_profile_url = candidate_profile_url
                        break

        if target_index < 0:
            log_warn(log_callback, f"[{index}/{total_pairs}] 跳过：在博主主页和回复页中都没有找到目标推文。目标可能太旧、不可见、不是该作者公开时间线内容，或页面没有继续加载。")
            return

        indices = selected_context_indices(timeline_urls, target_index, context_size=context_size_val)
        rows = []
        for current_index in indices:
            tweet_url = timeline_urls[current_index]
            relation = relation_for_index(target_index, current_index)
            log_line(log_callback, f"[{index}/{total_pairs}] 提取 {relation}：{tweet_url}")
            metrics = extract_detail_metrics(page, tweet_url, {}, log_callback, page_timeout=page_load_timeout_val, stop_event=stop_event)
            rows.append(
                {
                    "博主主页链接": matched_profile_url,
                    "目标推文链接": normalize_x_url(target_tweet_url),
                    "推文链接": tweet_url,
                    "时间轴关系": relation,
                    **{field: metrics.get(field, "") for field in OUTPUT_FIELDS[4:]},
                }
            )

        with writer_lock:
            writer.writerows(sanitize_csv_rows(rows))
        log_line(log_callback, f"[{index}/{total_pairs}] 完成：写入 {len(rows)} 条。")
    except Exception as e:
        log_error(log_callback, f"[{index}/{total_pairs}] 处理失败：{e}")
    finally:
        # 连接此时仍然存活（playwright.stop() 延后到最后），
        # page.close() 才能真正发送 Target.closeTarget 关闭真实标签页。
        if page is not None:
            try:
                if not page.is_closed():
                    page.close()
            except Exception:
                pass
        if browser is not None:
            try:
                browser.close()
            except Exception:
                pass
        if playwright is not None:
            try:
                playwright.stop()
            except Exception:
                pass

def run_scraper(txt_path: str, cdp_port_or_url: str, log_callback, finish_callback, stop_event=None, config=None, pause_event=None):
    if config is None:
        config = {}
    context_size_val = int(config.get("context_size", CONTEXT_SIZE))
    max_profile_scrolls_val = int(config.get("max_profile_scrolls", MAX_PROFILE_SCROLLS))
    profile_scroll_pause_val = float(config.get("scroll_interval", PROFILE_SCROLL_PAUSE))
    page_load_timeout_val = int(config.get("page_load_timeout", PAGE_LOAD_TIMEOUT))
    max_parallel_tabs = int(config.get("max_parallel_tabs", 3))

    output_path = None
    completed_path = None
    try:
        from src.core import ensure_chrome_for_cdp
        try:
            ensure_chrome_for_cdp(cdp_port_or_url, log_callback=log_callback)
        except Exception:
            pass

        pairs = parse_input_pairs(txt_path)
        if not pairs:
            log_warn(log_callback, "TXT 中没有有效的\"推文链接 + 博主主页链接\"行。")
            return

        output_path = build_output_path("x", f"x_context_{time.strftime('%Y%m%d_%H%M%S')}.xlsx")
        writer = XlsxRowWriter(output_path, OUTPUT_FIELDS)

        writer_lock = threading.Lock()
        log_lock = threading.Lock()

        def make_thread_safe_log_callback(original_cb, lock):
            if original_cb is None:
                return None
            def safe_cb(msg: str):
                with lock:
                    original_cb(msg)
            return safe_cb

        safe_log_callback = make_thread_safe_log_callback(log_callback, log_lock)

        with ThreadPoolExecutor(max_workers=max_parallel_tabs) as executor:
            futures = []
            for index, (target_tweet_url, profile_url) in enumerate(pairs, 1):
                if should_stop(stop_event):
                    break
                if wait_if_paused(pause_event, stop_event):
                    break
                futures.append(
                    executor.submit(
                        _scrape_single_context_task,
                        index,
                        target_tweet_url,
                        profile_url,
                        cdp_port_or_url,
                        context_size_val,
                        max_profile_scrolls_val,
                        profile_scroll_pause_val,
                        page_load_timeout_val,
                        safe_log_callback,
                        stop_event,
                        pause_event,
                        writer,
                        writer_lock,
                        len(pairs),
                    )
                )

            for future in as_completed(futures):
                try:
                    future.result()
                except Exception as exc:
                    log_error(safe_log_callback, f"线程执行异常: {exc}")

        completed_path = output_path
        with writer_lock:
            writer.save()
        log_line(safe_log_callback, f"完成，已保存：{output_path}")
    finally:
        if finish_callback:
            finish_callback(completed_path)

