from __future__ import annotations

import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timedelta
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
from src.core.task_checkpoint import open_checkpointed_row_writer, open_task_checkpoint
from src.core.parallel import AtomicCounter, ThreadSafeWriter, normalize_parallel_windows
from src.platforms.x_twitter.page_recovery import wait_for_x_page_recovery
from src.platforms.x_twitter.keyword import (
    MAX_SEARCH_SCROLLS,
    _find_recommendation_boundary_index,
    _try_reload_if_empty,
    build_search_query,
    build_search_url,
    get_tweet_url,
    should_keep_article,
)
from src.platforms.x_twitter.profile_tweets import (
    DEFAULT_MAX_SCROLLS,
    INITIAL_LOAD_DELAY,
    NO_NEW_SCROLL_LIMIT,
    PAGE_LOAD_TIMEOUT,
    SCROLL_DELAY,
    SCROLL_PX,
    XTransientProfileSkipped,
    collect_profile_tweets,
    make_x_transient_skip_state,
    navigate_to_profile,
    normalize_scroll_delay_range,
    use_profile_search_entry,
)
from src.platforms.x_twitter.profiles import (
    extract_author_from_article,
    extract_profile_record,
    normalize_x_url,
)


CSV_FIELDS = [
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
    "作品标题列表",
    "作品链接列表",
    "作品发布时间列表",
]
QUICK_PROFILE_WORK_LIMIT = 50


@dataclass
class AuthorSeed:
    profile_url: str
    author_name: str = ""
    account_id: str = ""
    keywords: list[str] = field(default_factory=list)
    seed_links: list[str] = field(default_factory=list)


def ensure_playwright_available() -> None:
    if sync_playwright is None:
        raise ModuleNotFoundError("playwright is required for X keyword author works scraping")


def parse_date_window(limit_time_str: str, start_date: str, end_date: str) -> tuple[bool, datetime | None, datetime | None]:
    limit_time_bool = limit_time_str == "是"
    if not limit_time_bool:
        return False, None, None
    start_dt = datetime.strptime(start_date.strip(), "%Y-%m-%d")
    end_dt = datetime.strptime(end_date.strip(), "%Y-%m-%d")
    if start_dt > end_dt:
        raise ValueError("开始日期不能晚于结束日期。")
    return True, start_dt, end_dt


def quick_mode_enabled(value: str | None) -> bool:
    return str(value or "是").strip() == "是"


def resolve_profile_work_limit(config: dict | None, quick_mode_value: str | None = "是") -> int:
    configured_limit = int((config or {}).get("max_profile_works_per_author", QUICK_PROFILE_WORK_LIMIT))
    return QUICK_PROFILE_WORK_LIMIT if quick_mode_enabled(quick_mode_value) else configured_limit


def _author_key(profile_url: str, account_id: str = "") -> str:
    account = (account_id or "").strip().lstrip("@").lower()
    if account:
        return account
    normalized = normalize_x_url(profile_url).rstrip("/")
    match = re.search(r"x\.com/([^/?#]+)$", normalized, re.I)
    return match.group(1).lower() if match else normalized.lower()


def merge_seed_author(authors: dict[str, AuthorSeed], keyword: str, tweet_url: str, author: dict[str, str]) -> AuthorSeed | None:
    profile_url = normalize_x_url(author.get("profile_url", ""))
    account_id = (author.get("account_id") or "").strip().lstrip("@")
    if not profile_url and account_id:
        profile_url = f"https://x.com/{account_id}"
    if not profile_url:
        return None

    key = _author_key(profile_url, account_id)
    seed = authors.get(key)
    if seed is None:
        seed = AuthorSeed(
            profile_url=profile_url,
            author_name=author.get("author_name", ""),
            account_id=account_id,
        )
        authors[key] = seed
    if keyword and keyword not in seed.keywords:
        seed.keywords.append(keyword)
    if tweet_url and tweet_url not in seed.seed_links:
        seed.seed_links.append(tweet_url)
    if not seed.author_name and author.get("author_name"):
        seed.author_name = author["author_name"]
    if not seed.account_id and account_id:
        seed.account_id = account_id
    return seed


def _cell_text(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _join_cell(values) -> str:
    cleaned = [_cell_text(value) for value in values if _cell_text(value)]
    return "\n".join(cleaned)


def _parse_work_time(value: str) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        pass
    match = re.search(r"(\d{4})-(\d{1,2})-(\d{1,2})", text)
    if not match:
        return None
    try:
        return datetime(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    except ValueError:
        return None


def _count_works_in_window(
    works: list[dict[str, str]],
    limit_time_bool: bool = False,
    start_dt: datetime | None = None,
    end_dt: datetime | None = None,
) -> int:
    if not limit_time_bool or not start_dt or not end_dt:
        return len(works)
    count = 0
    for work in works:
        publish_dt = _parse_work_time(work.get("published_at", "") or work.get("publishedAt", ""))
        if publish_dt and start_dt.date() <= publish_dt.date() <= end_dt.date():
            count += 1
    return count


def build_author_row(
    seed: AuthorSeed,
    profile_record: dict[str, str],
    works: list[dict[str, str]],
    limit_time_bool: bool = False,
    start_dt: datetime | None = None,
    end_dt: datetime | None = None,
) -> dict[str, str]:
    titles = [work.get("content", "") for work in works]
    links = [work.get("url", "") for work in works]
    publish_times = [work.get("published_at", "") or work.get("publishedAt", "") for work in works]
    in_window_count = _count_works_in_window(works, limit_time_bool, start_dt, end_dt)
    return {
        "搜索词": _join_cell(seed.keywords),
        "命中作品数": str(len(seed.seed_links)),
        "命中作品链接列表": _join_cell(seed.seed_links),
        "作者主页链接": profile_record.get("作者主页链接") or seed.profile_url,
        "作者名称": profile_record.get("作者的名称") or seed.author_name,
        "作者ID": profile_record.get("账号ID") or seed.account_id,
        "粉丝数": profile_record.get("粉丝数", ""),
        "作者简介": profile_record.get("简介", ""),
        "采集作品数": str(len(works)),
        "时间窗口内作品数": str(in_window_count),
        "作品标题列表": _join_cell(titles),
        "作品链接列表": _join_cell(links),
        "作品发布时间列表": _join_cell(publish_times),
    }


def collect_seed_authors(
    page,
    keywords: list[str],
    adv_params: dict,
    log_callback,
    stop_event=None,
    pause_event=None,
    max_seed_works: int = 300,
    max_authors: int = 100,
    max_search_scrolls: int = MAX_SEARCH_SCROLLS,
    page_timeout: int = 40000,
    slice_days: int = 7,
    search_refresh_count: int = 3,
    search_refresh_interval: float = 5.0,
    recovery_config=None,
) -> dict[str, AuthorSeed]:
    authors: dict[str, AuthorSeed] = {}
    seen_tweets: set[str] = set()
    seed_count = 0
    limit_time_bool = adv_params.get("limit_time") == "是"
    if limit_time_bool:
        start_dt = datetime.strptime(adv_params["start_date"], "%Y-%m-%d")
        end_dt = datetime.strptime(adv_params["end_date"], "%Y-%m-%d") + timedelta(days=1)
    else:
        start_dt = datetime.now()
        end_dt = datetime.now()

    for keyword_index, keyword in enumerate(keywords, 1):
        if seed_count >= max_seed_works or should_stop(stop_event):
            break
        if wait_if_paused(pause_event, stop_event):
            break

        current_end_dt = end_dt
        slice_index = 1
        while (limit_time_bool and current_end_dt > start_dt) or (not limit_time_bool and slice_index == 1):
            if seed_count >= max_seed_works or should_stop(stop_event):
                break
            if wait_if_paused(pause_event, stop_event):
                break

            if limit_time_bool:
                current_start_dt = max(start_dt, current_end_dt - timedelta(days=slice_days))
                since = current_start_dt.strftime("%Y-%m-%d")
                until = current_end_dt.strftime("%Y-%m-%d")
            else:
                current_start_dt = start_dt
                since = ""
                until = ""

            final_query = build_search_query(keyword, adv_params, since, until)
            search_url = build_search_url(final_query, adv_params.get("search_tab", "top"))
            log_line(log_callback, f"[{keyword_index}/{len(keywords)}] 搜索作者种子：{final_query}")
            try:
                page.goto(search_url, wait_until="domcontentloaded", timeout=page_timeout)
            except Exception:
                log_warn(log_callback, "  搜索页加载超时，继续读取已加载内容。")
            if not wait_for_x_page_recovery(
                page,
                log_callback=log_callback,
                page_timeout=page_timeout,
                stop_event=stop_event,
                pause_event=pause_event,
                context_label="X 关键词搜索页",
                recovery_config=recovery_config,
            ):
                break
            if interruptible_sleep(4.0, stop_event):
                break
            _try_reload_if_empty(
                page,
                page_timeout,
                search_refresh_count,
                search_refresh_interval,
                lambda message: log_line(log_callback, message),
                stop_event,
                "搜索页",
            )

            no_change_count = 0
            previous_seed_count = seed_count
            for scroll_index in range(max_search_scrolls):
                if seed_count >= max_seed_works or should_stop(stop_event):
                    break
                if wait_if_paused(pause_event, stop_event):
                    break
                if not wait_for_x_page_recovery(
                    page,
                    log_callback=log_callback,
                    page_timeout=page_timeout,
                    stop_event=stop_event,
                    pause_event=pause_event,
                    context_label="X 关键词搜索页",
                    recovery_config=recovery_config,
                ):
                    break

                try:
                    articles = page.locator('article[data-testid="tweet"]').all()
                except Exception:
                    articles = []
                boundary_idx = _find_recommendation_boundary_index(page)
                if boundary_idx >= 0:
                    articles = articles[:boundary_idx]

                for article in articles:
                    if seed_count >= max_seed_works or should_stop(stop_event):
                        break
                    try:
                        if not should_keep_article(article):
                            continue
                        tweet_url = get_tweet_url(article)
                        if not tweet_url or tweet_url in seen_tweets:
                            continue
                        seen_tweets.add(tweet_url)
                        author = extract_author_from_article(article)
                        if len(authors) >= max_authors and _author_key(author.get("profile_url", ""), author.get("account_id", "")) not in authors:
                            continue
                        if merge_seed_author(authors, keyword, tweet_url, author):
                            seed_count += 1
                            log_line(log_callback, f"  发现作者种子 {seed_count}/{max_seed_works}: {tweet_url}")
                    except Exception as exc:
                        log_warn(log_callback, f"  跳过一个搜索结果：{exc}")

                if seed_count == previous_seed_count:
                    no_change_count += 1
                    if no_change_count >= 5:
                        break
                else:
                    previous_seed_count = seed_count
                    no_change_count = 0

                try:
                    page.mouse.wheel(0, 1200)
                except Exception:
                    pass
                if interruptible_sleep(2.5, stop_event):
                    break

            if limit_time_bool:
                current_end_dt = current_start_dt
            slice_index += 1

    return authors


def collect_keyword_author_works_with_parallel_windows(
    author_seeds: list[AuthorSeed],
    *,
    checkpoint,
    output_path: str,
    writer,
    cdp_port_or_url,
    log_callback,
    stop_event=None,
    pause_event=None,
    config=None,
    parallel_windows: int = 1,
    browser_choice: str | None = None,
    page_timeout: int = PAGE_LOAD_TIMEOUT,
    scroll_interval_min: float = SCROLL_DELAY,
    scroll_interval_max: float = SCROLL_DELAY,
    no_new_scroll_limit: int = NO_NEW_SCROLL_LIMIT,
    max_profile_scrolls: int = DEFAULT_MAX_SCROLLS,
    max_profile_works: int = QUICK_PROFILE_WORK_LIMIT,
    scroll_px: int = SCROLL_PX,
    initial_load_delay: float = INITIAL_LOAD_DELAY,
    limit_time_bool: bool = False,
    start_dt: datetime | None = None,
    end_dt: datetime | None = None,
    search_entry_enabled: bool = False,
) -> int:
    if not author_seeds:
        return 0
    worker_count = min(normalize_parallel_windows({"parallel_windows": parallel_windows}), len(author_seeds))
    safe_writer = ThreadSafeWriter(writer)
    completed_counter = AtomicCounter(0)
    work_queue: Queue[tuple[int, AuthorSeed]] = Queue()
    for index, seed in enumerate(author_seeds, 1):
        work_queue.put((index, seed))

    log_line(log_callback, f"Parallel X keyword-author windows enabled: {worker_count}. Runtime locks prevent duplicate profile pages.")

    def _worker(worker_index: int) -> int:
        owner_id = f"{checkpoint.run_id}:x-keyword-author-window-{worker_index}"
        processed = 0
        page = None
        transient_skip_state = make_x_transient_skip_state(config or {})
        with sync_playwright() as worker_playwright:
            _, context = connect_existing_chromium(worker_playwright, cdp_port_or_url, browser=browser_choice)
            page = context.new_page()
            try:
                while not should_stop(stop_event):
                    if wait_if_paused(pause_event, stop_event):
                        break
                    try:
                        index, seed = work_queue.get_nowait()
                    except Empty:
                        break
                    seed_key = normalize_x_url(seed.profile_url).strip().lower()
                    try:
                        claimed, claim_status = checkpoint.claim_item(seed.profile_url, positive_count_fields=("works_count",))
                        if not claimed:
                            log_line(log_callback, f"[W{worker_index} {index}/{len(author_seeds)}] skip {claim_status}: {seed.profile_url}")
                            continue
                        runtime_claimed, _ = checkpoint.claim_runtime_item(
                            seed_key,
                            namespace="x_profile_page",
                            owner_id=owner_id,
                        )
                        if not runtime_claimed:
                            checkpoint.release_item(seed.profile_url)
                            log_line(log_callback, f"[W{worker_index} {index}/{len(author_seeds)}] profile active in another window, skip this run: {seed.profile_url}")
                            continue

                        log_line(log_callback, f"[W{worker_index} {index}/{len(author_seeds)}] author profile: {seed.profile_url}")
                        profile_ready = navigate_to_profile(
                            page,
                            seed.profile_url,
                            log_callback,
                            page_timeout=page_timeout,
                            stop_event=stop_event,
                            pause_event=pause_event,
                            initial_delay=initial_load_delay,
                            recovery_config=config,
                            use_search_entry=search_entry_enabled,
                        )
                        extracted_profile = (
                            extract_profile_record(
                                page,
                                seed.profile_url,
                                log_callback,
                                page_timeout=page_timeout,
                                stop_event=stop_event,
                                needs_navigation=False,
                                recovery_config=config,
                            )
                            if profile_ready
                            else None
                        )
                        profile_record_ok = bool(extracted_profile)
                        profile_record = extracted_profile or {}

                        works_collected_ok = False
                        try:
                            if not profile_ready:
                                raise RuntimeError("profile page not ready")
                            works = collect_profile_tweets(
                                page,
                                None,
                                seed.profile_url,
                                max_profile_scrolls,
                                False,
                                None,
                                None,
                                False,
                                0,
                                log_callback,
                                stop_event=stop_event,
                                writer=None,
                                page_timeout=page_timeout,
                                scroll_delay_min=scroll_interval_min,
                                scroll_delay_max=scroll_interval_max,
                                no_new_scroll_limit=no_new_scroll_limit,
                                pause_event=pause_event,
                                max_collect=max_profile_works,
                                scroll_px=scroll_px,
                                initial_load_delay=initial_load_delay,
                                page_already_loaded=True,
                                include_reposts=False,
                                recovery_config=config,
                                transient_skip_state=transient_skip_state,
                                transient_retry=False,
                            )
                            works_collected_ok = True
                        except XTransientProfileSkipped as exc:
                            checkpoint.release_item(seed.profile_url)
                            log_warn(log_callback, f"  [W{worker_index}] transient X skip: {seed.profile_url}; {exc}")
                            continue
                        except Exception as exc:
                            log_warn(log_callback, f"  [W{worker_index}] author works failed: {exc}")
                            works = []

                        safe_writer.writerow(build_author_row(seed, profile_record, works, limit_time_bool, start_dt, end_dt))
                        if profile_ready and profile_record_ok and works_collected_ok:
                            checkpoint.mark_completed(
                                seed.profile_url,
                                {
                                    "output_path": output_path,
                                    "index": index,
                                    "works_count": len(works),
                                    "worker": worker_index,
                                },
                            )
                            completed_counter.next()
                        else:
                            checkpoint.release_item(seed.profile_url)
                            log_warn(log_callback, f"  [W{worker_index}] incomplete; checkpoint not completed.")
                        processed += 1
                        log_line(log_callback, f"  [W{worker_index}] wrote author {seed.account_id or seed.profile_url}, works={len(works)}")
                    except Exception as exc:
                        checkpoint.release_item(seed.profile_url)
                        log_warn(log_callback, f"  [W{worker_index}] failed: {seed.profile_url}; {exc}")
                    finally:
                        try:
                            checkpoint.release_runtime_item(seed_key, namespace="x_profile_page", owner_id=owner_id)
                        except Exception:
                            pass
                        work_queue.task_done()
            finally:
                try:
                    if page is not None and not page.is_closed():
                        page.close()
                except Exception:
                    pass
        return processed

    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        futures = [executor.submit(_worker, worker_index) for worker_index in range(1, worker_count + 1)]
        for future in as_completed(futures):
            future.result()

    safe_writer.save()
    return completed_counter.value


def run_x_keyword_author_works_spider(
    keywords_list,
    adv_params,
    cdp_port_or_url,
    log_callback,
    finish_callback,
    stop_event=None,
    config=None,
    pause_event=None,
):
    ensure_playwright_available()
    if config is None:
        config = {}

    max_seed_works = int(config.get("max_seed_works", 300))
    max_authors = int(config.get("max_authors", 100))
    quick_mode_value = adv_params.get("quick_mode", config.get("quick_mode", "是"))
    max_profile_works = resolve_profile_work_limit(config, quick_mode_value)
    max_search_scrolls = int(config.get("max_search_scrolls", config.get("max_scrolls", MAX_SEARCH_SCROLLS)))
    max_profile_scrolls = int(config.get("max_profile_scrolls", DEFAULT_MAX_SCROLLS))
    page_timeout = int(config.get("page_load_timeout", PAGE_LOAD_TIMEOUT))
    scroll_interval_min, scroll_interval_max = normalize_scroll_delay_range(
        config,
        fallback=config.get("scroll_interval", SCROLL_DELAY),
    )
    no_new_scroll_limit = int(config.get("no_new_scroll_limit", NO_NEW_SCROLL_LIMIT))
    slice_days = int(config.get("slice_days", 7))
    scroll_px = int(config.get("scroll_px", SCROLL_PX))
    initial_load_delay = float(config.get("initial_load_delay", INITIAL_LOAD_DELAY))
    search_refresh_count = int(config.get("search_refresh_count", 3))
    search_refresh_interval = float(config.get("search_refresh_interval", 5.0))
    browser_choice = config.get("browser")
    search_entry_enabled = use_profile_search_entry(config)
    parallel_windows = normalize_parallel_windows(config)

    completed_path = None
    search_page = profile_page = works_page = None
    playwright = None
    try:
        limit_time_bool, start_dt, end_dt = parse_date_window(
            adv_params.get("limit_time", "否"),
            adv_params.get("start_date", ""),
            adv_params.get("end_date", ""),
        )
        if quick_mode_enabled(quick_mode_value):
            log_line(log_callback, f"快速模式已开启：作者主页作品最多取最新 {QUICK_PROFILE_WORK_LIMIT} 条，不足则采完即停。")
        checkpoint = open_task_checkpoint(
            "x_keyword_author_works",
            {
                "keywords": list(keywords_list),
                "limit_time": limit_time_bool,
                "start_date": adv_params.get("start_date", "") if limit_time_bool else "",
                "end_date": adv_params.get("end_date", "") if limit_time_bool else "",
                "lang": adv_params.get("lang", "any"),
                "quick_mode": quick_mode_value,
                "max_profile_works": max_profile_works,
            },
            log_callback=log_callback,
        )
        ensure_chrome_for_cdp(cdp_port_or_url, log_callback=log_callback, browser=browser_choice)
        playwright = sync_playwright().start()
        browser, context = connect_existing_chromium(playwright, cdp_port_or_url, browser=browser_choice)
        search_page = context.new_page()
        profile_page = context.new_page()
        works_page = context.new_page()

        authors = collect_seed_authors(
            search_page,
            list(keywords_list),
            adv_params,
            log_callback,
            stop_event=stop_event,
            pause_event=pause_event,
            max_seed_works=max_seed_works,
            max_authors=max_authors,
            max_search_scrolls=max_search_scrolls,
            page_timeout=page_timeout,
            slice_days=slice_days,
            search_refresh_count=search_refresh_count,
            search_refresh_interval=search_refresh_interval,
            recovery_config=config,
        )
        if not authors:
            log_warn(log_callback, "没有从关键词结果中发现有效作者。")
            return

        default_output_path = build_output_path(
            "x",
            f"x_keyword_author_works_{time.strftime('%Y%m%d_%H%M%S')}.xlsx",
            channel="keyword_author_works",
        )
        output_path, writer = open_checkpointed_row_writer(
            checkpoint,
            default_output_path,
            CSV_FIELDS,
            log_callback=log_callback,
            autosave_every=5000,
        )
        checkpoint.add_output_path(output_path)

        author_seeds = list(authors.values())[:max_authors]
        if parallel_windows > 1:
            for opened_page in (search_page, profile_page, works_page):
                try:
                    if opened_page is not None and not opened_page.is_closed():
                        opened_page.close()
                except Exception:
                    pass
            search_page = profile_page = works_page = None
            collect_keyword_author_works_with_parallel_windows(
                author_seeds,
                checkpoint=checkpoint,
                output_path=output_path,
                writer=writer,
                cdp_port_or_url=cdp_port_or_url,
                log_callback=log_callback,
                stop_event=stop_event,
                pause_event=pause_event,
                config=config,
                parallel_windows=parallel_windows,
                browser_choice=browser_choice,
                page_timeout=page_timeout,
                scroll_interval_min=scroll_interval_min,
                scroll_interval_max=scroll_interval_max,
                no_new_scroll_limit=no_new_scroll_limit,
                max_profile_scrolls=max_profile_scrolls,
                max_profile_works=max_profile_works,
                scroll_px=scroll_px,
                initial_load_delay=initial_load_delay,
                limit_time_bool=limit_time_bool,
                start_dt=start_dt,
                end_dt=end_dt,
                search_entry_enabled=search_entry_enabled,
            )
            writer.save()
            completed_path = output_path
            log_line(log_callback, f"完成，已保存：{output_path}")
            return

        total_authors = len(author_seeds)
        pending_authors = [{"seed": seed, "transient_retry": False} for seed in author_seeds]
        deferred_authors: list[AuthorSeed] = []
        final_transient_skips: set[str] = set()
        transient_skip_state = make_x_transient_skip_state(config)
        index = 0

        while pending_authors:
            if should_stop(stop_event):
                break
            if wait_if_paused(pause_event, stop_event):
                break
            item = pending_authors.pop(0)
            seed = item["seed"]
            transient_retry = bool(item.get("transient_retry"))
            seed_key = normalize_x_url(seed.profile_url).strip().lower()
            if seed_key in final_transient_skips:
                continue
            if not transient_retry:
                index += 1
            claimed, claim_status = checkpoint.claim_item(seed.profile_url, positive_count_fields=("works_count",))
            if not claimed:
                if claim_status == "active":
                    log_line(log_callback, f"[{index}/{total_authors}] 双开分流跳过正在处理的作者：{seed.profile_url}")
                else:
                    log_line(log_callback, f"[{index}/{total_authors}] 断点续跑跳过已完成作者：{seed.profile_url}")
                continue
            prefix = "回退补采" if transient_retry else "进入"
            log_line(log_callback, f"[{index}/{total_authors}] {prefix}作者主页：{seed.profile_url}")
            profile_ready = navigate_to_profile(
                profile_page,
                seed.profile_url,
                log_callback,
                page_timeout=page_timeout,
                stop_event=stop_event,
                pause_event=pause_event,
                initial_delay=initial_load_delay,
                recovery_config=config,
                use_search_entry=search_entry_enabled,
            )
            extracted_profile = (
                extract_profile_record(
                    profile_page,
                    seed.profile_url,
                    log_callback,
                    page_timeout=page_timeout,
                    stop_event=stop_event,
                    needs_navigation=False,
                    recovery_config=config,
                )
                if profile_ready
                else None
            )
            profile_record_ok = bool(extracted_profile)
            profile_record = extracted_profile or {
                "作者主页链接": seed.profile_url,
                "作者的名称": seed.author_name,
                "账号ID": seed.account_id,
                "粉丝数": "",
                "简介": "",
            }
            works_collected_ok = False
            try:
                if not navigate_to_profile(
                    works_page,
                    profile_record.get("作者主页链接") or seed.profile_url,
                    log_callback,
                    page_timeout=page_timeout,
                    stop_event=stop_event,
                    pause_event=pause_event,
                    initial_delay=initial_load_delay,
                    recovery_config=config,
                    use_search_entry=search_entry_enabled,
                ):
                    raise RuntimeError("未能进入作者主页")
                works = collect_profile_tweets(
                    works_page,
                    None,
                    seed.profile_url,
                    max_profile_scrolls,
                    False,
                    None,
                    None,
                    False,
                    0,
                    log_callback,
                    stop_event=stop_event,
                    writer=None,
                    page_timeout=page_timeout,
                    scroll_delay_min=scroll_interval_min,
                    scroll_delay_max=scroll_interval_max,
                    no_new_scroll_limit=no_new_scroll_limit,
                    pause_event=pause_event,
                    max_collect=max_profile_works,
                    scroll_px=scroll_px,
                    initial_load_delay=initial_load_delay,
                    page_already_loaded=True,
                    include_reposts=False,
                    recovery_config=config,
                    transient_skip_state=transient_skip_state,
                    transient_retry=transient_retry,
                )
                works_collected_ok = True
            except XTransientProfileSkipped as exc:
                works = []
                checkpoint.release_item(seed.profile_url)
                if exc.retry_after_success and not transient_retry:
                    if seed_key not in {normalize_x_url(item.profile_url).strip().lower() for item in deferred_authors}:
                        deferred_authors.append(seed)
                    log_warn(log_callback, f"  已临时跳过，等待后续作者成功后回退补采一次：{seed.profile_url}")
                else:
                    final_transient_skips.add(seed_key)
                    log_warn(log_callback, f"  回退补采仍触发 X 风控，本轮不再回退：{seed.profile_url}")
                continue
            except Exception as exc:
                log_warn(log_callback, f"  作者作品采集失败：{exc}")
                works = []
            writer.writerow(build_author_row(seed, profile_record, works, limit_time_bool, start_dt, end_dt))
            if profile_ready and profile_record_ok and works_collected_ok:
                checkpoint.mark_completed(
                    seed.profile_url,
                    {"output_path": output_path, "index": index, "works_count": len(works)},
                )
                while deferred_authors:
                    retry_seed = deferred_authors.pop(0)
                    retry_key = normalize_x_url(retry_seed.profile_url).strip().lower()
                    if retry_key in final_transient_skips:
                        continue
                    pending_authors.insert(0, {"seed": retry_seed, "transient_retry": True})
                    log_line(log_callback, f"  本轮已有作者采集成功，回退补采此前跳过的作者：{retry_seed.profile_url}")
                    break
            else:
                checkpoint.release_item(seed.profile_url)
                log_warn(log_callback, "  本轮未完整采集成功，未写入断点完成标记，下次会继续重试。")
            log_line(log_callback, f"  写入作者：{profile_record.get('账号ID') or seed.account_id or seed.profile_url}，作品 {len(works)} 条。")

        writer.save()
        completed_path = output_path
        log_line(log_callback, f"完成，已保存：{output_path}")
    except Exception as exc:
        log_error(log_callback, f"运行失败：{exc}")
    finally:
        for page in (search_page, profile_page, works_page):
            try:
                if page is not None and not page.is_closed():
                    page.close()
            except Exception:
                pass
        finish_callback(completed_path)
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
