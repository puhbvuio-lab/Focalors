from __future__ import annotations

import random
import time
from datetime import datetime
import re

from src.core import (
    expand_compact_number,
    interruptible_sleep,
    log_error,
    log_line,
    log_warn,
    sanitize_csv_cell,
    should_stop,
    wait_if_paused,
)

DEFAULT_SCAN_LIMIT = 500
SCROLL_PAUSE = 1.5
NO_NEW_SCROLL_LIMIT = 5
# 首屏回复渲染等待：连续多少次数量不变即认为稳定
RENDER_STABLE_CHECKS = 3
DEFAULT_RENDER_WAIT = 8.0
# 推荐区出现后，再容忍几轮无新增才停止
DEFAULT_BOUNDARY_PATIENCE = 3
PROMOTED_MARKERS = ("promoted", "ad", "广告", "推广", "スポンサー", "pr", "赞助")


def format_comment_time(raw_time: str) -> str:
    value = (raw_time or "").strip()
    if not value:
        return ""
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        match = re.search(r"(\d{4}-\d{2}-\d{2})[T\s](\d{2}):(\d{2}):(\d{2})", value)
        if match:
            return f"{match.group(1)} {match.group(2)}:{match.group(3)}:{match.group(4)}"
    return value

def has_selector(node, selector: str) -> bool:
    try:
        return node.query_selector(selector) is not None
    except Exception:
        return False

def text_has_promoted_marker(text: str) -> bool:
    raw_text = text or ""
    lines = [line.strip().lower() for line in raw_text.splitlines() if line.strip()]
    if any(line in {"ad", "promoted", "广告", "推广"} for line in lines):
        return True
    normalized = re.sub(r"\s+", " ", raw_text).strip().lower()
    if not normalized:
        return False
    for marker in PROMOTED_MARKERS:
        if len(marker) <= 2 and marker.isascii():
            # Short ASCII markers ("ad", "pr") need word boundary to avoid false positives
            if re.search(r'\b' + re.escape(marker) + r'\b', normalized):
                return True
        elif marker in normalized:
            return True
    return False

def is_promoted_tweet(article) -> bool:
    try:
        text = article.evaluate(
            """node => {
                const hasPromotedContainer = el => {
                    if (!el || !el.querySelectorAll) return false;
                    const dataTestId = (el.getAttribute && el.getAttribute('data-testid')) || '';
                    if (/placementTracking|promoted/i.test(dataTestId)) return true;
                    return !!el.querySelector('[data-testid*="placementTracking"], [data-testid*="promoted"], [data-testid*="Promoted"]');
                };
                if (hasPromotedContainer(node)) return 'Ad';

                const parts = Array.from(node.querySelectorAll('[data-testid="socialContext"], [aria-label], span, div'))
                    .map(el => (el.innerText || el.getAttribute('aria-label') || '').trim())
                    .filter(Boolean);
                const cell = node.closest('[data-testid="cellInnerDiv"]') || node.parentElement;
                if (cell) {
                    if (hasPromotedContainer(cell)) return 'Ad';
                    parts.push(cell.innerText || '');
                    if (cell.previousElementSibling) {
                        parts.push(cell.previousElementSibling.innerText || cell.previousElementSibling.getAttribute('aria-label') || '');
                    }
                }
                return parts.join('\\n');
            }"""
        )
    except Exception:
        try:
            text = article.inner_text()
        except Exception:
            text = ""
    return text_has_promoted_marker(str(text or ""))

def detect_non_text_content_type(article) -> str:
    if has_selector(article, '[data-testid="videoPlayer"], video'):
        return "视频"
    if has_selector(article, '[data-testid="tweetPhoto"], img[src*="/media/"]'):
        return "图片"
    if has_selector(article, '[aria-label="GIF"], [data-testid="gif"]'):
        return "GIF"
    if has_selector(article, '[data-testid="card.wrapper"]'):
        return "链接卡片"
    if has_selector(article, '[role="radio"], [aria-label*="poll"]'):
        return "投票"
    return "非文本"

def _click_show_replies_buttons(page, stop_event=None) -> int:
    """Click all visible 'show replies' buttons to reveal hidden comments."""
    SHOW_REPLIES_TEXTS = (
        "show replies", "show reply", "显示回复", "显示回复内容",
        "返信を表示", "返信をすべて表示", "답글 보기",
        "voir les réponses", "antworten anzeigen", "mostrar respuestas",
        "mostrar respostas", "показать ответы",
        "show more replies", "显示更多回复", "もっと返信を表示",
    )
    clicked = 0
    for _ in range(10):
        if should_stop(stop_event):
            break
        try:
            found = page.evaluate("""(texts) => {
                let clicked = 0;
                const allBtns = document.querySelectorAll(
                    'button, div[role="button"], [data-testid="cellInnerDiv"] button, [data-testid="cellInnerDiv"] div[role="button"]'
                );
                for (const btn of allBtns) {
                    const txt = (btn.textContent || '').trim().toLowerCase();
                    if (texts.some(t => txt === t || txt.startsWith(t))) {
                        btn.scrollIntoView({block: 'center'});
                        btn.click();
                        clicked++;
                    }
                }
                return clicked;
            }""", list(SHOW_REPLIES_TEXTS))
            if found == 0:
                break
            clicked += found
            interruptible_sleep(1.5, stop_event)
        except Exception:
            break
    return clicked


def _count_first_level_articles(page) -> int:
    """统计当前 DOM 中已渲染的首层评论数量（主楼 tabindex=-1，评论为 0）。"""
    try:
        return int(
            page.evaluate(
                """() => document.querySelectorAll('article[data-testid="tweet"][tabindex="0"]').length"""
            )
            or 0
        )
    except Exception:
        return 0


def _wait_for_replies_rendered(page, render_wait: float, stop_event=None, log_callback=None) -> int:
    """
    等待首屏评论渲染稳定。

    X 的推文详情页会先渲染少量回复，其余回复在随后的若干秒内陆续插入 DOM。
    如果加载完立刻扫描，往往只能扫到第一批（实测 21 条回复的推文只扫到 7 条）。
    这里等到「连续多次数量不再变化」或到达等待上限后再开始扫描。
    """
    if render_wait <= 0:
        return _count_first_level_articles(page)

    deadline = time.time() + render_wait
    stable = 0
    last_count = -1
    count = _count_first_level_articles(page)
    while time.time() < deadline:
        if should_stop(stop_event):
            break
        count = _count_first_level_articles(page)
        if count > 0 and count == last_count:
            stable += 1
            if stable >= RENDER_STABLE_CHECKS:
                break
        else:
            stable = 0
        last_count = count
        interruptible_sleep(1.0, stop_event)
    if count:
        log_line(log_callback, f"  首屏评论已渲染 {count} 条，开始扫描。")
    return count


def _has_recommendation_boundary(page) -> bool:
    """判断页面是否已经出现「发现更多 / Discover more」推荐区。"""
    try:
        return bool(
            page.evaluate(
                """() => {
                    const markers = ['discover more', 'find more', '发现更多', '更多了解',
                        'もっと見る', '더 보기', 'encontrar más', 'descubre más'];
                    const cells = document.querySelectorAll('[data-testid="cellInnerDiv"]');
                    for (const cell of cells) {
                        if (cell.querySelector('article[data-testid="tweet"]')) continue;
                        const text = (cell.textContent || '').trim().toLowerCase();
                        if (markers.some(m => text.includes(m))) return true;
                    }
                    return false;
                }"""
            )
        )
    except Exception:
        return False


def should_stop_scanning(
    no_new_count: int,
    no_new_scroll_limit: int,
    boundary_seen: bool,
    boundary_rounds_without_new: int,
    boundary_patience: int,
) -> tuple[bool, str]:
    """
    判断评论扫描是否可以结束。

    只有「连续多轮没有新增评论」才停止。推荐区（Discover more）出现本身
    不是停止条件——它出现后上方仍可能继续补渲染评论，历史实现据此直接
    结束，导致 21 条回复的推文只抓到 7～8 条，滚动较深时甚至一条都抓不到。
    """
    if no_new_count >= no_new_scroll_limit:
        return True, f"  连续 {no_new_scroll_limit} 次滚动没有发现新评论，停止。"
    if boundary_seen and boundary_rounds_without_new >= boundary_patience:
        return True, f"  推荐区之后连续 {boundary_rounds_without_new} 轮无新增评论，停止抓取。"
    return False, ""



def extract_comments(
    page,
    tweet_url: str,
    max_count: int = DEFAULT_SCAN_LIMIT,
    log_callback=None,
    stop_event=None,
    scroll_pause=None,
    no_new_scroll_limit=None,
    pause_event=None,
    scroll_pause_max=None,
    render_wait=None,
    boundary_patience=None,
) -> list[dict[str, str]]:
    if scroll_pause is None:
        scroll_pause = SCROLL_PAUSE
    if scroll_pause_max is None:
        scroll_pause_max = max(scroll_pause, scroll_pause * 2)
    scroll_pause_max = max(float(scroll_pause_max), float(scroll_pause))
    if no_new_scroll_limit is None:
        no_new_scroll_limit = NO_NEW_SCROLL_LIMIT
    if render_wait is None:
        render_wait = DEFAULT_RENDER_WAIT
    if boundary_patience is None:
        boundary_patience = DEFAULT_BOUNDARY_PATIENCE

    comments: list[dict[str, str]] = []
    seen_ids = set()
    no_new_count = 0
    show_replies_tried = 0
    boundary_seen = False
    boundary_rounds_without_new = 0

    log_line(log_callback, f"  开始抓取评论，目标 {max_count} 条。")

    # Wait for first-level comments (tabindex="0") to appear.
    try:
        page.wait_for_selector('article[data-testid="tweet"][tabindex="0"]', timeout=8000)
    except Exception:
        pass

    # 等首屏回复渲染稳定，避免只扫到最先插入 DOM 的那一小批
    _wait_for_replies_rendered(page, render_wait, stop_event, log_callback)

    while len(comments) < max_count:
        if should_stop(stop_event):
            break
        if wait_if_paused(pause_event, stop_event):
            break

        # 推荐区只是「可能到底」的信号，不能直接结束：
        # 该区块出现后上方仍可能继续补渲染评论，因此改为软判定。
        if not boundary_seen and _has_recommendation_boundary(page):
            boundary_seen = True
            log_line(log_callback, "  检测到推荐区域，继续滚动以确认是否还有未渲染的评论。")

        # Identify first-level comments:
        # 1. tabindex="0" → actual comment (tabindex="-1" is the main tweet)
        # 2. Walk backward through cellInnerDiv siblings:
        #    - Empty cellInnerDiv → separator → first-level
        #    - Ad cell (article with tabindex≠0) → skip, keep walking
        #    - tabindex=0 article before separator → could be nested reply, or
        #      first-level comment with intervening ads. Use left indentation to decide:
        #      nested replies have ≥40px left padding; first-level comments have ~0.
        article_flags = page.evaluate(
            """() => {
                // 先定位「发现更多」推荐区所在的单元格：它下方的推文是推荐内容，
                // 不属于该推文的评论，必须排除。
                const cells = Array.from(document.querySelectorAll('[data-testid="cellInnerDiv"]'));
                const markers = ['discover more', 'find more', '发现更多', '更多了解',
                    'もっと見る', '더 보기', 'encontrar más', 'descubre más'];
                let boundaryIndex = -1;
                for (let i = 0; i < cells.length; i++) {
                    if (cells[i].querySelector('article[data-testid="tweet"]')) continue;
                    const text = (cells[i].textContent || '').trim().toLowerCase();
                    if (markers.some(m => text.includes(m))) { boundaryIndex = i; break; }
                }

                const articles = Array.from(document.querySelectorAll('article[data-testid="tweet"]'));
                const SEPARATOR_INDENT = 30;
                return articles.map(el => {
                    const cell = el.closest('[data-testid="cellInnerDiv"]');
                    const cellIndex = cell ? cells.indexOf(cell) : -1;
                    const beforeBoundary = boundaryIndex === -1 || cellIndex === -1 || cellIndex < boundaryIndex;
                    let firstLevel = false;
                    if (el.getAttribute('tabindex') === '0' && cell) {
                        let sibling = cell.previousElementSibling;
                        while (sibling && sibling.getAttribute('data-testid') === 'cellInnerDiv') {
                            const siblingArticle = sibling.querySelector('article[data-testid="tweet"]');
                            if (!siblingArticle) { firstLevel = true; break; }  // empty cell → separator → first-level
                            const sibTab = siblingArticle.getAttribute('tabindex');
                            if (sibTab === '0') {
                                // Found another comment before finding separator.
                                // Check indentation: nested replies have left padding ≥ 40px.
                                try {
                                    const cs = window.getComputedStyle(cell);
                                    const leftIndent = (parseFloat(cs.paddingLeft) || 0) + (parseFloat(cs.marginLeft) || 0);
                                    firstLevel = leftIndent < SEPARATOR_INDENT;
                                } catch (_) { firstLevel = false; }
                                break;
                            }
                            // sibTab is '-1' or null → ad/promoted cell, skip and continue
                            sibling = sibling.previousElementSibling;
                        }
                    }
                    return { firstLevel, beforeBoundary };
                });
            }"""
        )

        all_articles = page.query_selector_all('article[data-testid="tweet"]')

        new_found = 0

        for i, article in enumerate(all_articles):
            try:
                if i >= len(article_flags):
                    continue
                flags = article_flags[i]
                if not flags.get("firstLevel") or not flags.get("beforeBoundary", True):
                    continue

                if is_promoted_tweet(article):
                    continue

                # Revert auto-translation and remove CSS truncation before reading text
                try:
                    article.evaluate("""async (el) => {
                        const revertTexts = ['view original', '查看原文', '原文を表示', 'show original', '原文を見る'];
                        const allNodes = el.querySelectorAll('*');
                        for (const node of allNodes) {
                            const text = (node.textContent || '').trim().toLowerCase();
                            if (!text || node.children.length > 0) continue;
                            if (revertTexts.includes(text)) {
                                try { node.click(); } catch (_) {}
                                break;
                            }
                        }
                        const tweetText = el.querySelector('[data-testid="tweetText"]');
                        if (tweetText) {
                            tweetText.style.setProperty('max-height', 'none', 'important');
                            tweetText.style.setProperty('overflow', 'visible', 'important');
                            tweetText.style.setProperty('-webkit-line-clamp', 'unset', 'important');
                        }
                        await new Promise(r => setTimeout(r, 400));
                    }""")
                except Exception:
                    pass

                content_el = article.query_selector('div[data-testid="tweetText"]')
                content = content_el.inner_text().strip() if content_el else ""

                user_name_el = article.query_selector('div[data-testid="User-Name"]')
                user_text = user_name_el.inner_text().strip() if user_name_el else ""

                comment_time = ""
                time_el = article.query_selector("time")
                if time_el:
                    comment_time = time_el.get_attribute("datetime") or time_el.inner_text()

                if not content:
                    content = f"[{detect_non_text_content_type(article)}]"

                comment_id = f"{user_text[:80]}|{comment_time}|{content[:120]}"
                if not comment_id.strip("|") or comment_id in seen_ids:
                    continue
                seen_ids.add(comment_id)
                new_found += 1

                author_name = ""
                author_handle = ""
                if user_name_el:
                    links = user_name_el.query_selector_all('a[role="link"]')
                    if links:
                        name_span = links[0].query_selector("span")
                        if name_span:
                            author_name = name_span.inner_text().strip()
                        href = links[0].get_attribute("href") or ""
                        author_handle = href.strip("/")

                    for span in user_name_el.query_selector_all("span"):
                        span_text = span.inner_text().strip()
                        if span_text.startswith("@"):
                            author_handle = span_text
                            break

                like_count = "0"
                for testid in ("like", "unlike"):
                    container = article.query_selector(f'[data-testid="{testid}"]')
                    if not container:
                        continue
                    # Try aria-label on the inner button/div first
                    inner_btn = container.query_selector('button, div[role="button"]')
                    if inner_btn:
                        aria = inner_btn.get_attribute("aria-label") or ""
                        match = re.search(r"([\d,.]+(?:\.\d+)?\s*[KkMmBb]?)", aria)
                        if match:
                            like_count = expand_compact_number(match.group(1))
                            break
                    # Fallback: get count from container text (the sibling span next to the icon)
                    raw_text = container.inner_text().strip()
                    if raw_text and re.search(r"\d", raw_text):
                        like_count = expand_compact_number(raw_text)
                        break

                reply_count = "0"
                for sel in ('[data-testid="reply"]',):
                    reply_container = article.query_selector(sel)
                    if not reply_container:
                        continue
                    inner_btn = reply_container.query_selector('button, div[role="button"]')
                    if inner_btn:
                        aria = inner_btn.get_attribute("aria-label") or ""
                        match = re.search(r"([\d,.]+(?:\.\d+)?\s*[KkMmBb]?)", aria)
                        if match:
                            reply_count = expand_compact_number(match.group(1))
                            break
                    raw_text = reply_container.inner_text().strip()
                    if raw_text and re.search(r"\d", raw_text):
                        reply_count = expand_compact_number(raw_text)
                        break

                comments.append(
                    {
                        "author_name": str(sanitize_csv_cell(author_name)),
                        "author_handle": str(sanitize_csv_cell(author_handle)),
                        "content": str(sanitize_csv_cell(content)),
                        "time": str(sanitize_csv_cell(format_comment_time(comment_time))),
                        "likes": str(sanitize_csv_cell(like_count)),
                        "replies": str(sanitize_csv_cell(reply_count)),
                    }
                )
                log_line(log_callback, f"    [{len(comments)}/{max_count}] {author_handle}: {content[:40]}")
            except Exception as exc:
                log_warn(log_callback, f"    解析评论时出错：{exc}")

        if new_found == 0:
            if show_replies_tried < 3:
                extra = _click_show_replies_buttons(page, stop_event)
                show_replies_tried += 1
                if extra > 0:
                    log_line(log_callback, f"  点击了 {extra} 个「显示回复」按钮。")
                    interruptible_sleep(random.uniform(1.5, 3.0), stop_event)
                    # Scroll down so revealed replies enter the viewport
                    page.evaluate("window.scrollTo(0, document.documentElement.scrollHeight)")
                    interruptible_sleep(random.uniform(float(scroll_pause), scroll_pause_max), stop_event)
                    continue
            no_new_count += 1
            if boundary_seen:
                boundary_rounds_without_new += 1
            stop_scan, stop_reason = should_stop_scanning(
                no_new_count,
                no_new_scroll_limit,
                boundary_seen,
                boundary_rounds_without_new,
                boundary_patience,
            )
            if stop_scan:
                log_line(log_callback, stop_reason)
                break
        else:
            no_new_count = 0
            boundary_rounds_without_new = 0

        if len(comments) < max_count:
            # Scroll to bottom of page to trigger lazy loading of more comments
            page.evaluate("window.scrollTo(0, document.documentElement.scrollHeight)")
            # 拟人化随机间隔：固定节拍容易被平台识别，也让未渲染的评论有时间插入
            interruptible_sleep(random.uniform(float(scroll_pause), scroll_pause_max), stop_event)

    log_line(log_callback, f"  评论抓取完成：{len(comments)} 条。")
    return comments


import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

def _scrape_single_comment_task(
    index: int,
    tweet_url: str,
    cdp_port_or_url: str,
    scan_limit: int,
    comment_top_limit: int,
    page_load_timeout_val: int,
    scroll_pause: float,
    scroll_pause_max: float,
    no_new_scroll_limit: int,
    render_wait: float,
    boundary_patience: int,
    log_callback,
    stop_event,
    pause_event,
    writer,
    writer_lock,
    total_urls: int,
):
    from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError
    from src.core import (
        connect_existing_chromium,
        interruptible_sleep,
    )
    from src.platforms.x_twitter.tweet_metrics import clean_tweet_url

    if should_stop(stop_event):
        return
    if wait_if_paused(pause_event, stop_event):
        return

    normalized_url = clean_tweet_url(tweet_url)
    log_line(log_callback, f"[{index}/{total_urls}] 开始读取推文链接：{normalized_url}")
    playwright = None
    browser = None
    page = None
    try:
        playwright = sync_playwright().start()
        try:
            browser, context = connect_existing_chromium(playwright, cdp_port_or_url)
        except Exception as exc:
            log_error(log_callback, f"[{index}/{total_urls}] 无法连接 Chrome 浏览器：{exc}")
            return

        page = context.new_page()
        page.goto(normalized_url, wait_until="domcontentloaded", timeout=page_load_timeout_val)
        interruptible_sleep(2.5, stop_event)

        comments = extract_comments(
            page,
            normalized_url,
            scan_limit,
            log_callback,
            stop_event,
            scroll_pause=scroll_pause,
            scroll_pause_max=scroll_pause_max,
            no_new_scroll_limit=no_new_scroll_limit,
            render_wait=render_wait,
            boundary_patience=boundary_patience,
            pause_event=pause_event,
        )
        comments.sort(key=lambda item: int(item.get("likes", "0") or 0), reverse=True)

        with writer_lock:
            if not comments:
                row = {
                    "序号": str(index),
                    "推文链接": normalized_url,
                    "评论的点赞量": "",
                    "评论内容": "该推文无评论",
                    "评论发布时间": ""
                }
                writer.writerow(row)
            else:
                for comment in comments[:comment_top_limit]:
                    row = {
                        "序号": str(index),
                        "推文链接": normalized_url,
                        "评论的点赞量": comment.get("likes", "0"),
                        "评论内容": comment.get("content", ""),
                        "评论发布时间": comment.get("time", "")
                    }
                    writer.writerow(row)
        log_line(log_callback, f"[{index}/{total_urls}] 完成：成功扫描评论 {len(comments)} 条，数据已写入缓冲区。")
    except PlaywrightTimeoutError:
        log_error(log_callback, f"[{index}/{total_urls}] 错误：页面加载超时。")
    except Exception as exc:
        log_error(log_callback, f"[{index}/{total_urls}] 错误：处理失败，{exc}")
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

def run_x_top_comments_spider(
    txt_path: str,
    cdp_port_or_url: str,
    max_scan_comments: int,
    log_callback,
    finish_callback,
    stop_event=None,
    pause_event=None,
    config=None,
):
    """运行 X/Twitter 热门评论爬取任务的驱动函数。

    读取 txt_path 中的推文链接，连接 Chrome 浏览器并逐个抓取首层评论，
    按点赞数降序排序，最终将前 N 条评论保存至 Excel 文件中。
    """
    if config is None:
        config = {}
    comment_top_limit = int(config.get("comment_top_limit", 100))
    page_load_timeout_val = int(config.get("page_load_timeout", 30000))
    scroll_pause = float(config.get("scroll_interval", 1.5))
    scroll_pause_max = float(config.get("comment_scroll_max", max(scroll_pause, scroll_pause * 2)))
    no_new_scroll_limit = int(config.get("comment_no_new_scroll_limit", config.get("no_new_scroll_limit", 5)))
    render_wait = float(config.get("comment_render_wait", DEFAULT_RENDER_WAIT))
    boundary_patience = int(config.get("comment_boundary_patience", DEFAULT_BOUNDARY_PATIENCE))
    max_parallel_tabs = int(config.get("max_parallel_tabs", 3))

    completed_path = None
    try:
        from src.core import (
            XlsxRowWriter,
            build_output_path,
            ensure_chrome_for_cdp,
        )
        from src.platforms.x_twitter.tweet_metrics import parse_tweet_urls

        try:
            ensure_chrome_for_cdp(cdp_port_or_url, log_callback=log_callback)
        except Exception:
            pass

        tweet_urls = parse_tweet_urls(txt_path)
        if not tweet_urls:
            log_warn(log_callback, "TXT 中没有有效的推文链接。")
            return

        scan_limit = max(int(max_scan_comments), comment_top_limit)
        output_path = build_output_path("x", f"x_top_comments_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx")
        
        comment_fields = ["序号", "推文链接", "评论的点赞量", "评论内容", "评论发布时间"]
        writer = XlsxRowWriter(output_path, comment_fields)
        log_line(log_callback, f"输出文件位置：{output_path}")

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
            for index, tweet_url in enumerate(tweet_urls, 1):
                if should_stop(stop_event):
                    break
                if wait_if_paused(pause_event, stop_event):
                    break
                futures.append(
                    executor.submit(
                        _scrape_single_comment_task,
                        index,
                        tweet_url,
                        cdp_port_or_url,
                        scan_limit,
                        comment_top_limit,
                        page_load_timeout_val,
                        scroll_pause,
                        scroll_pause_max,
                        no_new_scroll_limit,
                        render_wait,
                        boundary_patience,
                        safe_log_callback,
                        stop_event,
                        pause_event,
                        writer,
                        writer_lock,
                        len(tweet_urls),
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
        log_line(safe_log_callback, f"任务全部完成，数据已妥善保存至：{output_path}")
    finally:
        if finish_callback:
            finish_callback(completed_path)

