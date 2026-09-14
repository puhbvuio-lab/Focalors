from __future__ import annotations

import random
import re
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

from playwright.sync_api import sync_playwright

from src.core import (
    XlsxRowWriter,
    build_output_path,
    connect_existing_chromium,
    expand_compact_number,
    interruptible_sleep,
    log_error,
    log_line,
    log_warn,
    sanitize_csv_row,
    should_stop,
    wait_if_paused,
)

CSV_FIELDS = ["博主主页链接", "博主名称", "博主ID", "粉丝量", "作者简介"]

def clean_url(url: str) -> str:
    """
    清洗并规范化输入的主页链接，补齐协议头并裁剪查询参数。
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

def normalize_profile_url(url: str) -> str:
    """
    从链接中提取博主 ID，组装成标准的 TikTok 博主主页 URL。
    """
    cleaned = clean_url(url)
    match = re.search(r"tiktok\.com/(@[^/?#]+)", cleaned)
    return f"https://www.tiktok.com/{match.group(1)}" if match else ""

def profile_id_from_url(profile_url: str) -> str:
    """
    从博主主页 URL 提取 @username 博主 ID。
    """
    match = re.search(r"tiktok\.com/@([^/?#]+)", profile_url)
    return f"@{match.group(1)}" if match else ""

def parse_profile_urls(txt_path: str) -> list[str]:
    """
    从博主 TXT 配置文件中读取全部非重复且合法的博主主页链接。
    """
    urls: list[str] = []
    seen = set()
    with open(txt_path, "r", encoding="utf-8-sig") as f:
        for line in f:
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

def get_first_text(page, selectors: list[str], timeout: int = 2500) -> str:
    """
    使用多重选择器候选列表，安全返回页面中第一个匹配成功的节点的 inner_text。
    """
    for selector in selectors:
        try:
            loc = page.locator(selector).first
            if loc.count() <= 0:
                continue
            text = loc.inner_text(timeout=timeout).strip()
            if text:
                return text
        except Exception:
            continue
    return ""

def extract_profile_row(page, profile_url: str, page_load_timeout: int = 35000, captcha_wait: int = 12, stop_event=None) -> dict[str, str]:
    """
    进入指定的博主主页，检测人机验证码，并安全提取博主名称、ID、粉丝量、博主简介等元数据。
    如果账号不存在或已注销，进行优雅的错误处理并返回标识字段。
    """
    page.goto(profile_url, wait_until="domcontentloaded", timeout=page_load_timeout)
    interruptible_sleep(random.uniform(1.5, 2.5), stop_event)

    try:
        # 检测是否弹出人机验证页面，若有则睡眠指定秒数供人工操作
        if "captcha" in page.url or page.locator("div[id^='captcha']").count() > 0:
            interruptible_sleep(captcha_wait, stop_event)
    except Exception:
        pass

    missing_text = page.locator("text=/Couldn't find this account|无法找到此账号|账号不存在/i")
    if missing_text.count() > 0:
        return {
            "博主主页链接": profile_url,
            "博主名称": "账号不可用",
            "博主ID": profile_id_from_url(profile_url),
            "粉丝量": "",
            "作者简介": "账号不存在、已注销或当前不可见",
        }

    # 多重元素路径定位保障数据高提取率
    user_title = get_first_text(page, ["[data-e2e='user-title']", "h1"])
    user_subtitle = get_first_text(page, ["[data-e2e='user-subtitle']", "h2"])
    followers = expand_compact_number(get_first_text(page, ["[data-e2e='followers-count']"]))
    bio = get_first_text(page, ["[data-e2e='user-bio']"])

    author_id = user_title or profile_id_from_url(profile_url)
    author_name = user_subtitle or user_title or author_id
    bio = bio.replace("\r", "").replace("\n", " | ")

    return {
        "博主主页链接": profile_url,
        "博主名称": author_name,
        "博主ID": author_id,
        "粉丝量": followers,
        "作者简介": bio,
    }

def run_tiktok_profile_spider(txt_path: str, cdp_port_or_url: str, log_callback, finish_callback, stop_event=None, pause_event=None, config=None):
    """
    TikTok 博主主页基础元数据爬虫主入口。
    并发遍历 TXT 文件中的博主链接，提取基础元数据并保存至对应的 Excel 报表中，
    通过 ThreadPoolExecutor 进行并发提取，使用 Lock 保护写入与日志。
    """
    if config is None:
        config = {}
    page_load_timeout = int(config.get("page_load_timeout", 35000))
    captcha_wait = int(config.get("captcha_wait", 12))
    max_parallel_tabs = int(config.get("max_parallel_tabs", 3))

    cooldown_every = int(config.get("cooldown_every", 3))
    cooldown_min = float(config.get("cooldown_min", 4.0))
    cooldown_max = float(config.get("cooldown_max", 9.0))

    output_path = None
    completed_path = None
    try:
        profile_urls = parse_profile_urls(txt_path)
        if not profile_urls:
            log_warn(log_callback, "TXT 中没有找到有效的 TikTok 博主主页链接。")
            return

        output_path = build_output_path("tiktok", f"tiktok_profiles_{time.strftime('%Y%m%d_%H%M%S')}.xlsx")
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

        completed_count = 0
        completed_lock = threading.Lock()

        def worker(profile_url, index):
            if should_stop(stop_event):
                return
            if wait_if_paused(pause_event, stop_event):
                return
            
            thread_log = make_thread_log(log_callback, log_lock, profile_id_from_url(profile_url) or f"user_{index}")
            thread_log(f"[{index}/{len(profile_urls)}] 开始提取博主信息：{profile_url}")
            
            browser = None
            page = None
            try:
                with sync_playwright() as p:
                    browser, context = connect_existing_chromium(p, cdp_port_or_url)
                    page = context.new_page()
                    row = extract_profile_row(page, profile_url, page_load_timeout=page_load_timeout, captcha_wait=captcha_wait, stop_event=stop_event)
                    thread_log(f"完成：{row['博主名称']} | {row['博主ID']} | 粉丝 {row['粉丝量'] or '未提取'}")
            except Exception as exc:
                row = {
                    "博主主页链接": profile_url,
                    "博主名称": "抓取失败",
                    "博主ID": profile_id_from_url(profile_url),
                    "粉丝量": "",
                    "作者简介": str(exc),
                }
                thread_log(f"[ERROR] 失败：{exc}")
            finally:
                if page and not page.is_closed():
                    try:
                        page.close()
                    except Exception:
                        pass
                try:
                    if browser:
                        browser.close()
                except Exception:
                    pass
            
            with writer_lock:
                writer.writerow(sanitize_csv_row(row))

            # Cooldown logic
            with completed_lock:
                nonlocal completed_count
                completed_count += 1
                current_completed = completed_count

            if current_completed % cooldown_every == 0 and current_completed < len(profile_urls):
                cooldown_time = random.uniform(cooldown_min, cooldown_max)
                log_line(log_callback, f"已处理 {current_completed} 个博主，触发批量冷却，休眠 {cooldown_time:.1f} 秒...")
                interruptible_sleep(cooldown_time, stop_event)

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
        log_line(log_callback, f"完成，已保存：{output_path}")
        completed_path = output_path
    finally:
        finish_callback(completed_path)
