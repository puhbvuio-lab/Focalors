from __future__ import annotations

import json
import re
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

try:
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
    from playwright.sync_api import sync_playwright
except ModuleNotFoundError:
    PlaywrightTimeoutError = TimeoutError
    sync_playwright = None

from src.core import (
    XlsxRowWriter,
    build_output_path,
    connect_existing_chromium,
    interruptible_sleep,
    sanitize_csv_row,
    should_stop,
    wait_if_paused,
)
from src.platforms.tiktok.profile_videos import (
    normalize_profile_url,
    parse_profile_urls,
    trigger_profile_lazy_load,
    log_error,
    log_line,
    log_warn,
)

CSV_FIELDS = ["序号", "视频链接", "播放量"]
PAGE_LOAD_TIMEOUT = 45000
SCROLL_INTERVAL_SECONDS = 2.5
NO_NEW_SCROLL_LIMIT = 10


def run_tiktok_profile_play_counts_spider(
    txt_path: str,
    cdp_port_or_url: str,
    max_scrolls: int,
    log_callback,
    finish_callback,
    stop_event=None,
    pause_event=None,
    config=None,
):
    """
    抓取 TikTok 博主主页视频播放量的并发爬虫入口。
    不需要请求单个视频的详情页，直接通过网络请求拦截 /api/post/item_list 接口的数据，
    利用 ThreadPoolExecutor 并发提取主页滚动展示的所有视频链接与播放量。
    """
    if config is None:
        config = {}
    page_load_timeout = int(config.get("page_load_timeout", PAGE_LOAD_TIMEOUT))
    scroll_interval = float(config.get("scroll_interval", SCROLL_INTERVAL_SECONDS))
    no_new_scroll_limit = int(config.get("no_new_scroll_limit", NO_NEW_SCROLL_LIMIT))
    max_scrolls = int(config.get("max_scrolls", max_scrolls))
    max_parallel_tabs = int(config.get("max_parallel_tabs", 3))

    output_path = None
    completed_path = None
    try:
        if sync_playwright is None:
            log_error(log_callback, "缺少依赖：playwright。请先安装 requirements.txt 中的依赖。")
            return

        profile_urls = parse_profile_urls(txt_path)
        if not profile_urls:
            log_warn(log_callback, "TXT 中没有找到有效的 TikTok 博主主页链接。")
            return

        output_path = build_output_path("tiktok", f"tiktok_profile_play_counts_{time.strftime('%Y%m%d_%H%M%S')}.xlsx", channel="profile_play_counts")
        writer = XlsxRowWriter(output_path, CSV_FIELDS)
        writer_lock = threading.Lock()
        log_lock = threading.Lock()

        def make_thread_log(base_log_callback, lock, prefix):
            def wrapped(msg):
                if base_log_callback:
                    with lock:
                        base_log_callback(f"[{prefix}] {msg}")
            return wrapped

        written_count = 0
        serial_number = 1

        log_line(log_callback, "正在连接并拉起 Chrome...")
        from src.core import ensure_chrome_for_cdp
        ensure_chrome_for_cdp(cdp_port_or_url, log_callback=log_callback)

        def worker(raw_profile_url, profile_index):
            if should_stop(stop_event) or wait_if_paused(pause_event, stop_event):
                return
            
            profile_url = normalize_profile_url(raw_profile_url)
            if not profile_url:
                log_warn(log_callback, f"[{profile_index}/{len(profile_urls)}] 跳过无效主页：{raw_profile_url}")
                return
            
            match = re.search(r"tiktok\.com/(@[^/?#]+)", profile_url)
            username = match.group(1) if match else ""
            if not username:
                return

            thread_log = make_thread_log(log_callback, log_lock, username)
            thread_log(f"开始读取主页：{profile_url}")
            
            browser = None
            profile_page = None
            api_data = {"items": []}
            seen_video_ids = set()

            # 定义 Playwright 响应拦截监听器，拦截 itemList 获取视频 ID 与播放量
            def handle_response(response):
                if "/api/post/item_list" in response.url and "secUid" in response.url:
                    try:
                        text = response.text()
                        if text.strip():
                            body = json.loads(text)
                            for item in body.get("itemList", []):
                                vid = item.get("id", "")
                                if vid and vid not in seen_video_ids:
                                    seen_video_ids.add(vid)
                                    stats = item.get("stats", {})
                                    api_data["items"].append({
                                        "video_id": vid,
                                        "play_count": stats.get("playCount", 0)
                                    })
                    except Exception:
                        pass

            try:
                with sync_playwright() as playwright:
                    try:
                        browser, context = connect_existing_chromium(playwright, cdp_port_or_url, log_callback=log_callback)
                    except Exception as exc:
                        thread_log(f"[ERROR] 连接失败：{exc}")
                        return

                    profile_page = context.new_page()
                    profile_page.on("response", handle_response)
                    
                    try:
                        profile_page.goto(profile_url, wait_until="domcontentloaded", timeout=page_load_timeout)
                        interruptible_sleep(2.5, stop_event)
                    except PlaywrightTimeoutError:
                        thread_log("[WARN] 主页加载超时，跳过。")
                        profile_page.remove_listener("response", handle_response)
                        return

                    no_new_count = 0
                    local_written = 0

                    for scroll_index in range(max_scrolls):
                        if should_stop(stop_event) or wait_if_paused(pause_event, stop_event):
                            break

                        new_items = api_data["items"]
                        api_data["items"] = []
                        
                        if new_items:
                            no_new_count = 0
                            thread_log(f"滚动 {scroll_index + 1}/{max_scrolls}：拦截到 {len(new_items)} 条视频数据。")
                            
                            # 解析视频数据并写入 Excel
                            rows_to_write = []
                            for item in new_items:
                                video_link = f"https://www.tiktok.com/{username}/video/{item['video_id']}"
                                play_count = item["play_count"]
                                rows_to_write.append((video_link, play_count))

                            with writer_lock:
                                nonlocal serial_number, written_count
                                for video_link, play_count in rows_to_write:
                                    row = {
                                        "序号": str(serial_number),
                                        "视频链接": video_link,
                                        "播放量": str(play_count),
                                    }
                                    writer.writerow(sanitize_csv_row(row))
                                    written_count += 1
                                    local_written += 1
                                    serial_number += 1
                                    thread_log(f"  [{written_count}] {video_link} 播放量 {play_count}")
                        else:
                            no_new_count += 1

                        if no_new_count >= no_new_scroll_limit:
                            thread_log("连续多次未拦截到新数据，结束当前主页。")
                            break

                        trigger_profile_lazy_load(profile_page)
                        if interruptible_sleep(scroll_interval, stop_event):
                            break

                    profile_page.remove_listener("response", handle_response)
            except Exception as e:
                thread_log(f"[ERROR] 发生异常: {e}")
            finally:
                try:
                    if profile_page and not profile_page.is_closed():
                        profile_page.close()
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
        log_line(log_callback, f"完成：共写入 {written_count} 条，已保存：{output_path}")
    except Exception as e:
        import traceback
        log_error(log_callback, f"发生主线程异常: {e}\n{traceback.format_exc()}")
    finally:
        finish_callback(completed_path)

