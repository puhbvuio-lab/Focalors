from __future__ import annotations

import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from queue import Empty, Queue

try:
    from playwright.sync_api import sync_playwright
except ModuleNotFoundError:  # pragma: no cover
    sync_playwright = None

from src.core import (
    DEFAULT_X_CDP_URL,
    build_output_path,
    connect_existing_chromium,
    log_error,
    log_line,
    log_warn,
    should_stop,
    wait_if_paused,
)
from src.core.task_checkpoint import open_checkpointed_multi_sheet_writer, open_task_checkpoint
from src.core.parallel import AtomicCounter, ThreadSafeWriter, normalize_parallel_windows
from src.platforms.x_twitter.profile_tweets import (
    DEFAULT_MAX_SCROLLS,
    DEFAULT_PROFILE_TWEET_LIMIT,
    INITIAL_LOAD_DELAY,
    NO_NEW_SCROLL_LIMIT,
    PAGE_LOAD_TIMEOUT,
    SCROLL_DELAY,
    SCROLL_PX,
    XTransientProfileSkipped,
    collect_profile_tweets,
    extract_profile_username,
    make_x_transient_skip_state,
    navigate_to_profile,
    normalize_scroll_delay_range,
    parse_profile_urls,
    row_from_tweet,
    use_profile_search_entry,
)
from src.platforms.x_twitter.profiles import extract_profile_record, normalize_x_url


PROFILE_FIELDS = ["作者主页链接", "作者名称", "作者ID", "粉丝量", "作者简介"]
TWEET_FIELDS = [
    "序号",
    "编号",
    "推文链接",
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
]


def _parse_date_range(start_date: str, end_date: str) -> tuple[datetime, datetime]:
    start_dt = datetime.strptime(start_date.strip(), "%Y-%m-%d")
    end_dt = datetime.strptime(end_date.strip(), "%Y-%m-%d")
    if start_dt > end_dt:
        raise ValueError("开始日期不能晚于结束日期。")
    return start_dt, end_dt


def _cell_text(value: str, limit: int | None = None) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if limit is not None and len(text) > limit:
        return text[:limit].rstrip()
    return text


def _fallback_profile_record(profile_url: str) -> dict[str, str]:
    normalized_url = normalize_x_url(profile_url)
    username = extract_profile_username(normalized_url)
    return {
        "作者主页链接": normalized_url,
        "作者的名称": "",
        "账号ID": username,
        "粉丝数": "",
        "简介": "",
    }


def build_profile_row(profile_record: dict[str, str]) -> dict[str, str]:
    return {
        "作者主页链接": profile_record.get("作者主页链接", ""),
        "作者名称": profile_record.get("作者的名称", ""),
        "作者ID": profile_record.get("账号ID", ""),
        "粉丝量": profile_record.get("粉丝数", ""),
        "作者简介": profile_record.get("简介", ""),
    }


def build_tweet_row(index: int, profile_record: dict[str, str], tweet: dict[str, str]) -> dict[str, str]:
    base = row_from_tweet(index, tweet)
    tweet_url = base.get("帖子链接", "")
    content = _cell_text(base.get("帖子内容", ""))
    return {
        "序号": base.get("序号", str(index)),
        "编号": base.get("帖子ID", ""),
        "推文链接": tweet_url,
        "作品链接": tweet_url,
        "博主主页链接": profile_record.get("作者主页链接", ""),
        "作者主页链接": profile_record.get("作者主页链接", ""),
        "标题": _cell_text(content, limit=120),
        "作品内容": f"{content}[推文]" if content else "",
        "频道名称": profile_record.get("作者的名称", ""),
        "发布日期": base.get("发布时间", ""),
        "作品类型": "推文",
        "直播状态": "非直播",
        "关联作品标题": "",
        "关联作品链接": "",
        "作品时长": "",
        "作品简介": content,
        "浏览量": base.get("浏览量", ""),
        "播放量": "",
        "点赞数": base.get("点赞量", ""),
        "评论数": base.get("评论数", ""),
    }


def collect_profile_bundle_with_parallel_windows(
    profile_urls: list[str],
    *,
    checkpoint,
    output_path: str,
    writer,
    cdp_port_or_url: str,
    log_callback,
    stop_event=None,
    pause_event=None,
    config=None,
    parallel_windows: int = 1,
    browser_choice: str | None = None,
    page_timeout: int = PAGE_LOAD_TIMEOUT,
    scroll_delay_min: float = SCROLL_DELAY,
    scroll_delay_max: float = SCROLL_DELAY,
    no_new_scroll_limit: int = NO_NEW_SCROLL_LIMIT,
    max_scrolls: int = DEFAULT_MAX_SCROLLS,
    max_tweets_per_author: int = DEFAULT_PROFILE_TWEET_LIMIT,
    scroll_px: int = SCROLL_PX,
    initial_load_delay: float = INITIAL_LOAD_DELAY,
    date_window_size: int = 20,
    include_reposts: bool = False,
    search_entry_enabled: bool = False,
    profile_sheet_name: str = "",
    tweet_sheet_name: str = "",
) -> int:
    if not profile_urls:
        return 0
    if not profile_sheet_name or not tweet_sheet_name:
        raise ValueError("profile_sheet_name and tweet_sheet_name are required")

    worker_count = min(normalize_parallel_windows({"parallel_windows": parallel_windows}), len(profile_urls))
    safe_writer = ThreadSafeWriter(writer)
    raw_writer = safe_writer.raw
    start_tweet_index = 0
    if hasattr(raw_writer, "worksheets") and tweet_sheet_name in raw_writer.worksheets:
        start_tweet_index = max(0, raw_writer.worksheets[tweet_sheet_name].max_row - 1)
    tweet_counter = AtomicCounter(start_tweet_index)
    completed_counter = AtomicCounter(0)

    work_queue: Queue[tuple[int, str]] = Queue()
    for index, profile_url in enumerate(profile_urls, 1):
        work_queue.put((index, profile_url))

    log_line(log_callback, f"Parallel X profile bundle windows enabled: {worker_count}. Runtime locks prevent duplicate profile pages.")

    def _worker(worker_index: int) -> int:
        owner_id = f"{checkpoint.run_id}:x-profile-bundle-window-{worker_index}"
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
                        profile_index, raw_profile_url = work_queue.get_nowait()
                    except Empty:
                        break

                    profile_url = normalize_x_url(raw_profile_url)
                    profile_key = profile_url.strip().lower()
                    try:
                        claimed, claim_status = checkpoint.claim_item(profile_url, positive_count_fields=("tweet_count",))
                        if not claimed:
                            log_line(log_callback, f"[W{worker_index} {profile_index}/{len(profile_urls)}] skip {claim_status}: {profile_url}")
                            continue

                        runtime_claimed, _ = checkpoint.claim_runtime_item(
                            profile_key,
                            namespace="x_profile_page",
                            owner_id=owner_id,
                        )
                        if not runtime_claimed:
                            checkpoint.release_item(profile_url)
                            log_line(log_callback, f"[W{worker_index} {profile_index}/{len(profile_urls)}] profile active in another window, skip this run: {profile_url}")
                            continue

                        log_line(log_callback, f"[W{worker_index} {profile_index}/{len(profile_urls)}] profile bundle: {profile_url}")
                        profile_ready = navigate_to_profile(
                            page,
                            profile_url,
                            log_callback,
                            page_timeout=page_timeout,
                            stop_event=stop_event,
                            pause_event=pause_event,
                            initial_delay=initial_load_delay,
                            recovery_config=config,
                            use_search_entry=search_entry_enabled,
                        )
                        if profile_ready:
                            extracted_profile = extract_profile_record(
                                page,
                                profile_url,
                                log_callback,
                                page_timeout=page_timeout,
                                stop_event=stop_event,
                                needs_navigation=False,
                                recovery_config=config,
                            )
                            profile_record_ok = bool(extracted_profile)
                            profile_record = extracted_profile or _fallback_profile_record(profile_url)
                        else:
                            profile_record_ok = False
                            profile_record = _fallback_profile_record(profile_url)

                        tweets_collected_ok = False
                        try:
                            if not profile_ready:
                                raise RuntimeError("profile page not ready")
                            tweets = collect_profile_tweets(
                                page,
                                None,
                                profile_url,
                                max_scrolls,
                                False,
                                None,
                                None,
                                False,
                                0,
                                log_callback,
                                stop_event=stop_event,
                                writer=None,
                                page_timeout=page_timeout,
                                scroll_delay_min=scroll_delay_min,
                                scroll_delay_max=scroll_delay_max,
                                no_new_scroll_limit=no_new_scroll_limit,
                                pause_event=pause_event,
                                max_collect=max_tweets_per_author,
                                scroll_px=scroll_px,
                                initial_load_delay=initial_load_delay,
                                page_already_loaded=True,
                                date_window_size=date_window_size,
                                include_reposts=include_reposts,
                                recovery_config=config,
                                transient_skip_state=transient_skip_state,
                                transient_retry=False,
                            )
                            tweets_collected_ok = True
                        except XTransientProfileSkipped as exc:
                            checkpoint.release_item(profile_url)
                            log_warn(log_callback, f"  [W{worker_index}] transient X skip: {profile_url}; {exc}")
                            continue
                        except Exception as exc:
                            log_warn(log_callback, f"  [W{worker_index}] tweets failed: {exc}")
                            tweets = []

                        safe_writer.writerow(profile_sheet_name, build_profile_row(profile_record))
                        for tweet in tweets:
                            safe_writer.writerow(tweet_sheet_name, build_tweet_row(tweet_counter.next(), profile_record, tweet))
                        if profile_ready and profile_record_ok and tweets_collected_ok:
                            checkpoint.mark_completed(
                                profile_url,
                                {
                                    "output_path": output_path,
                                    "profile_index": profile_index,
                                    "tweet_count": len(tweets),
                                    "worker": worker_index,
                                },
                            )
                            completed_counter.next()
                        else:
                            checkpoint.release_item(profile_url)
                            log_warn(log_callback, f"  [W{worker_index}] incomplete; checkpoint not completed.")
                        processed += 1
                        log_line(log_callback, f"  [W{worker_index}] wrote profile bundle {profile_url}, tweets={len(tweets)}")
                    except Exception as exc:
                        checkpoint.release_item(profile_url)
                        log_warn(log_callback, f"  [W{worker_index}] failed: {profile_url}; {exc}")
                    finally:
                        try:
                            checkpoint.release_runtime_item(profile_key, namespace="x_profile_page", owner_id=owner_id)
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


def run_x_profile_bundle_spider(
    profile_urls_text: str,
    limit_time_str: str,
    start_date: str,
    end_date: str,
    cdp_port_or_url: str = DEFAULT_X_CDP_URL,
    log_callback=None,
    finish_callback=None,
    stop_event=None,
    config=None,
    pause_event=None,
):
    if config is None:
        config = {}
    completed_path = None
    page = None

    playwright = None
    try:
        if sync_playwright is None:
            log_error(log_callback, "缺少依赖：playwright。请先安装 requirements.txt 中的依赖。")
            return

        profile_urls = parse_profile_urls(profile_urls_text)
        if not profile_urls:
            log_warn(log_callback, "未读取到有效的 X 博主主页链接。")
            return

        page_timeout = int(config.get("page_load_timeout", PAGE_LOAD_TIMEOUT))
        scroll_delay_min, scroll_delay_max = normalize_scroll_delay_range(
            config,
            fallback=config.get("scroll_interval", SCROLL_DELAY),
        )
        no_new_scroll_limit = int(config.get("no_new_scroll_limit", NO_NEW_SCROLL_LIMIT))
        max_scrolls = int(config.get("max_scrolls", DEFAULT_MAX_SCROLLS))
        max_tweets_per_author = max(1, int(config.get("max_tweets_per_author", DEFAULT_PROFILE_TWEET_LIMIT)))
        scroll_px = int(config.get("scroll_px", SCROLL_PX))
        initial_load_delay = float(config.get("initial_load_delay", INITIAL_LOAD_DELAY))
        date_window_size = int(config.get("date_window_size", 20))
        include_reposts = str(config.get("include_reposts", "否")).strip() == "是"
        browser_choice = config.get("browser")
        search_entry_enabled = use_profile_search_entry(config)
        parallel_windows = normalize_parallel_windows(config)

        requested_time_limit = limit_time_str == "是"
        limit_time_bool = False
        start_dt = end_dt = None
        if requested_time_limit:
            log_line(log_callback, "主页推文采集采用最新数量优先，已忽略时间窗口过滤。")
        checkpoint = open_task_checkpoint(
            "x_profile_bundle",
            {
                "profile_urls": profile_urls,
                "max_tweets_per_author": max_tweets_per_author,
                "max_scrolls": max_scrolls,
                "include_reposts": include_reposts,
            },
            log_callback=log_callback,
            merge_on_keys=("profile_urls",),
            merge_keep_keys=("include_reposts",),
        )

        default_output_path = build_output_path(
            "x",
            f"x_profile_bundle_{time.strftime('%Y%m%d_%H%M%S')}.xlsx",
            channel="profile_bundle",
        )
        output_path, writer = open_checkpointed_multi_sheet_writer(
            checkpoint,
            default_output_path,
            {
                "博主信息": PROFILE_FIELDS,
                "博主对应推文": TWEET_FIELDS,
            },
            log_callback=log_callback,
            autosave_every=5000,
        )
        checkpoint.add_output_path(output_path)

        if parallel_windows > 1:
            sheet_names = list(getattr(writer, "sheets_fields", {}).keys())
            collect_profile_bundle_with_parallel_windows(
                profile_urls,
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
                scroll_delay_min=scroll_delay_min,
                scroll_delay_max=scroll_delay_max,
                no_new_scroll_limit=no_new_scroll_limit,
                max_scrolls=max_scrolls,
                max_tweets_per_author=max_tweets_per_author,
                scroll_px=scroll_px,
                initial_load_delay=initial_load_delay,
                date_window_size=date_window_size,
                include_reposts=include_reposts,
                search_entry_enabled=search_entry_enabled,
                profile_sheet_name=sheet_names[0],
                tweet_sheet_name=sheet_names[1],
            )
            completed_path = output_path
            writer.save()
            log_line(log_callback, f"完成，已保存：{output_path}")
            return

        playwright = sync_playwright().start()
        log_line(log_callback, "正在连接本地浏览器...")
        try:
            browser, context = connect_existing_chromium(playwright, cdp_port_or_url, browser=browser_choice)
        except Exception as exc:
            log_error(log_callback, f"无法连接浏览器：{exc}")
            log_error(log_callback, "连接失败：请确认浏览器已自动打开并已登录 X/Twitter。")
            return

        page = context.new_page()
        tweet_index = 0
        total_profiles = len(profile_urls)
        pending_profiles = [{"profile_url": url, "transient_retry": False} for url in profile_urls]
        deferred_profiles: list[str] = []
        final_transient_skips: set[str] = set()
        profile_rows_written: set[str] = set()
        transient_skip_state = make_x_transient_skip_state(config)

        profile_index = 0
        while pending_profiles:
            if should_stop(stop_event):
                log_line(log_callback, "任务已停止。")
                break
            if wait_if_paused(pause_event, stop_event):
                break

            item = pending_profiles.pop(0)
            profile_url = item["profile_url"]
            transient_retry = bool(item.get("transient_retry"))
            profile_url = normalize_x_url(profile_url)
            profile_key = profile_url.strip().lower()
            if profile_key in final_transient_skips:
                continue
            if not transient_retry:
                profile_index += 1
            claimed, claim_status = checkpoint.claim_item(profile_url, positive_count_fields=("tweet_count",))
            if not claimed:
                if claim_status == "active":
                    log_line(log_callback, f"[{profile_index}/{total_profiles}] 双开分流跳过正在处理的博主：{profile_url}")
                else:
                    log_line(log_callback, f"[{profile_index}/{total_profiles}] 断点续跑跳过已完成博主：{profile_url}")
                continue
            prefix = "回退补采" if transient_retry else "采集"
            log_line(log_callback, f"[{profile_index}/{total_profiles}] {prefix}博主信息与推文：{profile_url}")

            profile_ready = False
            profile_record_ok = False
            try:
                profile_ready = navigate_to_profile(
                    page,
                    profile_url,
                    log_callback,
                    page_timeout=page_timeout,
                    stop_event=stop_event,
                    pause_event=pause_event,
                    initial_delay=initial_load_delay,
                    recovery_config=config,
                    use_search_entry=search_entry_enabled,
                )
                if not profile_ready:
                    log_warn(log_callback, f"  未能进入作者主页，使用链接兜底：{profile_url}")
                    profile_record = _fallback_profile_record(profile_url)
                else:
                    extracted_profile = extract_profile_record(
                        page,
                        profile_url,
                        log_callback,
                        page_timeout=page_timeout,
                        stop_event=stop_event,
                        needs_navigation=False,
                        recovery_config=config,
                    )
                    profile_record_ok = bool(extracted_profile)
                    profile_record = extracted_profile or _fallback_profile_record(profile_url)
            except Exception as exc:
                log_warn(log_callback, f"  博主信息采集失败，使用链接兜底：{exc}")
                profile_record = _fallback_profile_record(profile_url)

            if profile_key not in profile_rows_written:
                writer.writerow("博主信息", build_profile_row(profile_record))
                profile_rows_written.add(profile_key)

            tweets_collected_ok = False
            try:
                if not profile_ready:
                    raise RuntimeError("未能进入作者主页")
                tweets = collect_profile_tweets(
                    page,
                    None,
                    profile_record.get("作者主页链接") or profile_url,
                    max_scrolls,
                    limit_time_bool,
                    start_dt,
                    end_dt,
                    False,
                    0,
                    log_callback,
                    stop_event=stop_event,
                    writer=None,
                    page_timeout=page_timeout,
                    scroll_delay_min=scroll_delay_min,
                    scroll_delay_max=scroll_delay_max,
                    no_new_scroll_limit=no_new_scroll_limit,
                    pause_event=pause_event,
                    max_collect=max_tweets_per_author,
                    scroll_px=scroll_px,
                    initial_load_delay=initial_load_delay,
                    page_already_loaded=True,
                    date_window_size=date_window_size,
                    include_reposts=include_reposts,
                    recovery_config=config,
                    transient_skip_state=transient_skip_state,
                    transient_retry=transient_retry,
                )
                tweets_collected_ok = True
            except XTransientProfileSkipped as exc:
                tweets = []
                checkpoint.release_item(profile_url)
                if exc.retry_after_success and not transient_retry:
                    if profile_key not in {normalize_x_url(url).strip().lower() for url in deferred_profiles}:
                        deferred_profiles.append(profile_url)
                    log_warn(log_callback, f"  已临时跳过，等待后续作者成功后回退补采一次：{profile_url}")
                else:
                    final_transient_skips.add(profile_key)
                    log_warn(log_callback, f"  回退补采仍触发 X 风控，本轮不再回退：{profile_url}")
                continue
            except Exception as exc:
                log_warn(log_callback, f"  推文采集失败：{exc}")
                tweets = []

            for tweet in tweets:
                tweet_index += 1
                writer.writerow("博主对应推文", build_tweet_row(tweet_index, profile_record, tweet))
            log_line(
                log_callback,
                f"  完成：{profile_record.get('账号ID') or profile_url}，写入 {len(tweets)} 条推文。",
            )
            if profile_ready and profile_record_ok and tweets_collected_ok:
                checkpoint.mark_completed(
                    profile_url,
                    {"output_path": output_path, "profile_index": profile_index, "tweet_count": len(tweets)},
                )
                while deferred_profiles:
                    retry_profile_url = deferred_profiles.pop(0)
                    retry_key = normalize_x_url(retry_profile_url).strip().lower()
                    if retry_key in final_transient_skips:
                        continue
                    pending_profiles.insert(0, {"profile_url": retry_profile_url, "transient_retry": True})
                    log_line(log_callback, f"  本轮已有作者采集成功，回退补采此前跳过的博主：{retry_profile_url}")
                    break
            else:
                checkpoint.release_item(profile_url)
                log_warn(log_callback, "  本轮未完整采集成功，未写入断点完成标记，下次会继续重试。")

        completed_path = output_path
        writer.save()
        log_line(log_callback, f"完成，已保存：{output_path}")
    except Exception as exc:
        log_error(log_callback, f"运行失败：{exc}")
    finally:
        try:
            if page is not None and not page.is_closed():
                page.close()
        except Exception:
            pass
        if finish_callback:
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
