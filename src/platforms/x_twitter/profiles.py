from __future__ import annotations

import re
import time

from playwright.sync_api import sync_playwright

from src.core import (
    build_output_path,
    connect_existing_chromium,
    expand_compact_number,
    interruptible_sleep,
    log_error,
    log_line,
    log_warn,
    sanitize_xlsx_cell,
    should_stop,
    wait_if_paused,
    XlsxRowWriter,
)
from src.core.task_checkpoint import open_checkpointed_row_writer, open_task_checkpoint

OUTPUT_FIELDS = ["推文链接", "作者主页链接", "作者的名称", "账号ID", "粉丝数", "简介"]
OUTPUT_FIELDS_PROFILE_MODE = ["作者主页链接", "作者的名称", "账号ID", "粉丝数", "简介"]
PAGE_LOAD_TIMEOUT = 45000
STATUS_RE = re.compile(r"/status/(\d+)")
TWEET_READY_TIMEOUT = 12000

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
    return normalized

def parse_tweet_links(txt_path: str) -> list[str]:
    links: list[str] = []
    with open(txt_path, "r", encoding="utf-8-sig") as f:
        for line in f:
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            url = normalize_x_url(stripped.split()[0])
            if "/status/" in url:
                links.append(url)
    return links

def parse_profile_links(txt_path: str) -> list[str]:
    links: list[str] = []
    with open(txt_path, "r", encoding="utf-8-sig") as f:
        for line in f:
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            url = normalize_x_url(stripped.split()[0])
            if url and "/status/" not in url:
                links.append(url)
    return links

def extract_status_id(url: str) -> str:
    match = STATUS_RE.search(url or "")
    return match.group(1) if match else ""

def parse_metric_number(text: str) -> float:
    if not text:
        return 0
    expanded = expand_compact_number(text)
    try:
        return float(expanded)
    except ValueError:
        return 0

def safe_text(locator, default: str = "") -> str:
    try:
        if locator.count() <= 0:
            return default
        return locator.first.inner_text(timeout=2000).strip() or default
    except Exception:
        return default

def safe_attr(locator, attr: str, default: str = "") -> str:
    try:
        if locator.count() <= 0:
            return default
        return locator.first.get_attribute(attr, timeout=2000) or default
    except Exception:
        return default

def extract_bio(page) -> str:
    """Extract the bio/description from a profile page."""
    selectors = [
        'div[data-testid="UserDescription"]',
        'div[data-testid="profile_bio"]',
        'div[data-testid="userProfileInfo"]',
        'div[id="profile-acc-bio"]',
        'div[dir="auto"]:has(span)',
    ]
    for selector in selectors:
        try:
            locator = page.locator(selector)
            if locator.count() <= 0:
                continue
            text = locator.first.inner_text(timeout=2000).strip()
            if text and len(text) > 0:
                return text
        except Exception:
            continue
    return ""


def find_target_article(page, target_status_id: str):
    try:
        page.wait_for_selector('article[data-testid="tweet"]', timeout=20000)
    except Exception:
        return None

    articles = page.locator('article[data-testid="tweet"]').all()
    for article in articles:
        try:
            hrefs = [
                a.get_attribute("href") or ""
                for a in article.locator('a[href*="/status/"]').all()
            ]
            if any(target_status_id in href for href in hrefs):
                return article
        except Exception:
            continue
    return articles[0] if articles else None

def load_tweet_page(page, tweet_url: str, target_status_id: str, log_callback, page_timeout=None, tweet_ready_timeout=None) -> bool:
    if page_timeout is None:
        page_timeout = PAGE_LOAD_TIMEOUT
    if tweet_ready_timeout is None:
        tweet_ready_timeout = TWEET_READY_TIMEOUT
    try:
        page.goto(tweet_url, wait_until="domcontentloaded", timeout=page_timeout)
        page.wait_for_selector('article[data-testid="tweet"]', timeout=tweet_ready_timeout)
        return True
    except Exception as e:
        current_url = getattr(page, "url", "")
        title = ""
        try:
            title = page.title()
        except Exception:
            pass
        log_warn(log_callback, 
            f"  推文正文未在 {tweet_ready_timeout // 1000} 秒内渲染，快速跳过。当前 URL: {current_url or '未知'}，标题: {title or '未知'}，错误: {e}"
        )
    return False

def extract_author_from_article(article) -> dict:
    user_block = article.locator('div[data-testid="User-Name"]').first
    author_name = ""
    account_id = ""
    profile_url = ""

    try:
        spans = user_block.locator("span").all()
        for span in spans:
            text = span.inner_text(timeout=1000).strip()
            if not text:
                continue
            if text.startswith("@") and not account_id:
                account_id = text.lstrip("@")
            elif not author_name:
                author_name = text
    except Exception:
        pass

    try:
        links = user_block.locator('a[role="link"]').all()
        for link in links:
            href = link.get_attribute("href") or ""
            normalized = normalize_x_url(href)
            if not normalized or "/status/" in normalized:
                continue
            handle_match = re.search(r"x\.com/([^/?#]+)$", normalized)
            if handle_match:
                account_id = handle_match.group(1)
                profile_url = f"https://x.com/{account_id}"
                break
    except Exception:
        pass

    if account_id and not profile_url:
        profile_url = f"https://x.com/{account_id}"

    return {
        "author_name": author_name,
        "account_id": account_id,
        "profile_url": profile_url,
    }

def extract_view_count(article) -> tuple[str, float]:
    selectors = [
        'a[href*="/analytics"]',
        'div[data-testid="postViewCount"]',
        'span[aria-label*="Views"]',
        'span[aria-label*="浏览"]',
        'span[aria-label*="表示"]',
    ]
    for selector in selectors:
        try:
            locator = article.locator(selector)
            if locator.count() <= 0:
                continue
            text = locator.first.inner_text(timeout=1500).strip()
            aria = locator.first.get_attribute("aria-label", timeout=1500) or ""
            raw = text or aria
            if raw:
                return raw, parse_metric_number(raw)
        except Exception:
            continue
    return "", 0

def extract_followers_count(
    page,
    profile_url: str,
    page_timeout=None,
    stop_event=None,
    needs_navigation=True,
    log_callback=None,
    pause_event=None,
    recovery_config=None,
    use_search_entry: bool = False,
) -> str:
    if page_timeout is None:
        page_timeout = PAGE_LOAD_TIMEOUT
    
    if needs_navigation:
        from src.platforms.x_twitter.profile_tweets import navigate_to_profile

        if not navigate_to_profile(
            page,
            profile_url,
            log_callback,
            page_timeout=page_timeout,
            stop_event=stop_event,
            pause_event=pause_event,
            recovery_config=recovery_config,
            use_search_entry=use_search_entry,
        ):
            return ""

    selectors = [
        'a[href$="/followers"]',
        'a[href*="/followers"]',
        'a[href*="/verified_followers"]',
        'text=/(?:followers?|粉丝|关注者|粉絲|關注者|フォロワー|팔로워|seguidores?|abonn[eé]s?|читатели|متابع(ون)?|takipçi(ler)?|pengikut|फ़ॉलोअर्स)/i',
    ]

    start_time = time.time()
    max_poll_time = 15  # 最多轮询15秒等待网络数据返回
    
    while time.time() - start_time < max_poll_time:
        if should_stop(stop_event):
            break
            
        # 尝试绕过敏感内容警告 (Sensitive Content Warning)
        try:
            btn_selectors = [
                'div[data-testid="empty_state_button_text"]',
                'div[role="button"]:has-text("view profile")',
                'div[role="button"]:has-text("查看个人主页")',
                'div[role="button"]:has-text("プロフィールを表示")'
            ]
            for btn_sel in btn_selectors:
                btn = page.locator(btn_sel)
                if btn.count() > 0 and btn.first.is_visible(timeout=100):
                    btn.first.click(timeout=1000)
                    interruptible_sleep(1.0, stop_event)
                    break
        except Exception:
            pass

        for selector in selectors:
            try:
                for node in page.locator(selector).all():
                    # 使用 text_content() 代替 inner_text()，即使被弹窗遮挡也能读取
                    text = node.text_content(timeout=500) or ""
                    aria = node.get_attribute("aria-label", timeout=500) or ""
                    
                    # 为了应对文本选择器匹配到最深层节点（只有“粉丝”而无数字）的情况，向上获取父级和祖父级的文本
                    family_text = ""
                    try:
                        family_text = node.evaluate("""n => {
                            let p1 = n.parentElement ? n.parentElement.textContent : '';
                            let p2 = (n.parentElement && n.parentElement.parentElement) ? n.parentElement.parentElement.textContent : '';
                            return [n.textContent, p1, p2].join(' | ');
                        }""")
                    except Exception:
                        pass
                    
                    for raw in (text, aria, family_text):
                        if not raw:
                            continue
                            
                        # 确保数字紧挨着粉丝关键词，防止误匹配个人简介里的无关文本
                        pattern = r"([\d,.]+(?:\.\d+)?\s*(?:[KkMmBb]|千|万|萬|亿|億)?)\s*(?:followers?|粉丝|关注者|粉絲|關注者|フォロワー|팔로워|seguidores?|abonn[eé]s?|читатели|متابع(?:ون)?|takipçi(?:ler)?|pengikut|फ़ॉलोअर्स)"
                        match = re.search(pattern, raw, re.IGNORECASE)
                        if match:
                            return expand_compact_number(match.group(1).strip())
            except Exception:
                continue
                
        interruptible_sleep(1.0, stop_event)

    return ""

def extract_tweet_author_record(
    tweet_page,
    profile_page,
    tweet_url: str,
    log_callback,
    page_timeout=None,
    tweet_ready_timeout=None,
    stop_event=None,
    pause_event=None,
    recovery_config=None,
    use_search_entry: bool = False,
) -> dict | None:
    target_status_id = extract_status_id(tweet_url)
    if not target_status_id:
        log_warn(log_callback, f"跳过：无法解析推文 ID：{tweet_url}")
        return None

    if not load_tweet_page(tweet_page, tweet_url, target_status_id, log_callback, page_timeout=page_timeout, tweet_ready_timeout=tweet_ready_timeout):
        log_warn(log_callback, f"跳过：推文页面一直卡在 X 启动页或未渲染正文：{tweet_url}")
        return None

    article = find_target_article(tweet_page, target_status_id)
    if article is None:
        log_warn(log_callback, f"跳过：未找到推文正文：{tweet_url}")
        return None

    author = extract_author_from_article(article)
    if not author["account_id"] or not author["profile_url"]:
        log_warn(log_callback, f"跳过：无法提取作者信息：{tweet_url}")
        return None

    view_text, view_value = extract_view_count(article)
    followers = extract_followers_count(
        profile_page,
        author["profile_url"],
        page_timeout=page_timeout,
        stop_event=stop_event,
        log_callback=log_callback,
        pause_event=pause_event,
        recovery_config=recovery_config,
        use_search_entry=use_search_entry,
    )

    bio = extract_bio(profile_page)

    return {
        "推文链接": normalize_x_url(tweet_url),
        "作者主页链接": author["profile_url"],
        "作者的名称": author["author_name"],
        "账号ID": author["account_id"],
        "粉丝数": followers,
        "简介": bio,
        "_view_text": view_text,
        "_view_value": view_value,
    }

def extract_profile_record(
    profile_page,
    profile_url: str,
    log_callback,
    page_timeout=None,
    stop_event=None,
    pause_event=None,
    recovery_config=None,
    use_search_entry: bool = False,
) -> dict | None:
    """Extract profile info directly from profile URL."""
    profile_url = normalize_x_url(profile_url)
    from src.platforms.x_twitter.profile_tweets import navigate_to_profile

    if not navigate_to_profile(
        profile_page,
        profile_url,
        log_callback,
        page_timeout=page_timeout if page_timeout is not None else PAGE_LOAD_TIMEOUT,
        stop_event=stop_event,
        pause_event=pause_event,
        recovery_config=recovery_config,
        use_search_entry=use_search_entry,
    ):
        log_warn(log_callback, f"跳过：无法加载主页：{profile_url}")
        return None

    # Extract account ID from URL
    account_match = re.search(r"x\.com/([^/?#]+)/?$", profile_url)
    if not account_match:
        log_warn(log_callback, f"跳过：无法解析账号 ID：{profile_url}")
        return None
    account_id = account_match.group(1)

    # Extract author name from profile header
    author_name = ""
    try:
        profile_page.wait_for_selector('div[data-testid="UserName"]', state="attached", timeout=10000)
    except Exception:
        pass

    try:
        name_selectors = [
            'div[data-testid="UserName"] span',
            'div[data-testid="profile_header_0"] div[dir="auto"] span'
        ]
        for selector in name_selectors:
            try:
                for node in profile_page.locator(selector).all():
                    text = node.text_content(timeout=500)
                    if text and text.strip() and not text.strip().startswith('@'):
                        author_name = text.strip()
                        break
            except Exception:
                continue
            if author_name:
                break
    except Exception:
        pass

    # Extract followers count
    followers = extract_followers_count(profile_page, profile_url, page_timeout=page_timeout, stop_event=stop_event, needs_navigation=False)

    # Extract bio
    bio = extract_bio(profile_page)

    return {
        "作者主页链接": profile_url,
        "作者的名称": author_name,
        "账号ID": account_id,
        "粉丝数": followers,
        "简介": bio,
    }

def output_row(record: dict, fields: list[str]) -> dict:
    return {field: record.get(field, "") for field in fields}


def update_writer_row(writer: XlsxRowWriter, row_number: int, record: dict, fields: list[str]) -> None:
    row = output_row(record, fields)
    for column_number, field in enumerate(fields, start=1):
        writer.worksheet.cell(row=row_number, column=column_number).value = sanitize_xlsx_cell(row.get(field, ""))

import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

def _scrape_single_profile_task(
    index: int,
    link: str,
    is_profile_mode: bool,
    cdp_port_or_url: str,
    page_load_timeout: int,
    tweet_ready_timeout: int,
    log_callback,
    stop_event,
    pause_event,
    writer,
    writer_lock,
    best_by_author,
    row_by_author,
    output_fields,
    total_links: int,
    cooldown_every: int = 3,
    cooldown_min: float = 4.0,
    cooldown_max: float = 9.0,
    completed_state: dict = None,
    browser_choice: str | None = None,
    checkpoint=None,
    output_path: str | None = None,
    recovery_config: dict | None = None,
    use_search_entry: bool = False,
):
    from src.core import interruptible_sleep
    import random
    from src.platforms.x_twitter.profiles import (
        extract_profile_record,
        extract_tweet_author_record,
        output_row,
        update_writer_row,
    )

    if should_stop(stop_event):
        return
    if wait_if_paused(pause_event, stop_event):
        return

    playwright = None
    browser = None
    tweet_page = None
    profile_page = None
    checkpoint_claimed = False
    task_succeeded = False
    record = None

    try:
        if checkpoint is not None:
            checkpoint_claimed, claim_status = checkpoint.claim_item(link)
            if not checkpoint_claimed:
                log_line(log_callback, f"[{index}/{total_links}] 断点跳过 ({claim_status})：{link}")
                return

        playwright = sync_playwright().start()
        try:
            browser, context = connect_existing_chromium(playwright, cdp_port_or_url, browser=browser_choice)
        except Exception as e:
            log_error(log_callback, f"[{index}/{total_links}] 连接失败：{e}")
            return

        tweet_page = context.new_page() if not is_profile_mode else None
        profile_page = context.new_page()

        if is_profile_mode:
            log_line(log_callback, f"[{index}/{total_links}] 处理博主链接：{link}")
            record = extract_profile_record(
                profile_page,
                link,
                log_callback,
                page_timeout=page_load_timeout,
                stop_event=stop_event,
                pause_event=pause_event,
                recovery_config=recovery_config,
                use_search_entry=use_search_entry,
            )
        else:
            log_line(log_callback, f"[{index}/{total_links}] 处理推文：{link}")
            record = extract_tweet_author_record(
                tweet_page,
                profile_page,
                link,
                log_callback,
                page_timeout=page_load_timeout,
                tweet_ready_timeout=tweet_ready_timeout,
                stop_event=stop_event,
                pause_event=pause_event,
                recovery_config=recovery_config,
                use_search_entry=use_search_entry,
            )

        if not record:
            return

        with writer_lock:
            account_key = record["账号ID"].lower()
            old_record = best_by_author.get(account_key)
            if old_record is None:
                writer.writerow(output_row(record, output_fields))
                best_by_author[account_key] = record
                row_by_author[account_key] = writer.worksheet.max_row

                # Update '序号' to be globally sequential
                seq_num = len(best_by_author)
                writer.worksheet.cell(row=row_by_author[account_key], column=1).value = str(seq_num)
                record["序号"] = str(seq_num)

                if is_profile_mode:
                    log_line(log_callback, f"  写入作者 {account_key or '未知'}。")
                else:
                    log_line(log_callback, f"  写入作者 {account_key or '未知'}，当前推文浏览量 {record.get('_view_text') or '未知'}。")
            elif not is_profile_mode and record["_view_value"] > old_record.get("_view_value", 0):
                best_by_author[account_key] = record
                record["序号"] = old_record.get("序号", "")
                update_writer_row(writer, row_by_author[account_key], record, output_fields)
                log_line(log_callback,
                    f"  更新作者 {record['账号ID']}：更高浏览量 {record.get('_view_text') or '未知'}。"
                )
            else:
                if is_profile_mode:
                    log_line(log_callback, f"  跳过：作者 {record['账号ID']} 已处理过。")
                else:
                    log_line(log_callback, f"  跳过：作者 {record['账号ID']} 已有更高浏览量推文。")

        task_succeeded = True
        if checkpoint is not None:
            checkpoint.mark_completed(
                link,
                {
                    "output_path": output_path,
                    "index": index,
                    "account_id": record.get("账号ID", ""),
                },
            )
    except Exception as exc:
        log_error(log_callback, f"[{index}/{total_links}] 异常: {exc}")
    finally:
        if checkpoint is not None and checkpoint_claimed and not task_succeeded:
            try:
                checkpoint.release_item(link)
            except Exception:
                pass
        # 连接此时仍然存活（playwright.stop() 延后到最后），
        # page.close() 才能真正发送 Target.closeTarget 关闭真实标签页。
        for opened_page in (tweet_page, profile_page):
            try:
                if opened_page is not None and not opened_page.is_closed():
                    opened_page.close()
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

        # Cooldown logic
        if completed_state is not None:
            with completed_state["lock"]:
                completed_state["count"] += 1
                current_completed = completed_state["count"]

            if current_completed % cooldown_every == 0 and current_completed < total_links:
                cooldown_time = random.uniform(cooldown_min, cooldown_max)
                log_line(log_callback, f"已处理 {current_completed} 个链接，触发批量冷却，休眠 {cooldown_time:.1f} 秒...")
                interruptible_sleep(cooldown_time, stop_event)

def run_scraper(txt_path: str, input_mode: str, cdp_port_or_url: str, log_callback, finish_callback, stop_event=None, config=None, pause_event=None):
    if config is None:
        config = {}
    page_load_timeout = int(config.get("page_load_timeout", PAGE_LOAD_TIMEOUT))
    tweet_ready_timeout = int(config.get("tweet_ready_timeout", TWEET_READY_TIMEOUT))
    max_parallel_tabs = int(config.get("max_parallel_tabs", 3))
    browser_choice = config.get("browser")
    from src.platforms.x_twitter.profile_tweets import use_profile_search_entry

    search_entry_enabled = use_profile_search_entry(config)

    cooldown_every = int(config.get("cooldown_every", 3))
    cooldown_min = float(config.get("cooldown_min", 4.0))
    cooldown_max = float(config.get("cooldown_max", 9.0))

    output_path = None
    checkpoint = None
    try:
        from src.core import ensure_chrome_for_cdp
        try:
            ensure_chrome_for_cdp(cdp_port_or_url, log_callback=log_callback)
        except Exception:
            pass

        is_profile_mode = input_mode == "博主链接"
        
        if is_profile_mode:
            links = parse_profile_links(txt_path)
            output_fields = OUTPUT_FIELDS_PROFILE_MODE
            if not links:
                log_warn(log_callback, "TXT 中没有有效的博主链接。")
                return
        else:
            links = parse_tweet_links(txt_path)
            output_fields = OUTPUT_FIELDS
            if not links:
                log_warn(log_callback, "TXT 中没有有效的推文链接。")
                return

        checkpoint = open_task_checkpoint(
            "x_profiles",
            {
                "links": links,
                "input_mode": input_mode,
            },
            log_callback=log_callback,
            merge_on_keys=("links",),
            merge_keep_keys=("input_mode",),
        )
        default_output_path = build_output_path("x", f"x_profiles_{time.strftime('%Y%m%d_%H%M%S')}.xlsx")
        output_path, writer = open_checkpointed_row_writer(
            checkpoint,
            default_output_path,
            output_fields,
            log_callback=log_callback,
        )
        checkpoint.add_output_path(output_path)
        best_by_author: dict[str, dict] = {}
        row_by_author: dict[str, int] = {}
        account_field = output_fields[2 if is_profile_mode else 3]
        for row_number, values in enumerate(writer.worksheet.iter_rows(min_row=2, values_only=True), start=2):
            existing = dict(zip(output_fields, values))
            account_key = str(existing.get(account_field) or "").strip().lower()
            if not account_key or account_key in best_by_author:
                continue
            existing["_view_value"] = 0
            best_by_author[account_key] = existing
            row_by_author[account_key] = row_number

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

        completed_state = {
            "count": 0,
            "lock": threading.Lock()
        }

        with ThreadPoolExecutor(max_workers=max_parallel_tabs) as executor:
            futures = []
            for index, link in enumerate(links, 1):
                if should_stop(stop_event):
                    break
                if wait_if_paused(pause_event, stop_event):
                    break
                futures.append(
                    executor.submit(
                        _scrape_single_profile_task,
                        index=index,
                        link=link,
                        is_profile_mode=is_profile_mode,
                        cdp_port_or_url=cdp_port_or_url,
                        page_load_timeout=page_load_timeout,
                        tweet_ready_timeout=tweet_ready_timeout,
                        log_callback=safe_log_callback,
                        stop_event=stop_event,
                        pause_event=pause_event,
                        writer=writer,
                        writer_lock=writer_lock,
                        best_by_author=best_by_author,
                        row_by_author=row_by_author,
                        output_fields=output_fields,
                        total_links=len(links),
                        cooldown_every=cooldown_every,
                        cooldown_min=cooldown_min,
                        cooldown_max=cooldown_max,
                        completed_state=completed_state,
                        browser_choice=browser_choice,
                        checkpoint=checkpoint,
                        output_path=output_path,
                        recovery_config=config,
                        use_search_entry=search_entry_enabled,
                    )
                )

            for future in as_completed(futures):
                try:
                    future.result()
                except Exception as exc:
                    log_error(safe_log_callback, f"线程执行异常: {exc}")

        if not output_path:
            log_warn(safe_log_callback, "没有提取到可输出的数据。")
            return
            
        with writer_lock:
            writer.save()
        log_line(safe_log_callback, f"完成，已保存：{output_path}")
    finally:
        if checkpoint is not None:
            checkpoint.close_run()
        if finish_callback:
            finish_callback(output_path)
