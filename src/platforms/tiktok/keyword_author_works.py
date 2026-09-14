from __future__ import annotations

import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime
from queue import Empty, Queue

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
from src.core.parallel import AtomicCounter, ThreadSafeWriter, normalize_parallel_windows
from src.platforms.tiktok.keyword import (
    MAX_SEARCH_SCROLLS,
    SEARCH_SCROLL_PAUSE,
    collect_visible_video_items,
    derive_publish_time_from_video_url,
    dynamic_search_scroll_limit,
    extract_video_row,
    in_date_range,
    open_search_page,
    parse_date_range,
    trigger_search_lazy_load,
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
    parse_video_id,
)
from src.platforms.tiktok.profiles import (
    extract_profile_row,
    normalize_profile_url,
    profile_id_from_url,
)


AUTHOR_FIELDS = [
    "搜索词",
    "命中作品数",
    "命中作品链接列表",
    "作者主页链接",
    "作者名称",
    "作者ID",
    "粉丝数",
    "作者简介",
    "采集作品数",
    "时间窗口内作品数",
]
VIDEO_FIELDS = [
    "搜索词",
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
    "作品类型",
    "直播状态",
    "关联作品标题",
    "关联作品链接",
    "作品时长",
    "作品简介",
    "浏览量",
    "播放量",
    "点赞数",
    "评论数",
    "收藏量",
    "分享数",
]
CSV_FIELDS = [
    *AUTHOR_FIELDS,
    "作品标题列表",
    "作品链接列表",
    "作品发布时间列表",
]
QUICK_PROFILE_WORK_LIMIT = 50
SEED_CACHE_STATE_KEY = "seed_author_cache_v1"


def quick_mode_enabled(value: str | None) -> bool:
    return str(value or "是").strip() == "是"


def resolve_profile_work_limit(config: dict | None, quick_mode_value: str | None = "是") -> int:
    configured_limit = int((config or {}).get("max_profile_works_per_author", QUICK_PROFILE_WORK_LIMIT))
    return QUICK_PROFILE_WORK_LIMIT if quick_mode_enabled(quick_mode_value) else configured_limit


@dataclass
class TikTokAuthorSeed:
    profile_url: str
    author_name: str = ""
    author_id: str = ""
    followers: str = ""
    bio: str = ""
    keywords: list[str] = field(default_factory=list)
    seed_links: list[str] = field(default_factory=list)


def _seed_to_dict(seed: TikTokAuthorSeed) -> dict[str, object]:
    return {
        "profile_url": seed.profile_url,
        "author_name": seed.author_name,
        "author_id": seed.author_id,
        "followers": seed.followers,
        "bio": seed.bio,
        "keywords": list(seed.keywords),
        "seed_links": list(seed.seed_links),
    }


def _seed_from_dict(data: dict) -> TikTokAuthorSeed | None:
    if not isinstance(data, dict):
        return None
    profile_url = str(data.get("profile_url") or "").strip()
    if not profile_url:
        return None
    return TikTokAuthorSeed(
        profile_url=profile_url,
        author_name=str(data.get("author_name") or ""),
        author_id=str(data.get("author_id") or ""),
        followers=str(data.get("followers") or ""),
        bio=str(data.get("bio") or ""),
        keywords=[str(item) for item in data.get("keywords", []) if str(item).strip()],
        seed_links=[str(item) for item in data.get("seed_links", []) if str(item).strip()],
    )


def load_seed_author_cache(checkpoint, source_ids: list[str], log_callback=None) -> tuple[dict[str, TikTokAuthorSeed], set[str]]:
    state = checkpoint.get_state(SEED_CACHE_STATE_KEY, {})
    if not isinstance(state, dict) or state.get("source_ids") != list(source_ids):
        return {}, set()

    authors: dict[str, TikTokAuthorSeed] = {}
    for item in state.get("authors", []):
        if not isinstance(item, dict):
            continue
        seed = _seed_from_dict(item.get("seed", {}))
        key = str(item.get("key") or "").strip().lower()
        if key and seed:
            authors[key] = seed
    completed_sources = {str(item) for item in state.get("completed_sources", []) if str(item).strip()}
    if authors or completed_sources:
        log_line(log_callback, f"断点续跑：已恢复 {len(authors)} 个已发现作者种子，{len(completed_sources)}/{len(source_ids)} 个入口已完成种子发现。")
    return authors, completed_sources


def save_seed_author_cache(checkpoint, source_ids: list[str], authors: dict[str, TikTokAuthorSeed], completed_sources: set[str]) -> None:
    checkpoint.set_state(
        SEED_CACHE_STATE_KEY,
        {
            "source_ids": list(source_ids),
            "completed_sources": sorted(str(item) for item in completed_sources),
            "authors": [{"key": key, "seed": _seed_to_dict(seed)} for key, seed in authors.items()],
        },
    )


def ensure_playwright_available() -> None:
    if sync_playwright is None:
        raise ModuleNotFoundError("playwright is required for TikTok keyword author works scraping")


def _author_key(profile_url: str, author_id: str = "") -> str:
    normalized = normalize_profile_url(profile_url) or profile_url
    handle = (author_id or "").strip().lower().lstrip("@")
    if handle:
        return handle
    match = re.search(r"tiktok\.com/@([^/?#]+)", normalized or "", re.I)
    return match.group(1).lower() if match else (normalized or "").lower()


def merge_seed_author(authors: dict[str, TikTokAuthorSeed], keyword: str, video_url: str, row: dict[str, str]) -> TikTokAuthorSeed | None:
    profile_url = normalize_profile_url(row.get("博主主页链接", ""))
    author_id = row.get("博主ID", "")
    if not profile_url and author_id:
        profile_url = normalize_profile_url(f"https://www.tiktok.com/{author_id}")
    if not profile_url:
        return None

    key = _author_key(profile_url, author_id)
    seed = authors.get(key)
    if seed is None:
        seed = TikTokAuthorSeed(
            profile_url=profile_url,
            author_name=row.get("博主名称", ""),
            author_id=author_id or profile_id_from_url(profile_url),
            followers=row.get("粉丝量", ""),
            bio=row.get("作者简介", ""),
        )
        authors[key] = seed
    if keyword and keyword not in seed.keywords:
        seed.keywords.append(keyword)
    if video_url and video_url not in seed.seed_links:
        seed.seed_links.append(video_url)
    if not seed.author_name and row.get("博主名称"):
        seed.author_name = row["博主名称"]
    if not seed.author_id and (author_id or profile_id_from_url(profile_url)):
        seed.author_id = author_id or profile_id_from_url(profile_url)
    if not seed.followers and row.get("粉丝量"):
        seed.followers = row["粉丝量"]
    if not seed.bio and row.get("作者简介"):
        seed.bio = row["作者简介"]
    return seed


def _cell_text(value: str, limit: int | None = None) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if limit is not None and len(text) > limit:
        return text[:limit].rstrip()
    return text


def _join_cell(values) -> str:
    cleaned = [_cell_text(value) for value in values if _cell_text(value)]
    return "\n".join(cleaned)


def _count_works_in_window(
    works: list[dict[str, str]],
    limit_time_bool: bool = False,
    start_dt: datetime | None = None,
    end_dt: datetime | None = None,
) -> int:
    if not limit_time_bool or not start_dt or not end_dt:
        return len(works)
    return sum(1 for work in works if in_date_range(work.get("published_at", ""), start_dt, end_dt))


def build_author_row(
    seed: TikTokAuthorSeed,
    profile_record: dict[str, str],
    works: list[dict[str, str]],
    limit_time_bool: bool = False,
    start_dt: datetime | None = None,
    end_dt: datetime | None = None,
) -> dict[str, str]:
    titles = [work.get("desc", "") for work in works]
    links = [work.get("video_url", "") for work in works]
    publish_times = [work.get("published_at", "") for work in works]
    in_window_count = _count_works_in_window(works, limit_time_bool, start_dt, end_dt)
    return {
        "搜索词": _join_cell(seed.keywords),
        "命中作品数": str(len(seed.seed_links)),
        "命中作品链接列表": _join_cell(seed.seed_links),
        "作者主页链接": profile_record.get("博主主页链接") or seed.profile_url,
        "作者名称": profile_record.get("博主名称") or seed.author_name,
        "作者ID": profile_record.get("博主ID") or seed.author_id,
        "粉丝数": profile_record.get("粉丝量") or seed.followers,
        "作者简介": profile_record.get("作者简介") or seed.bio,
        "采集作品数": str(len(works)),
        "时间窗口内作品数": str(in_window_count),
        "作品标题列表": _join_cell(titles),
        "作品链接列表": _join_cell(links),
        "作品发布时间列表": _join_cell(publish_times),
    }


def build_author_sheet_row(
    seed: TikTokAuthorSeed,
    profile_record: dict[str, str],
    works: list[dict[str, str]],
    limit_time_bool: bool = False,
    start_dt: datetime | None = None,
    end_dt: datetime | None = None,
    *,
    source_field: str = "搜索词",
) -> dict[str, str]:
    row = build_author_row(seed, profile_record, works, limit_time_bool, start_dt, end_dt)
    if source_field != "搜索词":
        row[source_field] = row.pop("搜索词", "")
    fields = [source_field, *[field for field in AUTHOR_FIELDS if field != "搜索词"]]
    return {field: row.get(field, "") for field in fields}


def build_video_row(
    index: int,
    seed: TikTokAuthorSeed,
    profile_record: dict[str, str],
    work: dict[str, str],
    *,
    source_field: str = "搜索词",
) -> dict[str, str]:
    profile_url = profile_record.get("博主主页链接") or seed.profile_url
    author_name = profile_record.get("博主名称") or seed.author_name
    author_id = profile_record.get("博主ID") or seed.author_id
    video_url = work.get("video_url", "")
    title = _cell_text(work.get("desc", ""), limit=120)
    desc = _cell_text(work.get("desc", ""))
    row = {
        source_field: _join_cell(seed.keywords),
        "序号": str(index),
        "编号": parse_video_id(video_url),
        "视频链接": video_url,
        "作品链接": video_url,
        "博主主页链接": profile_url,
        "作者主页链接": profile_url,
        "标题": title,
        "作品内容": f"{desc}[视频]" if desc else "",
        "频道名称": author_name,
        "发布日期": work.get("published_at", ""),
        "作品类型": "视频",
        "直播状态": "非直播",
        "关联作品标题": "",
        "关联作品链接": "",
        "作品时长": "",
        "作品简介": desc,
        "浏览量": work.get("浏览量", ""),
        "播放量": work.get("播放量", "") or work.get("play_count", ""),
        "点赞数": work.get("likes", ""),
        "评论数": work.get("comments", ""),
        "收藏量": work.get("collects", ""),
        "分享数": work.get("shares", ""),
    }
    return row


def collect_seed_authors(
    search_page,
    detail_page,
    keywords: list[str],
    start_dt: datetime | None,
    end_dt: datetime | None,
    limit_time_bool: bool,
    log_callback,
    stop_event=None,
    pause_event=None,
    max_seed_works: int = 300,
    max_authors: int = 100,
    max_search_scrolls: int = MAX_SEARCH_SCROLLS,
    search_scroll_pause: float = SEARCH_SCROLL_PAUSE,
    no_new_scroll_limit: int = 12,
    initial_authors: dict[str, TikTokAuthorSeed] | None = None,
    completed_sources: set[str] | None = None,
    seed_cache_callback=None,
) -> dict[str, TikTokAuthorSeed]:
    authors: dict[str, TikTokAuthorSeed] = dict(initial_authors or {})
    completed_sources = set(completed_sources or set())
    seen_links: set[str] = set()
    inspected_count = 0

    for keyword_index, keyword in enumerate(keywords, 1):
        source_id = str(keyword)
        if source_id in completed_sources:
            log_line(log_callback, f"[{keyword_index}/{len(keywords)}] 断点续跑跳过已完成种子发现关键词：{keyword}")
            continue
        if inspected_count >= max_seed_works or should_stop(stop_event):
            break
        if wait_if_paused(pause_event, stop_event):
            break

        log_line(log_callback, f"[{keyword_index}/{len(keywords)}] 搜索作者种子：{keyword}")
        open_search_page(search_page, keyword, stop_event=stop_event, log_callback=log_callback)
        scroll_limit = dynamic_search_scroll_limit(max_seed_works, max_search_scrolls)
        no_new_rounds = 0

        for scroll_index in range(scroll_limit):
            if inspected_count >= max_seed_works or should_stop(stop_event):
                break
            if wait_if_paused(pause_event, stop_event):
                break

            new_items = collect_visible_video_items(search_page, seen_links)
            if not new_items:
                no_new_rounds += 1
            else:
                no_new_rounds = 0

            for item in new_items:
                if inspected_count >= max_seed_works or should_stop(stop_event):
                    break
                video_url = item.get("视频链接", "")
                if not video_url:
                    continue
                try:
                    derived_publish_time = derive_publish_time_from_video_url(video_url)
                    if limit_time_bool and start_dt and end_dt and derived_publish_time and not in_date_range(derived_publish_time, start_dt, end_dt):
                        inspected_count += 1
                        log_line(log_callback, f"  跳过种子：视频 ID 时间不在范围内（{derived_publish_time}）")
                        continue

                    row = extract_video_row(
                        detail_page,
                        keyword,
                        video_url,
                        item.get("播放量", ""),
                        profile_url=item.get("博主主页链接", ""),
                        stop_event=stop_event,
                    )
                    inspected_count += 1
                    if limit_time_bool and start_dt and end_dt and not in_date_range(row.get("发布时间", ""), start_dt, end_dt):
                        log_line(log_callback, f"  跳过种子：发布时间不在范围内（{row.get('发布时间') or '未解析'}）")
                        continue
                    author_key = _author_key(row.get("博主主页链接", ""), row.get("博主ID", ""))
                    if len(authors) >= max_authors and author_key not in authors:
                        continue
                    if merge_seed_author(authors, keyword, video_url, row):
                        log_line(log_callback, f"  发现作者种子 {inspected_count}/{max_seed_works}: {video_url}")
                except Exception as exc:
                    inspected_count += 1
                    log_warn(log_callback, f"  跳过一个候选视频：{exc}")

            if no_new_rounds >= no_new_scroll_limit and scroll_index >= 5:
                break
            trigger_search_lazy_load(search_page)
            if interruptible_sleep(search_scroll_pause, stop_event):
                break
        if not should_stop(stop_event):
            completed_sources.add(source_id)
            if seed_cache_callback:
                seed_cache_callback(authors, completed_sources)

    return authors


def collect_author_works_with_parallel_windows(
    seeds: list[TikTokAuthorSeed],
    *,
    checkpoint,
    output_path: str,
    writer,
    cdp_port_or_url: str,
    log_callback,
    stop_event=None,
    pause_event=None,
    parallel_windows: int = 1,
    page_timeout: int = PAGE_LOAD_TIMEOUT,
    max_profile_scrolls: int = DEFAULT_MAX_SCROLLS,
    max_profile_works: int = QUICK_PROFILE_WORK_LIMIT,
    profile_scroll_interval: float = SCROLL_INTERVAL_SECONDS,
    no_new_scroll_limit: int = NO_NEW_SCROLL_LIMIT,
    scroll_px: int = SCROLL_PX,
    detail_load_timeout: int = DETAIL_LOAD_TIMEOUT,
    detail_delay_min: float = DETAIL_DELAY_MIN_SECONDS,
    detail_delay_max: float = DETAIL_DELAY_MAX_SECONDS,
    limit_time_bool: bool = False,
    start_dt: datetime | None = None,
    end_dt: datetime | None = None,
    author_sheet_name: str = "鍗氫富淇℃伅",
    video_sheet_name: str = "鍗氫富瀵瑰簲瑙嗛",
    author_row_builder=build_author_sheet_row,
    video_row_builder=build_video_row,
    checkpoint_key_builder=None,
    total_label: str | None = None,
) -> int:
    if not seeds:
        return 0
    worker_count = normalize_parallel_windows({"parallel_windows": parallel_windows})
    worker_count = min(worker_count, len(seeds))
    checkpoint_key_builder = checkpoint_key_builder or (lambda seed: seed.profile_url)
    total_label = total_label or str(len(seeds))
    safe_writer = ThreadSafeWriter(writer)
    raw_writer = safe_writer.raw
    start_video_index = 0
    if hasattr(raw_writer, "worksheets") and video_sheet_name in raw_writer.worksheets:
        start_video_index = max(0, raw_writer.worksheets[video_sheet_name].max_row - 1)
    video_counter = AtomicCounter(start_video_index)
    completed_counter = AtomicCounter(0)

    work_queue: Queue[tuple[int, TikTokAuthorSeed]] = Queue()
    for index, seed in enumerate(seeds, 1):
        work_queue.put((index, seed))

    log_line(log_callback, f"Parallel author windows enabled: {worker_count}. Runtime locks prevent duplicate profile pages.")

    def _worker(worker_index: int) -> int:
        owner_id = f"{checkpoint.run_id}:tiktok-author-window-{worker_index}"
        processed = 0
        profile_info_page = profile_page = works_detail_page = None
        with sync_playwright() as worker_playwright:
            _, context = connect_existing_chromium(worker_playwright, cdp_port_or_url)
            profile_info_page = context.new_page()
            profile_page = context.new_page()
            works_detail_page = context.new_page()
            try:
                while not should_stop(stop_event):
                    if wait_if_paused(pause_event, stop_event):
                        break
                    try:
                        index, seed = work_queue.get_nowait()
                    except Empty:
                        break
                    checkpoint_key = str(checkpoint_key_builder(seed))
                    try:
                        claimed, claim_status = checkpoint.claim_item(checkpoint_key, positive_count_fields=("works_count",))
                        if not claimed:
                            if claim_status == "active":
                                log_line(log_callback, f"[W{worker_index} {index}/{total_label}] 跳过正在处理的作者：{seed.profile_url}")
                            else:
                                log_line(log_callback, f"[W{worker_index} {index}/{total_label}] 跳过已完成作者：{seed.profile_url}")
                            continue

                        profile_lock_key = normalize_profile_url(seed.profile_url) or seed.profile_url
                        runtime_claimed, _ = checkpoint.claim_runtime_item(
                            profile_lock_key,
                            namespace="tiktok_profile_page",
                            owner_id=owner_id,
                        )
                        if not runtime_claimed:
                            checkpoint.release_item(checkpoint_key)
                            log_line(log_callback, f"[W{worker_index} {index}/{total_label}] 作者主页已有窗口在采，跳过本轮：{seed.profile_url}")
                            continue

                        log_line(log_callback, f"[W{worker_index} {index}/{total_label}] 进入博主主页：{seed.profile_url}")
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
                            log_warn(log_callback, f"  [W{worker_index}] 博主信息补充失败：{exc}")
                            profile_record = {}

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
                            log_warn(log_callback, f"  [W{worker_index}] 博主作品采集失败：{exc}")
                            works = []

                        safe_writer.writerow(
                            author_sheet_name,
                            author_row_builder(seed, profile_record, works, limit_time_bool, start_dt, end_dt),
                        )
                        for work in works:
                            safe_writer.writerow(video_sheet_name, video_row_builder(video_counter.next(), seed, profile_record, work))
                        if profile_record_ok and works_collected_ok and len(works) > 0:
                            checkpoint.mark_completed(
                                checkpoint_key,
                                {
                                    "output_path": output_path,
                                    "index": index,
                                    "profile_url": seed.profile_url,
                                    "works_count": len(works),
                                    "worker": worker_index,
                                },
                            )
                            completed_counter.next()
                        else:
                            checkpoint.release_item(checkpoint_key)
                            log_warn(log_callback, f"  [W{worker_index}] incomplete; checkpoint not completed.")
                        processed += 1
                        log_line(log_callback, f"  [W{worker_index}] wrote author {seed.author_id or seed.profile_url}, works={len(works)}")
                    finally:
                        try:
                            checkpoint.release_runtime_item(
                                normalize_profile_url(seed.profile_url) or seed.profile_url,
                                namespace="tiktok_profile_page",
                                owner_id=owner_id,
                            )
                        except Exception:
                            pass
                        work_queue.task_done()
            finally:
                for opened_page in (profile_info_page, profile_page, works_detail_page):
                    try:
                        if opened_page is not None and not opened_page.is_closed():
                            opened_page.close()
                    except Exception:
                        pass
        return processed

    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        futures = [executor.submit(_worker, worker_index) for worker_index in range(1, worker_count + 1)]
        for future in as_completed(futures):
            future.result()

    safe_writer.save()
    return completed_counter.value


def run_tiktok_keyword_author_works_spider(
    keywords_list,
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
    max_search_scrolls = int(config.get("max_search_scrolls", MAX_SEARCH_SCROLLS))
    max_profile_scrolls = int(config.get("max_profile_scrolls", config.get("max_scrolls", DEFAULT_MAX_SCROLLS)))
    page_timeout = int(config.get("page_load_timeout", PAGE_LOAD_TIMEOUT))
    search_scroll_pause = float(config.get("scroll_interval", SEARCH_SCROLL_PAUSE))
    profile_scroll_interval = float(config.get("profile_scroll_interval", SCROLL_INTERVAL_SECONDS))
    no_new_scroll_limit = int(config.get("no_new_scroll_limit", NO_NEW_SCROLL_LIMIT))
    scroll_px = int(config.get("scroll_px", SCROLL_PX))
    detail_load_timeout = int(config.get("detail_load_timeout", DETAIL_LOAD_TIMEOUT))
    detail_delay_min = float(config.get("detail_delay_min", DETAIL_DELAY_MIN_SECONDS))
    detail_delay_max = float(config.get("detail_delay_max", DETAIL_DELAY_MAX_SECONDS))
    parallel_windows = normalize_parallel_windows(config)

    completed_path = None
    search_page = seed_detail_page = profile_info_page = profile_page = works_detail_page = None
    try:
        limit_time_bool = limit_time_str == "是"
        start_dt = end_dt = None
        if limit_time_bool:
            start_dt, end_dt = parse_date_range(start_date, end_date)

        if quick_mode_enabled(quick_mode_value):
            log_line(log_callback, f"快速模式已开启：作者主页作品最多取最新 {QUICK_PROFILE_WORK_LIMIT} 条，不足则采完即停。")
        checkpoint = open_task_checkpoint(
            "tiktok_keyword_author_works",
            {
                "output_schema": "profile_video_sheets_v1",
                "keywords": list(keywords_list),
                "limit_time": limit_time_bool,
                "start_date": start_date if limit_time_bool else "",
                "end_date": end_date if limit_time_bool else "",
                "quick_mode": quick_mode_value,
                "max_profile_works": max_profile_works,
            },
            log_callback=log_callback,
        )
        source_ids = [str(keyword) for keyword in keywords_list]
        cached_authors, cached_sources = load_seed_author_cache(checkpoint, source_ids, log_callback=log_callback)
        ensure_chrome_for_cdp(cdp_port_or_url, log_callback=log_callback)
        with sync_playwright() as playwright:
            _, context = connect_existing_chromium(playwright, cdp_port_or_url)
            search_page = context.new_page()
            seed_detail_page = context.new_page()

            authors = collect_seed_authors(
                search_page,
                seed_detail_page,
                list(keywords_list),
                start_dt,
                end_dt,
                limit_time_bool,
                log_callback,
                stop_event=stop_event,
                pause_event=pause_event,
                max_seed_works=max_seed_works,
                max_authors=max_authors,
                max_search_scrolls=max_search_scrolls,
                search_scroll_pause=search_scroll_pause,
                no_new_scroll_limit=no_new_scroll_limit,
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
                log_warn(log_callback, "没有从关键词结果中发现有效作者。")
                return

            profile_page = search_page
            search_page = None
            profile_info_page = seed_detail_page
            works_detail_page = seed_detail_page
            seed_detail_page = None
            log_line(log_callback, "已复用搜索页和详情页进入作者阶段，避免额外打开空白标签页。")

            default_output_path = build_output_path(
                "tiktok",
                f"tiktok_keyword_author_works_{time.strftime('%Y%m%d_%H%M%S')}.xlsx",
                channel="keyword_author_works",
            )
            output_path, writer = open_checkpointed_multi_sheet_writer(
                checkpoint,
                default_output_path,
                {
                    "博主信息": AUTHOR_FIELDS,
                    "博主对应视频": VIDEO_FIELDS,
                },
                log_callback=log_callback,
                autosave_every=5000,
            )
            checkpoint.add_output_path(output_path)

            video_index = 0
            if hasattr(writer, "worksheets") and "博主对应视频" in writer.worksheets:
                video_index = max(0, writer.worksheets["博主对应视频"].max_row - 1)

            author_seeds = list(authors.values())[:max_authors]
            if parallel_windows > 1:
                for opened_page in (search_page, seed_detail_page, profile_info_page, profile_page, works_detail_page):
                    try:
                        if opened_page is not None and not opened_page.is_closed():
                            opened_page.close()
                    except Exception:
                        pass
                search_page = seed_detail_page = profile_info_page = profile_page = works_detail_page = None
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
                )
                writer.save()
                completed_path = output_path
                log_line(log_callback, f"完成，已保存：{output_path}")
                return

            for index, seed in enumerate(author_seeds, 1):
                if should_stop(stop_event):
                    break
                if wait_if_paused(pause_event, stop_event):
                    break
                claimed, claim_status = checkpoint.claim_item(seed.profile_url, positive_count_fields=("works_count",))
                if not claimed:
                    if claim_status == "active":
                        log_line(log_callback, f"[{index}/{min(len(authors), max_authors)}] 双开分流跳过正在处理的作者：{seed.profile_url}")
                    else:
                        log_line(log_callback, f"[{index}/{min(len(authors), max_authors)}] 断点续跑跳过已完成作者：{seed.profile_url}")
                    continue
                log_line(log_callback, f"[{index}/{min(len(authors), max_authors)}] 进入博主主页：{seed.profile_url}")
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

                writer.writerow("博主信息", build_author_sheet_row(seed, profile_record, works, limit_time_bool, start_dt, end_dt))
                for work in works:
                    video_index += 1
                    writer.writerow("博主对应视频", build_video_row(video_index, seed, profile_record, work))
                if profile_record_ok and works_collected_ok and len(works) > 0:
                    checkpoint.mark_completed(
                        seed.profile_url,
                        {"output_path": output_path, "index": index, "works_count": len(works)},
                    )
                else:
                    checkpoint.release_item(seed.profile_url)
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
        for page in (search_page, seed_detail_page, profile_info_page, profile_page, works_detail_page):
            try:
                if page is not None and not page.is_closed():
                    page.close()
            except Exception:
                pass
        finish_callback(completed_path)
