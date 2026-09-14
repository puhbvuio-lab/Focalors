from __future__ import annotations

import random
import re
import threading
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime

try:
    from playwright.sync_api import sync_playwright
except ModuleNotFoundError:  # pragma: no cover
    sync_playwright = None

from src.core import (
    build_output_path,
    connect_existing_chromium,
    ensure_chrome_for_cdp,
    interruptible_sleep,
    log_error,
    log_line,
    log_warn,
    should_stop,
    wait_if_paused,
)
from src.core.task_checkpoint import open_checkpointed_multi_sheet_writer, open_task_checkpoint
from src.core.parallel import normalize_parallel_windows
from src.platforms.tiktok.keyword import (
    MAX_SEARCH_SCROLLS,
    SEARCH_SCROLL_PAUSE,
    _click_tiktok_retry,
    _detect_tiktok_search_error,
    _retry_backoff_seconds,
    collect_visible_video_items,
    derive_publish_time_from_video_url,
    dynamic_search_scroll_limit,
    extract_video_row,
    in_date_range,
    parse_date_range,
    trigger_search_lazy_load,
)
from src.platforms.tiktok.keyword_author_works import (
    QUICK_PROFILE_WORK_LIMIT,
    AUTHOR_FIELDS,
    VIDEO_FIELDS,
    TikTokAuthorSeed,
    _author_key,
    build_author_sheet_row,
    build_author_row,
    build_video_row,
    collect_author_works_with_parallel_windows,
    load_seed_author_cache,
    merge_seed_author,
    quick_mode_enabled,
    resolve_profile_work_limit,
    save_seed_author_cache,
)
from src.platforms.tiktok.profile_videos import (
    DEFAULT_MAX_SCROLLS,
    DETAIL_DELAY_MAX_SECONDS,
    DETAIL_DELAY_MIN_SECONDS,
    DETAIL_LOAD_TIMEOUT,
    NO_NEW_SCROLL_LIMIT,
    PAGE_LOAD_TIMEOUT,
    SCROLL_INTERVAL_SECONDS,
    SCROLL_PX,
    collect_profile_video_details,
)
from src.platforms.tiktok.profiles import extract_profile_row


CSV_FIELDS = [
    "话题",
    "命中作品数",
    "命中作品链接列表",
    "作者主页链接",
    "作者名称",
    "作者ID",
    "粉丝数",
    "作者简介",
    "采集作品数",
    "时间窗口内作品数",
    "作品标题列表",
    "作品链接列表",
    "作品发布时间列表",
]
HASHTAG_AUTHOR_FIELDS = ["话题", *[field for field in AUTHOR_FIELDS if field != "搜索词"]]
HASHTAG_VIDEO_FIELDS = ["话题", *[field for field in VIDEO_FIELDS if field != "搜索词"]]


@dataclass(frozen=True)
class HashtagSource:
    label: str
    url: str


def ensure_playwright_available() -> None:
    if sync_playwright is None:
        raise ModuleNotFoundError("playwright is required for TikTok hashtag author works scraping")


def normalize_hashtag_input(value: str) -> HashtagSource:
    raw = str(value or "").strip()
    if not raw:
        raise ValueError("话题不能为空。")

    if re.match(r"^https?://", raw, re.I):
        parsed = urllib.parse.urlparse(raw)
        match = re.search(r"/tag/([^/?#]+)", parsed.path or "", re.I)
        if not match:
            raise ValueError(f"不是有效的 TikTok 话题页链接：{raw}")
        tag = urllib.parse.unquote(match.group(1)).strip()
    else:
        tag = raw.lstrip("#").strip()
        if tag.startswith("tag/"):
            tag = tag[4:].strip()
    if not tag:
        raise ValueError(f"无法识别话题：{value}")

    encoded = urllib.parse.quote(tag, safe="")
    return HashtagSource(label=f"#{tag}", url=f"https://www.tiktok.com/tag/{encoded}")


def parse_hashtag_sources(
    values: list[str] | tuple[str, ...] | str,
    *,
    skip_invalid: bool = False,
    log_callback=None,
) -> list[HashtagSource]:
    lines = values.splitlines() if isinstance(values, str) else list(values or [])
    sources: list[HashtagSource] = []
    seen: set[str] = set()
    for line in lines:
        if not str(line).strip():
            continue
        try:
            source = normalize_hashtag_input(str(line))
        except ValueError as exc:
            if skip_invalid:
                log_warn(log_callback, f"跳过无效话题输入：{line}（{exc}）")
                continue
            raise
        key = source.url.lower()
        if key in seen:
            continue
        seen.add(key)
        sources.append(source)
    return sources


def _author_row_for_hashtag(
    seed: TikTokAuthorSeed,
    profile_record: dict[str, str],
    works: list[dict[str, str]],
    limit_time_bool: bool = False,
    start_dt: datetime | None = None,
    end_dt: datetime | None = None,
) -> dict[str, str]:
    row = build_author_row(seed, profile_record, works, limit_time_bool, start_dt, end_dt)
    row["话题"] = row.pop("搜索词", "")
    return {field: row.get(field, "") for field in CSV_FIELDS}


def _author_sheet_row_for_hashtag(
    seed: TikTokAuthorSeed,
    profile_record: dict[str, str],
    works: list[dict[str, str]],
    limit_time_bool: bool = False,
    start_dt: datetime | None = None,
    end_dt: datetime | None = None,
) -> dict[str, str]:
    return build_author_sheet_row(
        seed,
        profile_record,
        works,
        limit_time_bool,
        start_dt,
        end_dt,
        source_field="话题",
    )


def _video_row_for_hashtag(
    index: int,
    seed: TikTokAuthorSeed,
    profile_record: dict[str, str],
    work: dict[str, str],
) -> dict[str, str]:
    return build_video_row(index, seed, profile_record, work, source_field="话题")


def _topic_author_key(source: HashtagSource, seed: TikTokAuthorSeed) -> str:
    return f"{source.url.lower()}|{_author_key(seed.profile_url, seed.author_id)}"


def _seed_checkpoint_key(seed: TikTokAuthorSeed) -> str:
    topic = seed.keywords[0] if seed.keywords else ""
    return f"{topic}|{seed.profile_url}"


def open_hashtag_page(page, source: HashtagSource, stop_event=None, log_callback=None, page_timeout: int = PAGE_LOAD_TIMEOUT, max_attempts: int = 5) -> bool:
    for attempt in range(1, max_attempts + 1):
        if should_stop(stop_event):
            return False
        try:
            if attempt >= 3:
                page.reload(wait_until="domcontentloaded", timeout=page_timeout)
            else:
                page.goto(source.url, wait_until="domcontentloaded", timeout=page_timeout)
        except Exception as exc:
            log_line(log_callback, f"  话题页导航失败（第 {attempt}/{max_attempts} 次）：{exc}")
            interruptible_sleep(_retry_backoff_seconds(attempt), stop_event)
            continue

        interruptible_sleep(random.uniform(1.8, 2.8), stop_event)
        try:
            page.wait_for_selector("a[href*='/video/'], a[href*='video/']", timeout=8000)
        except Exception:
            pass

        error_text = _detect_tiktok_search_error(page)
        if error_text is None:
            return True

        log_line(log_callback, f"  话题页出现错误态「{error_text}」（第 {attempt}/{max_attempts} 次），尝试重试...")
        cooldown = random.uniform(5.0, 9.0) if attempt <= 2 else _retry_backoff_seconds(attempt)
        interruptible_sleep(cooldown, stop_event)
        if _click_tiktok_retry(page):
            interruptible_sleep(random.uniform(4.0, 6.0), stop_event)
            if _detect_tiktok_search_error(page) is None:
                log_line(log_callback, "  点击重试后话题页已恢复。")
                return True
        interruptible_sleep(_retry_backoff_seconds(attempt), stop_event)

    log_line(log_callback, f"  话题页重试 {max_attempts} 次仍处于错误态，继续尝试采集：{source.url}")
    return False


def collect_hashtag_seed_authors(
    topic_page,
    detail_page,
    sources: list[HashtagSource],
    start_dt: datetime | None,
    end_dt: datetime | None,
    limit_time_bool: bool,
    log_callback,
    stop_event=None,
    pause_event=None,
    max_seed_works: int = 300,
    max_authors: int = 100,
    max_topic_scrolls: int = MAX_SEARCH_SCROLLS,
    topic_scroll_pause: float = SEARCH_SCROLL_PAUSE,
    no_new_scroll_limit: int = 12,
    page_timeout: int = PAGE_LOAD_TIMEOUT,
    initial_authors: dict[str, TikTokAuthorSeed] | None = None,
    completed_sources: set[str] | None = None,
    seed_cache_callback=None,
) -> dict[str, TikTokAuthorSeed]:
    authors: dict[str, TikTokAuthorSeed] = dict(initial_authors or {})
    completed_sources = set(completed_sources or set())
    inspected_count = 0

    for source_index, source in enumerate(sources, 1):
        source_id = source.url
        if source_id in completed_sources:
            log_line(log_callback, f"[{source_index}/{len(sources)}] 断点续跑跳过已完成种子发现话题：{source.label}")
            continue
        if should_stop(stop_event):
            break
        if wait_if_paused(pause_event, stop_event):
            break

        log_line(log_callback, f"[{source_index}/{len(sources)}] 打开话题页：{source.label} {source.url}")
        if not open_hashtag_page(topic_page, source, stop_event=stop_event, log_callback=log_callback, page_timeout=page_timeout):
            log_warn(log_callback, f"跳过话题：页面无法正常打开或持续错误：{source.label}")
            continue
        source_seed_limit = max(1, int(max_seed_works or 1))
        source_author_limit = max(1, int(max_authors or 1))
        scroll_limit = dynamic_search_scroll_limit(source_seed_limit, max_topic_scrolls)
        source_prefix = source.url.lower() + "|"
        source_authors: dict[str, TikTokAuthorSeed] = {
            _author_key(seed.profile_url, seed.author_id): seed
            for key, seed in authors.items()
            if str(key).lower().startswith(source_prefix)
        }
        source_seen_links: set[str] = {
            link
            for seed in source_authors.values()
            for link in seed.seed_links
            if link
        }
        source_inspected_count = 0
        source_new_author_count = len(source_authors)
        log_line(
            log_callback,
            f"  本话题采样配额：最多检查 {source_seed_limit} 个种子视频，最多进入 {source_author_limit} 个作者主页；已恢复 {source_new_author_count} 个作者种子。",
        )
        no_new_rounds = 0
        source_seen_count = 0

        for scroll_index in range(scroll_limit):
            if (
                source_inspected_count >= source_seed_limit
                or source_new_author_count >= source_author_limit
                or should_stop(stop_event)
            ):
                break
            if wait_if_paused(pause_event, stop_event):
                break

            new_items = collect_visible_video_items(topic_page, source_seen_links)
            source_seen_count += len(new_items)
            if not new_items:
                no_new_rounds += 1
            else:
                no_new_rounds = 0

            for item in new_items:
                if (
                    source_inspected_count >= source_seed_limit
                    or source_new_author_count >= source_author_limit
                    or should_stop(stop_event)
                ):
                    break
                video_url = item.get("视频链接", "")
                if not video_url:
                    continue
                try:
                    derived_publish_time = derive_publish_time_from_video_url(video_url)
                    if limit_time_bool and start_dt and end_dt and derived_publish_time and not in_date_range(derived_publish_time, start_dt, end_dt):
                        inspected_count += 1
                        source_inspected_count += 1
                        log_line(log_callback, f"  跳过种子：视频 ID 时间不在范围内（{derived_publish_time}）")
                        continue

                    row = extract_video_row(
                        detail_page,
                        source.label,
                        video_url,
                        item.get("播放量", ""),
                        profile_url=item.get("博主主页链接", ""),
                        stop_event=stop_event,
                    )
                    inspected_count += 1
                    source_inspected_count += 1
                    if limit_time_bool and start_dt and end_dt and not in_date_range(row.get("发布时间", ""), start_dt, end_dt):
                        log_line(log_callback, f"  跳过种子：发布时间不在范围内（{row.get('发布时间') or '未解析'}）")
                        continue
                    author_key = _author_key(row.get("博主主页链接", ""), row.get("博主ID", ""))
                    is_new_author = author_key not in source_authors
                    if is_new_author and source_new_author_count >= source_author_limit:
                        continue
                    seed = merge_seed_author(source_authors, source.label, video_url, row)
                    if seed:
                        if is_new_author:
                            source_new_author_count += 1
                        log_line(log_callback, f"  发现作者种子 本话题 {source_inspected_count}/{source_seed_limit}，累计 {inspected_count}: {video_url}")
                except Exception as exc:
                    inspected_count += 1
                    source_inspected_count += 1
                    log_warn(log_callback, f"  跳过一个话题候选视频：{exc}")

            if no_new_rounds >= no_new_scroll_limit and scroll_index >= 5:
                break
            if (
                source_inspected_count >= source_seed_limit
                or source_new_author_count >= source_author_limit
            ):
                break
            trigger_search_lazy_load(topic_page)
            if interruptible_sleep(topic_scroll_pause, stop_event):
                break
        if source_seen_count == 0:
            log_warn(log_callback, f"跳过话题：未发现可采集视频，可能是话题不存在、无公开内容或页面未加载成功：{source.label}")
        for seed in source_authors.values():
            authors[_topic_author_key(source, seed)] = seed
        if not should_stop(stop_event):
            completed_sources.add(source_id)
            if seed_cache_callback:
                seed_cache_callback(authors, completed_sources)
        log_line(log_callback, f"  本话题发现 {len(source_authors)} 个去重作者，累计待采集 {len(authors)} 个话题作者项。")

    return authors


def collect_hashtag_seed_authors_parallel(
    cdp_port_or_url: str,
    sources: list[HashtagSource],
    start_dt: datetime | None,
    end_dt: datetime | None,
    limit_time_bool: bool,
    log_callback,
    stop_event=None,
    pause_event=None,
    max_seed_works: int = 300,
    max_authors: int = 100,
    max_topic_scrolls: int = MAX_SEARCH_SCROLLS,
    topic_scroll_pause: float = SEARCH_SCROLL_PAUSE,
    no_new_scroll_limit: int = 12,
    page_timeout: int = PAGE_LOAD_TIMEOUT,
    initial_authors: dict[str, TikTokAuthorSeed] | None = None,
    completed_sources: set[str] | None = None,
    seed_cache_callback=None,
    parallel_windows: int = 1,
) -> dict[str, TikTokAuthorSeed]:
    authors: dict[str, TikTokAuthorSeed] = dict(initial_authors or {})
    completed_sources = set(completed_sources or set())
    worker_count = normalize_parallel_windows({"parallel_windows": parallel_windows})

    for source_index, source in enumerate(sources, 1):
        source_id = source.url
        if source_id in completed_sources:
            log_line(log_callback, f"[{source_index}/{len(sources)}] resume skip completed seed source: {source.label}")
            continue
        if should_stop(stop_event):
            break
        if wait_if_paused(pause_event, stop_event):
            break

        source_seed_limit = max(1, int(max_seed_works or 1))
        source_author_limit = max(1, int(max_authors or 1))
        scroll_limit = dynamic_search_scroll_limit(source_seed_limit, max_topic_scrolls)
        source_prefix = source.url.lower() + "|"
        source_authors: dict[str, TikTokAuthorSeed] = {
            _author_key(seed.profile_url, seed.author_id): seed
            for key, seed in authors.items()
            if str(key).lower().startswith(source_prefix)
        }
        source_seen_links: set[str] = {
            link
            for seed in source_authors.values()
            for link in seed.seed_links
            if link
        }
        source_inspected_count = 0
        source_new_author_count = len(source_authors)
        source_seen_count = 0
        lock = threading.RLock()
        active_workers = min(worker_count, source_seed_limit)

        log_line(
            log_callback,
            f"[{source_index}/{len(sources)}] parallel topic seed discovery: {source.label} "
            f"windows={active_workers}, seed_limit={source_seed_limit}, author_limit={source_author_limit}, "
            f"cached_authors={source_new_author_count}",
        )

        def _limits_reached() -> bool:
            with lock:
                return source_inspected_count >= source_seed_limit or source_new_author_count >= source_author_limit

        def _reserve_video(video_url: str) -> tuple[bool, int]:
            nonlocal source_inspected_count, source_seen_count
            if not video_url:
                return False, source_inspected_count
            normalized_video_url = str(video_url).strip()
            with lock:
                if (
                    not normalized_video_url
                    or normalized_video_url in source_seen_links
                    or source_inspected_count >= source_seed_limit
                    or source_new_author_count >= source_author_limit
                ):
                    return False, source_inspected_count
                source_seen_links.add(normalized_video_url)
                source_inspected_count += 1
                source_seen_count += 1
                return True, source_inspected_count

        def _merge_author(video_url: str, row: dict[str, str]) -> tuple[TikTokAuthorSeed | None, bool, int]:
            nonlocal source_new_author_count
            with lock:
                author_key = _author_key(row.get("博主主页链接", ""), row.get("博主ID", ""))
                is_new_author = bool(author_key and author_key not in source_authors)
                if is_new_author and source_new_author_count >= source_author_limit:
                    return None, False, source_new_author_count
                seed = merge_seed_author(source_authors, source.label, video_url, row)
                if seed and is_new_author:
                    source_new_author_count += 1
                return seed, is_new_author, source_new_author_count

        def _worker(worker_index: int) -> int:
            topic_page = detail_page = None
            inspected_by_worker = 0
            local_seen_links: set[str] = set()
            with sync_playwright() as worker_playwright:
                _, context = connect_existing_chromium(worker_playwright, cdp_port_or_url)
                topic_page = context.new_page()
                detail_page = context.new_page()
                try:
                    if not open_hashtag_page(
                        topic_page,
                        source,
                        stop_event=stop_event,
                        log_callback=log_callback,
                        page_timeout=page_timeout,
                    ):
                        log_warn(log_callback, f"[T{worker_index}] topic page not ready: {source.label}")
                        return inspected_by_worker

                    no_new_rounds = 0
                    for scroll_index in range(scroll_limit):
                        if _limits_reached() or should_stop(stop_event):
                            break
                        if wait_if_paused(pause_event, stop_event):
                            break

                        new_items = collect_visible_video_items(topic_page, local_seen_links)
                        if not new_items:
                            no_new_rounds += 1
                        else:
                            no_new_rounds = 0

                        for item in new_items:
                            if _limits_reached() or should_stop(stop_event):
                                break
                            video_url = item.get("视频链接", "")
                            reserved, current_count = _reserve_video(video_url)
                            if not reserved:
                                continue
                            inspected_by_worker += 1
                            try:
                                derived_publish_time = derive_publish_time_from_video_url(video_url)
                                if limit_time_bool and start_dt and end_dt and derived_publish_time and not in_date_range(derived_publish_time, start_dt, end_dt):
                                    log_line(log_callback, f"  [T{worker_index}] skip seed outside video-id time: {derived_publish_time}")
                                    continue

                                row = extract_video_row(
                                    detail_page,
                                    source.label,
                                    video_url,
                                    item.get("播放量", ""),
                                    profile_url=item.get("博主主页链接", ""),
                                    stop_event=stop_event,
                                )
                                if limit_time_bool and start_dt and end_dt and not in_date_range(row.get("发布时间", ""), start_dt, end_dt):
                                    log_line(log_callback, f"  [T{worker_index}] skip seed outside publish time: {row.get('发布时间') or 'unknown'}")
                                    continue
                                seed, is_new_author, author_count = _merge_author(video_url, row)
                                if seed:
                                    marker = "new" if is_new_author else "seen"
                                    log_line(
                                        log_callback,
                                        f"  [T{worker_index}] seed {current_count}/{source_seed_limit}, "
                                        f"authors={author_count}/{source_author_limit}, {marker}: {video_url}",
                                    )
                            except Exception as exc:
                                log_warn(log_callback, f"  [T{worker_index}] skip seed video: {exc}")

                        if no_new_rounds >= no_new_scroll_limit and scroll_index >= 5:
                            break
                        trigger_search_lazy_load(topic_page)
                        if interruptible_sleep(topic_scroll_pause, stop_event):
                            break
                finally:
                    for opened_page in (topic_page, detail_page):
                        try:
                            if opened_page is not None and not opened_page.is_closed():
                                opened_page.close()
                        except Exception:
                            pass
            return inspected_by_worker

        with ThreadPoolExecutor(max_workers=active_workers) as executor:
            futures = [executor.submit(_worker, worker_index) for worker_index in range(1, active_workers + 1)]
            for future in as_completed(futures):
                future.result()

        for seed in source_authors.values():
            authors[_topic_author_key(source, seed)] = seed
        if source_seen_count == 0:
            log_warn(log_callback, f"topic produced no seed videos: {source.label}")
        if not should_stop(stop_event):
            completed_sources.add(source_id)
            if seed_cache_callback:
                seed_cache_callback(authors, completed_sources)
        log_line(
            log_callback,
            f"topic done: {source.label}, inspected={source_inspected_count}, authors={len(source_authors)}, total_items={len(authors)}",
        )

    return authors


def run_tiktok_hashtag_author_works_spider(
    hashtag_inputs,
    limit_time_str,
    start_date,
    end_date,
    cdp_port_or_url,
    log_callback,
    finish_callback,
    stop_event=None,
    pause_event=None,
    config=None,
):
    ensure_playwright_available()
    if config is None:
        config = {}

    max_seed_works = int(config.get("max_seed_works", 300))
    max_authors = int(config.get("max_authors", 100))
    quick_mode_value = config.get("quick_mode", "是")
    max_profile_works = resolve_profile_work_limit(config, quick_mode_value)
    max_topic_scrolls = int(config.get("max_topic_scrolls", config.get("max_search_scrolls", MAX_SEARCH_SCROLLS)))
    max_profile_scrolls = int(config.get("max_profile_scrolls", config.get("max_scrolls", DEFAULT_MAX_SCROLLS)))
    page_timeout = int(config.get("page_load_timeout", PAGE_LOAD_TIMEOUT))
    topic_scroll_pause = float(config.get("scroll_interval", SEARCH_SCROLL_PAUSE))
    profile_scroll_interval = float(config.get("profile_scroll_interval", SCROLL_INTERVAL_SECONDS))
    no_new_scroll_limit = int(config.get("no_new_scroll_limit", NO_NEW_SCROLL_LIMIT))
    scroll_px = int(config.get("scroll_px", SCROLL_PX))
    detail_load_timeout = int(config.get("detail_load_timeout", DETAIL_LOAD_TIMEOUT))
    detail_delay_min = float(config.get("detail_delay_min", DETAIL_DELAY_MIN_SECONDS))
    detail_delay_max = float(config.get("detail_delay_max", DETAIL_DELAY_MAX_SECONDS))
    parallel_windows = normalize_parallel_windows(config)

    completed_path = None
    topic_page = seed_detail_page = profile_info_page = profile_page = works_detail_page = None
    try:
        sources = parse_hashtag_sources(hashtag_inputs, skip_invalid=True, log_callback=log_callback)
        if not sources:
            raise ValueError("至少需要输入一个有效的 TikTok 话题关键词。")

        limit_time_bool = limit_time_str == "是"
        start_dt = end_dt = None
        if limit_time_bool:
            start_dt, end_dt = parse_date_range(start_date, end_date)

        if quick_mode_enabled(quick_mode_value):
            log_line(log_callback, f"快速模式已开启：作者主页作品最多取最新 {QUICK_PROFILE_WORK_LIMIT} 条，不足则采完即停。")
        checkpoint = open_task_checkpoint(
            "tiktok_hashtag_author_works",
            {
                "output_schema": "profile_video_sheets_per_topic_v2",
                "hashtags": [source.url for source in sources],
                "max_seed_works_per_topic": max_seed_works,
                "max_authors_per_topic": max_authors,
                "limit_time": limit_time_bool,
                "start_date": start_date if limit_time_bool else "",
                "end_date": end_date if limit_time_bool else "",
                "quick_mode": quick_mode_value,
                "max_profile_works": max_profile_works,
            },
            log_callback=log_callback,
            merge_on_keys=("hashtags",),
        )
        source_ids = [source.url for source in sources]
        cached_authors, cached_sources = load_seed_author_cache(checkpoint, source_ids, log_callback=log_callback)
        ensure_chrome_for_cdp(cdp_port_or_url, log_callback=log_callback)
        if parallel_windows > 1:
            authors = collect_hashtag_seed_authors_parallel(
                cdp_port_or_url,
                sources,
                start_dt,
                end_dt,
                limit_time_bool,
                log_callback,
                stop_event=stop_event,
                pause_event=pause_event,
                max_seed_works=max_seed_works,
                max_authors=max_authors,
                max_topic_scrolls=max_topic_scrolls,
                topic_scroll_pause=topic_scroll_pause,
                no_new_scroll_limit=no_new_scroll_limit,
                page_timeout=page_timeout,
                initial_authors=cached_authors,
                completed_sources=cached_sources,
                seed_cache_callback=lambda current_authors, current_sources: save_seed_author_cache(
                    checkpoint,
                    source_ids,
                    current_authors,
                    current_sources,
                ),
                parallel_windows=parallel_windows,
            )
            if not authors:
                log_warn(log_callback, "No valid authors found from hashtag pages.")
                return

            default_output_path = build_output_path(
                "tiktok",
                f"tiktok_hashtag_author_works_{time.strftime('%Y%m%d_%H%M%S')}.xlsx",
                channel="hashtag_author_works",
            )
            output_path, writer = open_checkpointed_multi_sheet_writer(
                checkpoint,
                default_output_path,
                {
                    "博主信息": HASHTAG_AUTHOR_FIELDS,
                    "博主对应视频": HASHTAG_VIDEO_FIELDS,
                },
                log_callback=log_callback,
                autosave_every=5000,
            )
            checkpoint.add_output_path(output_path)

            sheet_names = list(getattr(writer, "sheets_fields", {}).keys())
            collect_author_works_with_parallel_windows(
                list(authors.values()),
                checkpoint=checkpoint,
                output_path=output_path,
                writer=writer,
                cdp_port_or_url=cdp_port_or_url,
                log_callback=log_callback,
                stop_event=stop_event,
                pause_event=pause_event,
                parallel_windows=parallel_windows,
                page_timeout=page_timeout,
                max_profile_scrolls=max_profile_scrolls,
                max_profile_works=max_profile_works,
                profile_scroll_interval=profile_scroll_interval,
                no_new_scroll_limit=no_new_scroll_limit,
                scroll_px=scroll_px,
                detail_load_timeout=detail_load_timeout,
                detail_delay_min=detail_delay_min,
                detail_delay_max=detail_delay_max,
                limit_time_bool=limit_time_bool,
                start_dt=start_dt,
                end_dt=end_dt,
                author_sheet_name=sheet_names[0],
                video_sheet_name=sheet_names[1],
                author_row_builder=_author_sheet_row_for_hashtag,
                video_row_builder=_video_row_for_hashtag,
                checkpoint_key_builder=_seed_checkpoint_key,
            )
            writer.save()
            completed_path = output_path
            log_line(log_callback, f"完成，已保存：{output_path}")
            return

        with sync_playwright() as playwright:
            _, context = connect_existing_chromium(playwright, cdp_port_or_url)
            topic_page = context.new_page()
            seed_detail_page = context.new_page()

            authors = collect_hashtag_seed_authors(
                topic_page,
                seed_detail_page,
                sources,
                start_dt,
                end_dt,
                limit_time_bool,
                log_callback,
                stop_event=stop_event,
                pause_event=pause_event,
                max_seed_works=max_seed_works,
                max_authors=max_authors,
                max_topic_scrolls=max_topic_scrolls,
                topic_scroll_pause=topic_scroll_pause,
                no_new_scroll_limit=no_new_scroll_limit,
                page_timeout=page_timeout,
                initial_authors=cached_authors,
                completed_sources=cached_sources,
                seed_cache_callback=lambda current_authors, current_sources: save_seed_author_cache(
                    checkpoint,
                    source_ids,
                    current_authors,
                    current_sources,
                ),
            )
            if not authors:
                log_warn(log_callback, "没有从话题页中发现有效作者。")
                return

            profile_page = topic_page
            topic_page = None
            profile_info_page = seed_detail_page
            works_detail_page = seed_detail_page
            seed_detail_page = None
            log_line(log_callback, "已复用话题页和详情页进入作者阶段，避免额外打开空白标签页。")

            default_output_path = build_output_path(
                "tiktok",
                f"tiktok_hashtag_author_works_{time.strftime('%Y%m%d_%H%M%S')}.xlsx",
                channel="hashtag_author_works",
            )
            output_path, writer = open_checkpointed_multi_sheet_writer(
                checkpoint,
                default_output_path,
                {
                    "博主信息": HASHTAG_AUTHOR_FIELDS,
                    "博主对应视频": HASHTAG_VIDEO_FIELDS,
                },
                log_callback=log_callback,
                autosave_every=5000,
            )
            checkpoint.add_output_path(output_path)

            video_index = 0
            if hasattr(writer, "worksheets") and "博主对应视频" in writer.worksheets:
                video_index = max(0, writer.worksheets["博主对应视频"].max_row - 1)

            author_seeds = list(authors.values())
            if parallel_windows > 1:
                for opened_page in (topic_page, seed_detail_page, profile_info_page, profile_page, works_detail_page):
                    try:
                        if opened_page is not None and not opened_page.is_closed():
                            opened_page.close()
                    except Exception:
                        pass
                topic_page = seed_detail_page = profile_info_page = profile_page = works_detail_page = None
                sheet_names = list(getattr(writer, "sheets_fields", {}).keys())
                collect_author_works_with_parallel_windows(
                    author_seeds,
                    checkpoint=checkpoint,
                    output_path=output_path,
                    writer=writer,
                    cdp_port_or_url=cdp_port_or_url,
                    log_callback=log_callback,
                    stop_event=stop_event,
                    pause_event=pause_event,
                    parallel_windows=parallel_windows,
                    page_timeout=page_timeout,
                    max_profile_scrolls=max_profile_scrolls,
                    max_profile_works=max_profile_works,
                    profile_scroll_interval=profile_scroll_interval,
                    no_new_scroll_limit=no_new_scroll_limit,
                    scroll_px=scroll_px,
                    detail_load_timeout=detail_load_timeout,
                    detail_delay_min=detail_delay_min,
                    detail_delay_max=detail_delay_max,
                    limit_time_bool=limit_time_bool,
                    start_dt=start_dt,
                    end_dt=end_dt,
                    author_sheet_name=sheet_names[0],
                    video_sheet_name=sheet_names[1],
                    author_row_builder=_author_sheet_row_for_hashtag,
                    video_row_builder=_video_row_for_hashtag,
                    checkpoint_key_builder=_seed_checkpoint_key,
                )
                writer.save()
                completed_path = output_path
                log_line(log_callback, f"完成，已保存：{output_path}")
                return

            total_authors = len(author_seeds)
            for index, seed in enumerate(author_seeds, 1):
                if should_stop(stop_event):
                    break
                if wait_if_paused(pause_event, stop_event):
                    break
                checkpoint_key = _seed_checkpoint_key(seed)
                claimed, claim_status = checkpoint.claim_item(checkpoint_key, positive_count_fields=("works_count",))
                if not claimed:
                    if claim_status == "active":
                        log_line(log_callback, f"[{index}/{total_authors}] 双开分流跳过正在处理的作者：{seed.profile_url}")
                    else:
                        log_line(log_callback, f"[{index}/{total_authors}] 断点续跑跳过已完成作者：{seed.profile_url}")
                    continue
                log_line(log_callback, f"[{index}/{total_authors}] 进入博主主页：{seed.profile_url}")
                profile_record_ok = False
                try:
                    profile_record = extract_profile_row(
                        profile_info_page,
                        seed.profile_url,
                        page_load_timeout=page_timeout,
                        captcha_wait=8,
                        stop_event=stop_event,
                    )
                    profile_record_ok = True
                except Exception as exc:
                    log_warn(log_callback, f"  博主信息补充失败：{exc}")
                    profile_record = {
                        "博主主页链接": seed.profile_url,
                        "博主名称": seed.author_name,
                        "博主ID": seed.author_id,
                        "粉丝量": seed.followers,
                        "作者简介": seed.bio,
                    }

                works_collected_ok = False
                try:
                    works = collect_profile_video_details(
                        profile_page,
                        works_detail_page,
                        seed.profile_url,
                        None,
                        None,
                        False,
                        log_callback,
                        stop_event=stop_event,
                        pause_event=pause_event,
                        max_scrolls=max_profile_scrolls,
                        max_collect=max_profile_works,
                        page_load_timeout=page_timeout,
                        scroll_interval=profile_scroll_interval,
                        no_new_scroll_limit=no_new_scroll_limit,
                        scroll_px=scroll_px,
                        detail_load_timeout=detail_load_timeout,
                        detail_delay_min=detail_delay_min,
                        detail_delay_max=detail_delay_max,
                    )
                    works_collected_ok = True
                except Exception as exc:
                    log_warn(log_callback, f"  博主作品采集失败：{exc}")
                    works = []

                writer.writerow("博主信息", _author_sheet_row_for_hashtag(seed, profile_record, works, limit_time_bool, start_dt, end_dt))
                for work in works:
                    video_index += 1
                    writer.writerow("博主对应视频", _video_row_for_hashtag(video_index, seed, profile_record, work))
                if profile_record_ok and works_collected_ok and len(works) > 0:
                    checkpoint.mark_completed(
                        checkpoint_key,
                        {"output_path": output_path, "index": index, "profile_url": seed.profile_url, "topic": seed.keywords[0] if seed.keywords else "", "works_count": len(works)},
                    )
                else:
                    checkpoint.release_item(checkpoint_key)
                    if works_collected_ok and len(works) == 0:
                        log_warn(log_callback, "  未采到主页作品，未写入断点完成标记，下次会继续重试。")
                    else:
                        log_warn(log_callback, "  本轮未完整采集成功，未写入断点完成标记，下次会继续重试。")
                log_line(log_callback, f"  写入博主：{profile_record.get('博主ID') or seed.author_id or seed.profile_url}，作品 {len(works)} 条。")

            writer.save()
            completed_path = output_path
            log_line(log_callback, f"完成，已保存：{output_path}")
    except Exception as exc:
        log_error(log_callback, f"运行失败：{exc}")
    finally:
        for page in (topic_page, seed_detail_page, profile_info_page, profile_page, works_detail_page):
            try:
                if page is not None and not page.is_closed():
                    page.close()
            except Exception:
                pass
        finish_callback(completed_path)
