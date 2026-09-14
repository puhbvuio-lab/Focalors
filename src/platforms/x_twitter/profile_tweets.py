from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from queue import Empty, Queue
import random
import re
import threading
import time

try:
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError, Error as PlaywrightError
    from playwright.sync_api import sync_playwright
except ModuleNotFoundError:
    PlaywrightTimeoutError = TimeoutError
    PlaywrightError = Exception
    sync_playwright = None

from src.core import (
    DEFAULT_X_CDP_URL,
    build_output_path,
    connect_existing_chromium,
    expand_compact_number,
    interruptible_sleep,
    log_error,
    log_line,
    log_warn,
    sanitize_csv_cell,
    should_stop,
    wait_if_paused,
)
from src.platforms.x_twitter.comments import extract_comments
from src.core.parallel import AtomicCounter, ThreadSafeWriter, normalize_parallel_windows
from src.core.task_checkpoint import (
    open_checkpointed_multi_sheet_writer,
    open_checkpointed_row_writer,
    open_task_checkpoint,
)
from src.platforms.x_twitter.page_recovery import (
    check_network_reachable,
    detect_x_transient_error,
    resolve_x_page_recovery_config,
    wait_for_x_page_recovery,
)


def _parse_date_range(start_str: str, end_str: str):
    from datetime import datetime
    start_dt = datetime.strptime(start_str, "%Y-%m-%d")
    end_dt = datetime.strptime(end_str, "%Y-%m-%d")
    if start_dt > end_dt:
        raise ValueError(f"开始日期 {start_str} 晚于结束日期 {end_str}")
    return start_dt, end_dt


CSV_FIELDS = ["序号", "帖子ID", "发布时间", "帖子内容", "浏览量", "点赞量", "转发量", "评论数", "帖子链接", "博主链接"]
PAGE_LOAD_TIMEOUT = 30000
INITIAL_LOAD_DELAY = 2.0
SCROLL_DELAY = 1.5
DEFAULT_SCROLL_DELAY_MIN = 2.4
DEFAULT_SCROLL_DELAY_MAX = 5.6
SCROLL_PX = 2800
NO_NEW_SCROLL_LIMIT = 10
DEFAULT_MAX_SCROLLS = 300
DEFAULT_PROFILE_TWEET_LIMIT = 50
DEFAULT_CONSECUTIVE_DATE_LIMIT = 3
GUARANTEE_MIN_SCROLLS = 15  # 保底滚动次数：即使无新内容也至少滚动这么多次
SAVE_BATCH_SIZE = 10
COOLDOWN_MIN_SECONDS = 1.5
COOLDOWN_MAX_SECONDS = 3.0


class XTransientProfileSkipped(RuntimeError):
    """Raised when a profile is skipped after X shows a reload/error page."""

    def __init__(self, message: str, retry_after_success: bool = True) -> None:
        super().__init__(message)
        self.retry_after_success = retry_after_success


BLOCKED_PROFILE_NAMES = {
    "home",
    "explore",
    "notifications",
    "messages",
    "i",
    "search",
    "settings",
    "signup",
    "login",
}


def clean_profile_url(url: str) -> str:
    value = (url or "").strip().replace("twitter.com", "x.com")
    if not value:
        return ""
    if value.startswith("//"):
        value = "https:" + value
    if value.startswith("/"):
        value = "https://x.com" + value
    if not value.startswith("http"):
        value = "https://" + value
    return value.split("?")[0].split("#")[0].rstrip("/")


def extract_profile_username(profile_url: str) -> str:
    match = re.match(r"https?://(?:www\.)?x\.com/([^/?#]+)", clean_profile_url(profile_url), re.I)
    if not match:
        return ""
    username = match.group(1).strip().strip("@")
    if username.lower() in BLOCKED_PROFILE_NAMES:
        return ""
    return username


def parse_profile_urls(text: str) -> list[str]:
    urls: list[str] = []
    seen = set()
    for line in (text or "").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        url = clean_profile_url(stripped.split()[0])
        username = extract_profile_username(url)
        if username and url not in seen:
            urls.append(url)
            seen.add(url)
    return urls


def format_tweet_time(raw_time: str) -> str:
    value = (raw_time or "").strip()
    if not value:
        return ""
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return value


def normalize_tweet(tweet: dict[str, str]) -> dict[str, str]:
    post_id = str(tweet.get("postId") or tweet.get("post_id") or "")
    return {
        "post_id": str(sanitize_csv_cell(post_id)),
        "published_at": str(sanitize_csv_cell(format_tweet_time(tweet.get("publishedAt", tweet.get("published_at", ""))))),
        "content": str(sanitize_csv_cell(tweet.get("content", ""))),
        "views": str(sanitize_csv_cell(expand_compact_number(tweet.get("views", "")))),
        "likes": str(sanitize_csv_cell(expand_compact_number(tweet.get("likes", "")))),
        "retweets": str(sanitize_csv_cell(expand_compact_number(tweet.get("retweets", "")))),
        "replies": str(sanitize_csv_cell(expand_compact_number(tweet.get("replies", "")))),
        "url": str(sanitize_csv_cell(tweet.get("url", ""))),
    }


def row_from_tweet(index: int, tweet: dict[str, str]) -> dict[str, str]:
    return {
        "序号": str(index),
        "帖子ID": tweet.get("post_id") or tweet.get("postId", ""),
        "发布时间": tweet.get("published_at") or tweet.get("publishedAt", ""),
        "帖子内容": tweet.get("content", ""),
        "浏览量": tweet.get("views", ""),
        "点赞量": tweet.get("likes", ""),
        "转发量": tweet.get("retweets", ""),
        "评论数": tweet.get("replies", ""),
        "帖子链接": tweet.get("url", ""),
        "博主链接": tweet.get("profile_url", ""),
    }


def cooldown_after_batch(total_written: int, log_callback, stop_event=None, pause_event=None, save_batch_size=None, cooldown_min=None, cooldown_max=None):
    if save_batch_size is None:
        save_batch_size = SAVE_BATCH_SIZE
    if cooldown_min is None:
        cooldown_min = COOLDOWN_MIN_SECONDS
    if cooldown_max is None:
        cooldown_max = COOLDOWN_MAX_SECONDS
    if total_written <= 0 or total_written % save_batch_size != 0:
        return
    seconds = random.uniform(cooldown_min, cooldown_max)
    log_line(log_callback, f"  已保存 {total_written} 条帖子，随机等待 {seconds:.1f} 秒。")
    deadline = time.time() + seconds
    while time.time() < deadline:
        if should_stop(stop_event):
            break
        if wait_if_paused(pause_event, stop_event):
            break
        time.sleep(min(0.5, deadline - time.time()))


def extract_post_count(page) -> int | None:
    return page.evaluate(
        """() => {
            try {
                const elements = Array.from(document.querySelectorAll('h2, div, span, p'));
                for (const h of elements) {
                    const text = (h.innerText || h.textContent || '').trim();
                    if (text.length > 25) continue;
                    
                    const match = text.match(/^(\\d[\\d,.]*[KkMm]?)\\s*(posts?|帖子|ポスト|件のポスト)$/i);
                    if (match) {
                        let numStr = match[1].toUpperCase();
                        let multiplier = 1;
                        if (numStr.includes('K')) { multiplier = 1000; numStr = numStr.replace('K', ''); }
                        else if (numStr.includes('M')) { multiplier = 1000000; numStr = numStr.replace('M', ''); }
                        numStr = numStr.replace(/,/g, '');
                        return Math.floor(parseFloat(numStr) * multiplier);
                    }
                }
            } catch (e) {}
            return null;
        }"""
    )


def extract_visible_profile_tweets(page, username: str) -> list[dict[str, str]]:
    username_lc = username.lower().lstrip("@")
    return page.evaluate(
        """async ({ username }) => {
            const results = [];
            const normalize = value => (value || '').trim().replace(/^@/, '').toLowerCase();
            const ownStatus = article => {
                const time = article.querySelector('time');
                const link = time ? time.closest('a[href*="/status/"]') : null;
                const href = link ? link.getAttribute('href') : '';
                const match = href.match(/\\/status\\/(\\d+)/);
                let handle = '';
                try {
                    const url = new URL(href, location.origin);
                    handle = (url.pathname.split('/').filter(Boolean)[0] || '').trim();
                } catch (error) {}
                return { href, postId: match ? match[1] : '', handle };
            };
            const isPromoted = article => {
                const text = (article.innerText || '').split('\\n').map(x => x.trim().toLowerCase());
                return text.some(line => ['ad', 'promoted', '广告', '推广'].includes(line));
            };
            const nonTextContent = article => {
                const types = [];
                if (article.querySelector('[data-testid="tweetPhoto"], img[src*="/media/"]')) types.push('图片');
                if (article.querySelector('video')) types.push('视频');
                if ((article.innerText || '').split('\\n').some(line => line.trim().toLowerCase() === 'gif')) types.push('GIF');
                if (article.querySelector('[data-testid="card.wrapper"], [data-testid="card.layoutLarge.media"], [data-testid="card.layoutSmall.media"]')) types.push('卡片');
                return types.length ? `[${types.join('+')}]` : '[非文本]';
            };
            const ariaMetric = (root, testIds) => {
                for (const id of testIds) {
                    const el = root.querySelector(`[data-testid="${id}"]`);
                    if (!el) continue;
                    const rawText = (el.innerText || el.textContent || '').trim();
                    if (rawText && /\\d/.test(rawText)) return rawText;
                    const aria = el.getAttribute('aria-label') || '';
                    const match = aria ? aria.match(/([\\d,]+(\\.\\d+)?\\s*[KkMmBb]?)/) : null;
                    if (match) return match[1].replace(/,/g, '');
                }
                return '';
            };
            const firstMetric = (root, selectors) => {
                for (const selector of selectors) {
                    const el = root.querySelector(selector);
                    if (!el) continue;
                    const rawText = (el.innerText || el.textContent || '').trim();
                    if (rawText && /\\d/.test(rawText)) return rawText;
                    const aria = el.getAttribute('aria-label') || '';
                    const match = aria ? aria.match(/([\\d,]+(\\.\\d+)?\\s*[KkMmBb]?)/) : null;
                    if (match) return match[1].replace(/,/g, '');
                }
                return '';
            };

            // Phase 1: collect all articles on profile timeline
            const articles = [];
            for (const article of document.querySelectorAll('article[data-testid="tweet"], article')) {
                try {
                    if (isPromoted(article)) continue;
                    const info = ownStatus(article);
                    if (!info.postId) continue;

                    const socialEl = article.querySelector('[data-testid="socialContext"]');
                    const socialText = socialEl ? (socialEl.innerText || socialEl.textContent || '').trim().toLowerCase() : '';
                    const isRepost = /repost|reposted|retweet|retweeted|republished|转推|转发|リポスト|リツイート|再投稿|已轉推/.test(socialText);

                    // 在主页时间线上：非转推帖子必须是博主本人的，转推帖子可以是任意作者的
                    if (!isRepost && normalize(info.handle) !== username) continue;

                    const timeEl = article.querySelector('time');
                    const publishedAt = timeEl ? (timeEl.getAttribute('datetime') || '') : '';
                    const href = info.href.startsWith('http') ? info.href : `https://x.com${info.href}`;
                    articles.push({ article, postId: info.postId, publishedAt, href, isRepost });
                } catch (error) {}
            }

            // Wait for React to re-render with original text
            if (articles.length > 0) {
                await new Promise(r => setTimeout(r, 500));
            }

            // Phase 2: read text and metrics
            for (const { article, postId, publishedAt, href, isRepost } of articles) {
                try {
                    const textEl = article.querySelector('[data-testid="tweetText"]');
                    const text = textEl ? (textEl.innerText || textEl.textContent || '').trim() : '';
                    results.push({
                        postId,
                        publishedAt,
                        content: text || nonTextContent(article),
                        url: href,
                        isRepost: isRepost || false,
                        views: firstMetric(article, [
                            'a[href*="/analytics"]',
                            'div[data-testid="postViewCount"]',
                            '[aria-label*="Views"]',
                            '[aria-label*="views"]',
                            '[aria-label*="浏览"]',
                            '[aria-label*="表示"]',
                        ]) || '',
                        likes: ariaMetric(article, ['like', 'unlike']) || '',
                        retweets: ariaMetric(article, ['retweet', 'unretweet']) || '',
                        replies: ariaMetric(article, ['reply']) || '',
                    });
                } catch (error) {}
            }
            return results;
        }""",
        {"username": username_lc},
    )


def collect_profile_tweets(
    page,
    detail_page,
    profile_url: str,
    max_scrolls: int,
    limit_time_bool: bool,
    start_dt,
    end_dt,
    get_comments_bool: bool,
    max_comments: int,
    log_callback,
    stop_event=None,
    writer=None,
    row_offset: int = 0,
    page_timeout=None,
    scroll_delay=None,
    scroll_delay_min=None,
    scroll_delay_max=None,
    no_new_scroll_limit=None,
    save_batch_size=None,
    cooldown_min=None,
    cooldown_max=None,
    pause_event=None,
    keyword: str | None = None,
    max_collect: int | None = None,
    scroll_px=None,
    initial_load_delay=None,
    consecutive_date_limit=None,
    guarantee_min_scrolls=None,
    page_already_loaded: bool = False,
    date_window_size: int = 20,
    include_reposts: bool = True,
    recovery_config=None,
    use_search_entry: bool = False,
    transient_skip_state: dict[str, int] | None = None,
    transient_retry: bool = False,
) -> list[dict[str, str]] | tuple[list[dict[str, str]], int, int]:
    if page_timeout is None:
        page_timeout = PAGE_LOAD_TIMEOUT
    if scroll_delay is None:
        scroll_delay = SCROLL_DELAY
    if scroll_delay_min is None:
        scroll_delay_min = scroll_delay
    if scroll_delay_max is None:
        scroll_delay_max = scroll_delay
    scroll_delay_min, scroll_delay_max = normalize_scroll_delay_range(
        {"scroll_interval_min": scroll_delay_min, "scroll_interval_max": scroll_delay_max},
        fallback=scroll_delay,
    )
    if scroll_px is None:
        scroll_px = SCROLL_PX
    if initial_load_delay is None:
        initial_load_delay = INITIAL_LOAD_DELAY
    if no_new_scroll_limit is None:
        no_new_scroll_limit = NO_NEW_SCROLL_LIMIT
    if save_batch_size is None:
        save_batch_size = SAVE_BATCH_SIZE
    if cooldown_min is None:
        cooldown_min = COOLDOWN_MIN_SECONDS
    if cooldown_max is None:
        cooldown_max = COOLDOWN_MAX_SECONDS
    if guarantee_min_scrolls is None:
        guarantee_min_scrolls = GUARANTEE_MIN_SCROLLS

    username = extract_profile_username(profile_url)
    if not username:
        raise ValueError(f"无效的 X 博主主页链接：{profile_url}")

    if keyword:
        import urllib.parse
        search_query = f"from:{username} {keyword}"
        target_url = f"https://x.com/search?q={urllib.parse.quote(search_query)}&src=typed_query&f=live"
    else:
        target_url = clean_profile_url(profile_url)

    tweets: list[dict[str, str]] = []
    pending_rows: list[dict[str, str]] = []
    written_count = 0
    seen_ids = set()
    no_new_count = 0
    stopped_by_date = False
    max_scrolls = max(1, int(max_scrolls or DEFAULT_MAX_SCROLLS))
    prev_page_height = 0  # 上一次滚动后的页面高度
    dom_changed_streak = 0  # DOM持续变化但无新帖文的连续计数
    # 滑动窗口：记录最近 N 条帖子是否在时间范围内
    date_window: list[bool] = []  # True=在范围内, False=在范围外

    try:
        if not page_already_loaded:
            if target_url:
                page.goto(target_url, wait_until="domcontentloaded", timeout=page_timeout)
            elif not navigate_to_profile(
                page,
                profile_url,
                log_callback,
                page_timeout=page_timeout,
                stop_event=stop_event,
                pause_event=pause_event,
                initial_delay=initial_load_delay,
                recovery_config=recovery_config,
                use_search_entry=use_search_entry,
            ):
                raise RuntimeError(f"未成功跳转至博主主页 {profile_url}")
            try:
                page.wait_for_selector('article[data-testid="tweet"], article', timeout=page_timeout)
            except PlaywrightTimeoutError:
                if not handle_empty_profile_tweets_recovery(
                    page,
                    username,
                    log_callback=log_callback,
                    page_timeout=page_timeout,
                    stop_event=stop_event,
                    pause_event=pause_event,
                    recovery_config=recovery_config,
                    transient_skip_state=transient_skip_state,
                    transient_retry=transient_retry,
                ):
                    if writer:
                        return tweets, row_offset, written_count
                    return tweets
            interruptible_sleep(initial_load_delay, stop_event)
        else:
            # 页面已由调用方加载，只需等待渲染完成
            try:
                page.wait_for_selector('article[data-testid="tweet"], article', timeout=page_timeout)
            except PlaywrightTimeoutError:
                if not handle_empty_profile_tweets_recovery(
                    page,
                    username,
                    log_callback=log_callback,
                    page_timeout=page_timeout,
                    stop_event=stop_event,
                    pause_event=pause_event,
                    recovery_config=recovery_config,
                    transient_skip_state=transient_skip_state,
                    transient_retry=transient_retry,
                ):
                    if writer:
                        return tweets, row_offset, written_count
                    return tweets
            interruptible_sleep(initial_load_delay, stop_event)
    except PlaywrightTimeoutError:
        if keyword:
            log_warn(log_callback, f"    搜索无结果或加载超时，跳过关键词 '{keyword}'。")
            if writer:
                return tweets, row_offset, written_count
            return tweets
        raise
    log_line(log_callback, f"  开始采集 @{username} 主页帖子，最多滚动 {max_scrolls} 次。")

    for scroll_index in range(max_scrolls):
        if should_stop(stop_event):
            break
        if wait_if_paused(pause_event, stop_event):
            break

        for text_lbl in ['view original', '查看原文', '原文を表示', 'show original', '原文を見る', 'show more', 'show more...', 'もっと見る', '더 보기', '显示更多']:
            try:
                locs = page.locator(f"article >> text='{text_lbl}'").all()
                for loc in locs:
                    try:
                        loc.click(timeout=500)
                    except (PlaywrightTimeoutError, PlaywrightError):
                        pass
            except (PlaywrightTimeoutError, PlaywrightError):
                pass
            
        visible_tweets = extract_visible_profile_tweets(page, username)
        if not visible_tweets:
            if not handle_empty_profile_tweets_recovery(
                page,
                username,
                log_callback=log_callback,
                page_timeout=page_timeout,
                stop_event=stop_event,
                pause_event=pause_event,
                recovery_config=recovery_config,
                transient_skip_state=transient_skip_state,
                transient_retry=transient_retry,
            ):
                if writer:
                    return tweets, row_offset, written_count
                return tweets
        added = 0
        for tweet in visible_tweets:
            post_id = str(tweet.get("postId") or "")
            if not post_id or post_id in seen_ids:
                continue
            seen_ids.add(post_id)
            normalized_tweet = normalize_tweet(tweet)
            is_repost = tweet.get("isRepost", False)

            # 时间过滤：滑动窗口策略
            # 只要最近 date_window_size 条帖子中有一条在时间范围内，就继续滚动
            if limit_time_bool:
                pub_time = normalized_tweet.get("published_at")
                in_range = False
                skip_collect = False
                if pub_time:
                    try:
                        pub_dt = datetime.strptime(pub_time, "%Y-%m-%d %H:%M:%S")
                        in_range = start_dt.date() <= pub_dt.date() <= end_dt.date()
                        if not in_range and not is_repost:
                            skip_collect = True  # 原创帖不在范围内，跳过采集
                    except (ValueError, TypeError):
                        pass
                else:
                    # 无法解析日期的原创帖，视为范围外
                    if not is_repost:
                        skip_collect = True

                # 所有非转推帖子都进窗口（转推不进窗口，它们不限于时间范围）
                if not is_repost:
                    date_window.append(in_range)
                    if len(date_window) > date_window_size:
                        date_window.pop(0)
                    if len(date_window) >= date_window_size and not any(date_window):
                        stopped_by_date = True
                        break

                if skip_collect:
                    continue
                    
            normalized_tweet["profile_url"] = profile_url
            tweets.append(normalized_tweet)
            added += 1
            if writer:
                row_offset += 1
                row = row_from_tweet(row_offset, normalized_tweet)
                pending_rows.append(row)
                
                if get_comments_bool:
                    try:
                        detail_page.goto(normalized_tweet["url"], wait_until="domcontentloaded", timeout=30000)
                        detail_page.wait_for_selector('article[data-testid="tweet"]', timeout=30000)
                        interruptible_sleep(2, stop_event)
                        comments = extract_comments(detail_page, normalized_tweet["url"], max_comments, log_callback, stop_event, pause_event=pause_event)
                        comment_rows = []
                        for comment in comments:
                            comment_row = {
                                "序号": str(row_offset),
                                "推文链接": normalized_tweet["url"],
                                "评论的点赞量": comment.get("likes", ""),
                                "评论内容": comment.get("content", ""),
                                "评论发布时间": comment.get("time", "")
                            }
                            comment_rows.append(comment_row)
                        if comment_rows:
                            writer.writerows("评论信息", comment_rows)
                    except Exception as exc:
                        log_warn(log_callback, f"    提取评论失败：{exc}")
                
                if len(pending_rows) >= save_batch_size:
                    if hasattr(writer, "worksheets"):
                        writer.writerows("推文信息", pending_rows)
                    else:
                        writer.writerows(pending_rows)
                    written_count += len(pending_rows)
                    pending_rows.clear()
                    cooldown_after_batch(written_count, log_callback, stop_event, pause_event=pause_event, save_batch_size=save_batch_size, cooldown_min=cooldown_min, cooldown_max=cooldown_max)
                    if should_stop(stop_event):
                        break

            if max_collect is not None and len(tweets) >= max_collect:
                break

        if stopped_by_date:
            log_line(log_callback, f"  最近 {date_window_size} 条帖子均不在时间范围内，停止滚动。")
            break

        # ---------- DOM 高度变化检测 ----------
        try:
            current_page_height = page.evaluate("document.documentElement.scrollHeight")
        except Exception:
            current_page_height = prev_page_height
        page_height_changed = (current_page_height != prev_page_height)
        if page_height_changed and prev_page_height > 0:
            dom_changed_streak += 1
        else:
            dom_changed_streak = 0

        if added:
            log_line(log_callback, f"  滚动 {scroll_index + 1}/{max_scrolls}：新增 {added} 条，累计 {len(tweets)} 条。")
            no_new_count = 0
            dom_changed_streak = 0
        elif page_height_changed and prev_page_height > 0:
            # 页面高度变化说明有新内容正在加载，但本次尚未提取到新帖文
            # 不算入连续无新增，给予额外等待后重新提取
            log_line(log_callback, f"  滚动 {scroll_index + 1}/{max_scrolls}：页面有新内容加载中（高度 {prev_page_height}→{current_page_height}），等待渲染后重新提取...")
            interruptible_sleep(3.0, stop_event)
            # 重新提取一次，React 渲染可能需要更多时间
            retry_tweets = extract_visible_profile_tweets(page, username)
            retry_added = 0
            for tweet in retry_tweets:
                post_id = str(tweet.get("postId") or "")
                if not post_id or post_id in seen_ids:
                    continue
                seen_ids.add(post_id)
                normalized_tweet = normalize_tweet(tweet)
                is_repost = tweet.get("isRepost", False)
                if limit_time_bool:
                    pub_time = normalized_tweet.get("published_at")
                    in_range = False
                    skip_collect = False
                    if pub_time:
                        try:
                            pub_dt = datetime.strptime(pub_time, "%Y-%m-%d %H:%M:%S")
                            in_range = start_dt.date() <= pub_dt.date() <= end_dt.date()
                            if not in_range and not is_repost:
                                skip_collect = True
                        except (ValueError, TypeError):
                            pass
                    else:
                        if not is_repost:
                            skip_collect = True
                    if not is_repost:
                        date_window.append(in_range)
                        if len(date_window) > date_window_size:
                            date_window.pop(0)
                        if len(date_window) >= date_window_size and not any(date_window):
                            stopped_by_date = True
                            break
                    if skip_collect:
                        continue
                normalized_tweet["profile_url"] = profile_url
                tweets.append(normalized_tweet)
                retry_added += 1
                if writer:
                    row_offset += 1
                    row = row_from_tweet(row_offset, normalized_tweet)
                    pending_rows.append(row)
            if stopped_by_date:
                log_line(log_callback, f"  最近 {date_window_size} 条帖子均不在时间范围内，停止滚动。")
                break
            if retry_added:
                log_line(log_callback, f"  重新提取成功：新增 {retry_added} 条，累计 {len(tweets)} 条。")
                no_new_count = 0
                dom_changed_streak = 0
            elif dom_changed_streak >= 5:
                log_warn(log_callback, f"  注意：页面连续 {dom_changed_streak} 次滚动均有高度变化但未提取到新帖文，可能存在渲染异常。")
        else:
            no_new_count += 1
            # 保底滚动：未达到保底次数前不提前退出
            if no_new_count >= no_new_scroll_limit and (scroll_index + 1) >= guarantee_min_scrolls:
                log_warn(log_callback, f"  连续 {no_new_scroll_limit} 次没有新增帖子（已滚动 {scroll_index + 1} 次），停止。")
                break
            elif no_new_count >= no_new_scroll_limit and (scroll_index + 1) < guarantee_min_scrolls:
                log_line(log_callback, f"  连续 {no_new_scroll_limit} 次无新增，但保底滚动次数未满（{scroll_index + 1}/{guarantee_min_scrolls}），继续滚动...")
                no_new_count = 0  # 重置计数，给予更多机会

        prev_page_height = current_page_height

        if max_collect is not None and len(tweets) >= max_collect:
            log_line(log_callback, f"  达到指定采集上限 {max_collect} 条，停止滚动。")
            break

        if should_stop(stop_event):
            break

        page.evaluate(f"window.scrollBy(0, {scroll_px})")
        interruptible_sleep(scroll_delay + 1.0 if no_new_count else scroll_delay, stop_event)

    if writer and pending_rows:
        if hasattr(writer, "worksheets"):
            writer.writerows("推文信息", pending_rows)
        else:
            writer.writerows(pending_rows)
        written_count += len(pending_rows)
        pending_rows.clear()

    if writer:
        return tweets, row_offset, written_count
    return tweets


def build_rows(tweets: list[dict[str, str]]) -> list[dict[str, str]]:
    rows = []
    for index, tweet in enumerate(tweets, 1):
        rows.append(row_from_tweet(index, tweet))
    return rows


class ThreadSafeWriterWrapper:
    def __init__(self, writer, lock):
        self.writer = writer
        self.lock = lock
        worksheets = getattr(writer, "worksheets", None)
        worksheet = worksheets.get("推文信息") if isinstance(worksheets, dict) else getattr(writer, "worksheet", None)
        try:
            self.global_seq = max(0, int(worksheet.max_row) - 1)
        except (AttributeError, TypeError, ValueError):
            self.global_seq = 0
        self.url_to_seq = {}

    def writerow(self, *args, **kwargs):
        with self.lock:
            if len(args) == 2 and isinstance(args[0], str):
                sheet_name, row = args[0], args[1]
                self._process_row(sheet_name, row)
                self.writer.writerow(sheet_name, row)
            else:
                row = args[0]
                self._process_row(None, row)
                self.writer.writerow(row)

    def writerows(self, *args, **kwargs):
        with self.lock:
            if len(args) == 2 and isinstance(args[0], str):
                sheet_name, rows = args[0], args[1]
                for row in rows:
                    self._process_row(sheet_name, row)
                self.writer.writerows(sheet_name, rows)
            else:
                rows = args[0]
                for row in rows:
                    self._process_row(None, row)
                self.writer.writerows(rows)

    def _process_row(self, sheet_name, row):
        if sheet_name == "推文信息" or (sheet_name is None and "帖子链接" in row):
            url = row.get("帖子链接") or row.get("url", "")
            if url:
                self.global_seq += 1
                self.url_to_seq[url] = str(self.global_seq)
                row["序号"] = str(self.global_seq)
        elif sheet_name == "评论信息":
            url = row.get("推文链接")
            if url and url in self.url_to_seq:
                row["序号"] = self.url_to_seq[url]

    def save(self):
        with self.lock:
            self.writer.save()

    @property
    def worksheet(self):
        return self.writer.worksheet

    @property
    def worksheets(self):
        return self.writer.worksheets

def _scrape_single_profile_tweets_task(
    profile_url: str,
    keywords_text: str,
    limit_time_bool: bool,
    start_dt,
    end_dt,
    get_comments_bool: bool,
    max_comments_val: int,
    cdp_port_or_url: str,
    max_scrolls: int,
    log_callback,
    stop_event,
    writer,
    writer_lock,
    page_load_timeout_val: int,
    scroll_delay_min_val: float,
    scroll_delay_max_val: float,
    no_new_scroll_limit_val: int,
    save_batch_size_val: int,
    cooldown_min_val: float,
    cooldown_max_val: float,
    scroll_px_val: int,
    initial_load_delay_val: float,
    pause_event,
    guarantee_min_scrolls_val: int,
    date_window_size: int,
    profile_index: int,
    total_profiles: int,
    truncate_threshold: int,
    browser_choice: str | None = None,
    recovery_config: dict | None = None,
    use_search_entry: bool = False,
    max_collect: int | None = None,
    checkpoint=None,
    output_path: str | None = None,
):
    from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError
    from src.core import interruptible_sleep
    from src.platforms.x_twitter.profile_tweets import (
        extract_profile_username,
        extract_post_count,
        collect_profile_tweets,
    )

    if should_stop(stop_event):
        return
    if wait_if_paused(pause_event, stop_event):
        return

    username = extract_profile_username(profile_url)
    log_line(log_callback, f"[{profile_index}/{total_profiles}] 开始处理博主主页：{profile_url}")

    playwright = None
    browser = None
    page = None
    detail_page = None
    checkpoint_claimed = False
    task_succeeded = False
    total_written = 0

    try:
        if checkpoint is not None:
            checkpoint_claimed, claim_status = checkpoint.claim_item(profile_url)
            if not checkpoint_claimed:
                log_line(log_callback, f"[{profile_index}/{total_profiles}] 断点跳过 ({claim_status})：{profile_url}")
                return

        playwright = sync_playwright().start()
        try:
            browser, context = connect_existing_chromium(playwright, cdp_port_or_url, browser=browser_choice)
        except Exception as exc:
            log_error(log_callback, f"[{profile_index}/{total_profiles}] 无法连接浏览器：{exc}")
            return

        page = context.new_page()
        detail_page = context.new_page() if get_comments_bool else None

        parsed_kws = [k.strip() for k in keywords_text.splitlines() if k.strip()]
        keyword_list = parsed_kws if parsed_kws else []

        if not navigate_to_profile(
            page,
            profile_url,
            log_callback,
            page_timeout=page_load_timeout_val,
            stop_event=stop_event,
            pause_event=pause_event,
            initial_delay=initial_load_delay_val,
            recovery_config=recovery_config,
            use_search_entry=use_search_entry,
        ):
            raise RuntimeError(f"未成功跳转至博主主页 {profile_url}")
        post_count = None
        for attempt in range(10):
            post_count = extract_post_count(page)
            if post_count is not None:
                break
            if interruptible_sleep(0.5, stop_event):
                break

        if post_count is None:
            log_line(log_callback, f"[{profile_index}/{total_profiles}] 无法提取博主帖文数量，按全量模式正常采集。")
        else:
            log_line(log_callback, f"[{profile_index}/{total_profiles}] 博主帖文数量：{post_count}")

        row_offset = 0

        use_truncate = False
        if isinstance(post_count, (int, float)) and post_count > truncate_threshold:
            use_truncate = True

        if not use_truncate:
            _, _, written_count = collect_profile_tweets(
                page, detail_page, profile_url, max_scrolls, limit_time_bool, start_dt, end_dt, get_comments_bool, max_comments_val, log_callback, stop_event, writer=writer, row_offset=row_offset, page_timeout=page_load_timeout_val, scroll_delay_min=scroll_delay_min_val, scroll_delay_max=scroll_delay_max_val, no_new_scroll_limit=no_new_scroll_limit_val, save_batch_size=save_batch_size_val, cooldown_min=cooldown_min_val, cooldown_max=cooldown_max_val, scroll_px=scroll_px_val, initial_load_delay=initial_load_delay_val, pause_event=pause_event, keyword=None, max_collect=max_collect, guarantee_min_scrolls=guarantee_min_scrolls_val, page_already_loaded=True, date_window_size=date_window_size
            )
            total_written += written_count
            log_line(log_callback, f"[{profile_index}/{total_profiles}] 完成 @{username} 全量采集：写入 {written_count} 条帖子。")
        else:
            log_line(log_callback, f"[{profile_index}/{total_profiles}] 帖文数大于 {truncate_threshold}，首先采集前 {truncate_threshold} 条帖子...")
            _, _, written_count = collect_profile_tweets(
                page, detail_page, profile_url, max_scrolls, limit_time_bool, start_dt, end_dt, get_comments_bool, max_comments_val, log_callback, stop_event, writer=writer, row_offset=row_offset, page_timeout=page_load_timeout_val, scroll_delay_min=scroll_delay_min_val, scroll_delay_max=scroll_delay_max_val, no_new_scroll_limit=no_new_scroll_limit_val, save_batch_size=save_batch_size_val, cooldown_min=cooldown_min_val, cooldown_max=cooldown_max_val, scroll_px=scroll_px_val, initial_load_delay=initial_load_delay_val, pause_event=pause_event, keyword=None, max_collect=truncate_threshold, guarantee_min_scrolls=guarantee_min_scrolls_val, page_already_loaded=True, date_window_size=date_window_size
            )
            total_written += written_count
            log_line(log_callback, f"[{profile_index}/{total_profiles}] 完成截断采集：新增写入 {written_count} 条帖子。")

            if keyword_list:
                log_line(log_callback, f"[{profile_index}/{total_profiles}] 开始按关键词进行补充采集 (共 {len(keyword_list)} 个关键词)...")
                for kw in keyword_list:
                    if should_stop(stop_event):
                        break
                    log_line(log_callback, f"[{profile_index}/{total_profiles}] -> 补充采集关键词: {kw}")
                    _, _, kw_written = collect_profile_tweets(
                        page, detail_page, profile_url, max_scrolls, limit_time_bool, start_dt, end_dt, get_comments_bool, max_comments_val, log_callback, stop_event, writer=writer, row_offset=row_offset, page_timeout=page_load_timeout_val, scroll_delay_min=scroll_delay_min_val, scroll_delay_max=scroll_delay_max_val, no_new_scroll_limit=no_new_scroll_limit_val, save_batch_size=save_batch_size_val, cooldown_min=cooldown_min_val, cooldown_max=cooldown_max_val, scroll_px=scroll_px_val, initial_load_delay=initial_load_delay_val, pause_event=pause_event, keyword=kw, max_collect=max_collect, guarantee_min_scrolls=guarantee_min_scrolls_val, page_already_loaded=False, date_window_size=date_window_size
                    )
                    total_written += kw_written
                    log_line(log_callback, f"[{profile_index}/{total_profiles}] 完成关键词 '{kw}' 采集：新增写入 {kw_written} 条。")
            else:
                log_line(log_callback, f"[{profile_index}/{total_profiles}] 未提供补充搜索关键词，跳过补充采集阶段。")

        if should_stop(stop_event):
            return
        task_succeeded = True
        if checkpoint is not None:
            checkpoint.mark_completed(
                profile_url,
                {
                    "output_path": output_path,
                    "profile_index": profile_index,
                    "written_count": total_written,
                },
            )

    except PlaywrightTimeoutError:
        log_warn(log_callback, f"[{profile_index}/{total_profiles}] 跳过：页面加载超时，请确认链接可打开且账号已登录。")
    except Exception as exc:
        log_warn(log_callback, f"[{profile_index}/{total_profiles}] 跳过：{exc}")
    finally:
        if checkpoint is not None and checkpoint_claimed and not task_succeeded:
            try:
                checkpoint.release_item(profile_url)
            except Exception:
                pass
        # 连接此时仍然存活（playwright.stop() 延后到最后），
        # page.close() 才能真正发送 Target.closeTarget 关闭真实标签页。
        for opened_page in (page, detail_page):
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


def collect_profile_tweets_with_parallel_windows(
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
    page_load_timeout_val: int = PAGE_LOAD_TIMEOUT,
    scroll_delay_min_val: float = DEFAULT_SCROLL_DELAY_MIN,
    scroll_delay_max_val: float = DEFAULT_SCROLL_DELAY_MAX,
    no_new_scroll_limit_val: int = NO_NEW_SCROLL_LIMIT,
    max_scrolls_val: int = DEFAULT_MAX_SCROLLS,
    max_tweets_per_author: int = DEFAULT_PROFILE_TWEET_LIMIT,
    save_batch_size_val: int = SAVE_BATCH_SIZE,
    cooldown_min_val: float = COOLDOWN_MIN_SECONDS,
    cooldown_max_val: float = COOLDOWN_MAX_SECONDS,
    scroll_px_val: int = SCROLL_PX,
    initial_load_delay_val: float = INITIAL_LOAD_DELAY,
    consecutive_date_limit_val: int = DEFAULT_CONSECUTIVE_DATE_LIMIT,
    guarantee_min_scrolls_val: int = GUARANTEE_MIN_SCROLLS,
    date_window_size: int = 20,
    search_entry_enabled: bool = False,
) -> int:
    if not profile_urls:
        return 0
    worker_count = min(normalize_parallel_windows({"parallel_windows": parallel_windows}), len(profile_urls))
    safe_writer = ThreadSafeWriter(writer)
    raw_writer = safe_writer.raw
    start_row = 0
    if hasattr(raw_writer, "worksheet"):
        start_row = max(0, raw_writer.worksheet.max_row - 1)
    row_counter = AtomicCounter(start_row)
    completed_counter = AtomicCounter(0)

    work_queue: Queue[tuple[int, str]] = Queue()
    for index, profile_url in enumerate(profile_urls, 1):
        work_queue.put((index, profile_url))

    log_line(log_callback, f"Parallel X profile windows enabled: {worker_count}. Runtime locks prevent duplicate profile pages.")

    def _worker(worker_index: int) -> int:
        owner_id = f"{checkpoint.run_id}:x-profile-window-{worker_index}"
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

                    profile_url = clean_profile_url(raw_profile_url)
                    profile_lock_key = profile_url.lower()
                    try:
                        username = extract_profile_username(profile_url)
                        claimed, claim_status = checkpoint.claim_item(profile_url)
                        if not claimed:
                            log_line(log_callback, f"[W{worker_index} {profile_index}/{len(profile_urls)}] skip {claim_status}: {profile_url}")
                            continue

                        runtime_claimed, _ = checkpoint.claim_runtime_item(
                            profile_lock_key,
                            namespace="x_profile_page",
                            owner_id=owner_id,
                        )
                        if not runtime_claimed:
                            checkpoint.release_item(profile_url)
                            log_line(log_callback, f"[W{worker_index} {profile_index}/{len(profile_urls)}] profile active in another window, skip this run: {profile_url}")
                            continue

                        log_line(log_callback, f"[W{worker_index} {profile_index}/{len(profile_urls)}] profile: {profile_url}")
                        if not navigate_to_profile(
                            page,
                            profile_url,
                            log_callback,
                            page_timeout=page_load_timeout_val,
                            stop_event=stop_event,
                            pause_event=pause_event,
                            initial_delay=initial_load_delay_val,
                            recovery_config=config,
                            use_search_entry=search_entry_enabled,
                        ):
                            log_warn(log_callback, f"  [W{worker_index}] profile navigation failed: {profile_url}")
                            checkpoint.release_item(profile_url)
                            continue

                        tweets = collect_profile_tweets(
                            page,
                            None,
                            profile_url,
                            max_scrolls_val,
                            False,
                            None,
                            None,
                            False,
                            0,
                            log_callback,
                            stop_event,
                            writer=None,
                            row_offset=0,
                            page_timeout=page_load_timeout_val,
                            scroll_delay_min=scroll_delay_min_val,
                            scroll_delay_max=scroll_delay_max_val,
                            no_new_scroll_limit=no_new_scroll_limit_val,
                            save_batch_size=save_batch_size_val,
                            cooldown_min=cooldown_min_val,
                            cooldown_max=cooldown_max_val,
                            scroll_px=scroll_px_val,
                            initial_load_delay=initial_load_delay_val,
                            pause_event=pause_event,
                            keyword=None,
                            max_collect=max_tweets_per_author,
                            consecutive_date_limit=consecutive_date_limit_val,
                            guarantee_min_scrolls=guarantee_min_scrolls_val,
                            page_already_loaded=True,
                            date_window_size=date_window_size,
                            recovery_config=config,
                            transient_skip_state=transient_skip_state,
                            transient_retry=False,
                        )
                        rows = [row_from_tweet(row_counter.next(), tweet) for tweet in tweets]
                        if rows:
                            safe_writer.writerows(rows)
                        checkpoint.mark_completed(
                            profile_url,
                            {
                                "output_path": output_path,
                                "profile_index": profile_index,
                                "written_count": len(tweets),
                                "worker": worker_index,
                            },
                        )
                        completed_counter.next()
                        processed += 1
                        log_line(log_callback, f"  [W{worker_index}] done @{username or profile_url}: wrote {len(tweets)} tweets.")
                    except XTransientProfileSkipped as exc:
                        checkpoint.release_item(profile_url)
                        log_warn(log_callback, f"  [W{worker_index}] transient X skip: {profile_url}; {exc}")
                    except PlaywrightTimeoutError:
                        checkpoint.release_item(profile_url)
                        log_warn(log_callback, f"  [W{worker_index}] timeout: {profile_url}")
                    except Exception as exc:
                        checkpoint.release_item(profile_url)
                        log_warn(log_callback, f"  [W{worker_index}] failed: {profile_url}; {exc}")
                    finally:
                        try:
                            checkpoint.release_runtime_item(
                                profile_lock_key,
                                namespace="x_profile_page",
                                owner_id=owner_id,
                            )
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

def run_x_profile_tweets_spider(
    profile_urls_text: str,
    keywords_text: str,
    limit_time_str: str,
    start_date: str,
    end_date: str,
    get_comments_str: str,
    max_comments: int,
    cdp_port_or_url: str = DEFAULT_X_CDP_URL,
    max_scrolls: int = DEFAULT_MAX_SCROLLS,
    log_callback=None,
    finish_callback=None,
    stop_event=None,
    config=None,
    pause_event=None,
):
    if config is None:
        config = {}
    page_load_timeout_val = int(config.get("page_load_timeout", PAGE_LOAD_TIMEOUT))
    scroll_delay_min_val, scroll_delay_max_val = normalize_scroll_delay_range(
        config,
        fallback=config.get("scroll_interval", SCROLL_DELAY),
    )
    no_new_scroll_limit_val = int(config.get("no_new_scroll_limit", NO_NEW_SCROLL_LIMIT))
    save_batch_size_val = int(config.get("save_batch_size", SAVE_BATCH_SIZE))
    cooldown_min_val = float(config.get("cooldown_min", COOLDOWN_MIN_SECONDS))
    cooldown_max_val = float(config.get("cooldown_max", COOLDOWN_MAX_SECONDS))
    scroll_px_val = int(config.get("scroll_px", SCROLL_PX))
    initial_load_delay_val = float(config.get("initial_load_delay", INITIAL_LOAD_DELAY))
    max_scrolls = int(config.get("max_scrolls", max_scrolls))
    browser_choice = config.get("browser")
    truncate_threshold = int(config.get("truncate_threshold", 1000))
    max_tweets_per_author = max(1, int(config.get("max_tweets_per_author", DEFAULT_PROFILE_TWEET_LIMIT)))
    consecutive_date_limit_val = int(config.get("consecutive_date_limit", DEFAULT_CONSECUTIVE_DATE_LIMIT))
    guarantee_min_scrolls_val = int(config.get("guarantee_min_scrolls", GUARANTEE_MIN_SCROLLS))
    date_window_size = int(config.get("date_window_size", 20))
    max_parallel_tabs = int(config.get("max_parallel_tabs", 3))
    search_entry_enabled = use_profile_search_entry(config)
    parallel_windows = normalize_parallel_windows(config)

    completed_path = None
    checkpoint = None
    try:
        from src.core import ensure_chrome_for_cdp
        try:
            ensure_chrome_for_cdp(cdp_port_or_url, log_callback=log_callback)
        except Exception:
            pass

        if sync_playwright is None:
            log_error(log_callback, "缺少依赖：playwright。请先安装 requirements.txt 中的依赖。")
            return

        profile_urls = parse_profile_urls(profile_urls_text)
        if not profile_urls:
            log_line(log_callback, "未读取到有效的 X 博主主页链接。")
            return

        limit_time_bool = limit_time_str == "是"
        get_comments_bool = get_comments_str == "是"
        start_dt, end_dt = None, None
        if limit_time_bool:
            start_dt, end_dt = _parse_date_range(start_date, end_date)

        max_comments_val = max(10, int(max_comments))
        checkpoint_scope = {
            "profile_urls": profile_urls,
            "keywords": [keyword.strip() for keyword in keywords_text.splitlines() if keyword.strip()],
            "limit_time": limit_time_bool,
            "start_date": start_date if limit_time_bool else "",
            "end_date": end_date if limit_time_bool else "",
            "get_comments": get_comments_bool,
            "max_comments": max_comments_val if get_comments_bool else 0,
            "max_scrolls": max_scrolls,
            "max_tweets_per_author": max_tweets_per_author,
        }
        checkpoint = open_task_checkpoint(
            "x_profile_tweets",
            checkpoint_scope,
            log_callback=log_callback,
            merge_on_keys=("profile_urls",),
            merge_keep_keys=(
                "keywords",
                "limit_time",
                "start_date",
                "end_date",
                "get_comments",
                "max_comments",
                "max_scrolls",
                "max_tweets_per_author",
            ),
        )
        default_output_path = build_output_path("x", f"x_profile_tweets_{time.strftime('%Y%m%d_%H%M%S')}.xlsx")

        if get_comments_bool:
            comment_fields = ["序号", "推文链接", "评论的点赞量", "评论内容", "评论发布时间"]
            output_path, base_writer = open_checkpointed_multi_sheet_writer(
                checkpoint,
                default_output_path,
                {"推文信息": CSV_FIELDS, "评论信息": comment_fields},
                log_callback=log_callback,
            )
        else:
            output_path, base_writer = open_checkpointed_row_writer(
                checkpoint,
                default_output_path,
                CSV_FIELDS,
                log_callback=log_callback,
            )
        checkpoint.add_output_path(output_path)

        writer_lock = threading.Lock()
        log_lock = threading.Lock()
        
        writer = ThreadSafeWriterWrapper(base_writer, writer_lock)
        if parallel_windows > 1:
            collect_profile_tweets_with_parallel_windows(
                profile_urls,
                checkpoint=checkpoint,
                output_path=output_path,
                writer=base_writer,
                cdp_port_or_url=cdp_port_or_url,
                log_callback=log_callback,
                stop_event=stop_event,
                pause_event=pause_event,
                config=config,
                parallel_windows=parallel_windows,
                browser_choice=browser_choice,
                page_load_timeout_val=page_load_timeout_val,
                scroll_delay_min_val=scroll_delay_min_val,
                scroll_delay_max_val=scroll_delay_max_val,
                no_new_scroll_limit_val=no_new_scroll_limit_val,
                max_scrolls_val=max_scrolls,
                max_tweets_per_author=max_tweets_per_author,
                save_batch_size_val=save_batch_size_val,
                cooldown_min_val=cooldown_min_val,
                cooldown_max_val=cooldown_max_val,
                scroll_px_val=scroll_px_val,
                initial_load_delay_val=initial_load_delay_val,
                consecutive_date_limit_val=consecutive_date_limit_val,
                guarantee_min_scrolls_val=guarantee_min_scrolls_val,
                date_window_size=date_window_size,
                search_entry_enabled=search_entry_enabled,
            )
            completed_path = output_path
            writer.save()
            log_line(log_callback, f"完成，已保存：{output_path}")
            return

        def make_thread_safe_log_callback(original_cb, lock):
            if original_cb is None:
                return None
            def safe_cb(msg: str):
                with lock:
                    original_cb(msg)
            return safe_cb

        safe_log_callback = make_thread_safe_log_callback(log_callback, log_lock)

        total_profiles = len(profile_urls)

        with ThreadPoolExecutor(max_workers=max_parallel_tabs) as executor:
            futures = []
            for profile_index, profile_url in enumerate(profile_urls, 1):
                if should_stop(stop_event):
                    break
                if wait_if_paused(pause_event, stop_event):
                    break
                futures.append(
                    executor.submit(
                        _scrape_single_profile_tweets_task,
                        profile_url=profile_url,
                        keywords_text=keywords_text,
                        limit_time_bool=limit_time_bool,
                        start_dt=start_dt,
                        end_dt=end_dt,
                        get_comments_bool=get_comments_bool,
                        max_comments_val=max_comments_val,
                        cdp_port_or_url=cdp_port_or_url,
                        max_scrolls=max_scrolls,
                        log_callback=safe_log_callback,
                        stop_event=stop_event,
                        writer=writer,
                        writer_lock=writer_lock,
                        page_load_timeout_val=page_load_timeout_val,
                        scroll_delay_min_val=scroll_delay_min_val,
                        scroll_delay_max_val=scroll_delay_max_val,
                        no_new_scroll_limit_val=no_new_scroll_limit_val,
                        save_batch_size_val=save_batch_size_val,
                        cooldown_min_val=cooldown_min_val,
                        cooldown_max_val=cooldown_max_val,
                        scroll_px_val=scroll_px_val,
                        initial_load_delay_val=initial_load_delay_val,
                        pause_event=pause_event,
                        guarantee_min_scrolls_val=guarantee_min_scrolls_val,
                        date_window_size=date_window_size,
                        profile_index=profile_index,
                        total_profiles=total_profiles,
                        truncate_threshold=truncate_threshold,
                        browser_choice=browser_choice,
                        recovery_config=config,
                        use_search_entry=search_entry_enabled,
                        max_collect=max_tweets_per_author,
                        checkpoint=checkpoint,
                        output_path=output_path,
                    )
                )

            for future in as_completed(futures):
                try:
                    future.result()
                except Exception as exc:
                    log_error(safe_log_callback, f"线程执行异常: {exc}")

        completed_path = output_path
        writer.save()
        log_line(safe_log_callback, f"完成，已保存：{output_path}")
    finally:
        if checkpoint is not None:
            checkpoint.close_run()
        if finish_callback:
            finish_callback(completed_path)



# --- Added for compatibility with keyword_author_works and other modules ---
DEFAULT_X_TRANSIENT_SKIP_BEFORE_WAIT = 2

def resolve_x_transient_skip_before_wait(config: dict | None) -> int:
    try:
        return max(1, int((config or {}).get("x_transient_skip_before_wait", DEFAULT_X_TRANSIENT_SKIP_BEFORE_WAIT)))
    except (TypeError, ValueError):
        return DEFAULT_X_TRANSIENT_SKIP_BEFORE_WAIT

def make_x_transient_skip_state(config: dict | None = None) -> dict[str, int]:
    return {
        "skip_count": 0,
        "skip_before_wait": resolve_x_transient_skip_before_wait(config),
    }

def _current_page_is_profile(page, username: str) -> bool:
    try:
        current_username = extract_profile_username(page.url)
    except Exception:
        current_username = ""
    return bool(current_username and current_username.lower() == username.lower().lstrip("@"))

_EXACT_PROFILE_LINK_JS = """({ username, click }) => {
    const target = (username || '').replace(/^@/, '').toLowerCase();
    const links = Array.from(document.querySelectorAll('a[href]'));
    for (const link of links) {
        try {
            const url = new URL(link.getAttribute('href') || '', location.origin);
            const parts = url.pathname.split('/').filter(Boolean);
            if (parts.length !== 1 || parts[0].toLowerCase() !== target) continue;
            if (link.closest('[role="tablist"], nav')) continue;
            link.scrollIntoView({ block: 'center', inline: 'center' });
            if (click) link.click();
            return true;
        } catch (error) {}
    }
    return false;
}"""

def _wait_until_profile_ready(page, username: str, timeout_ms: int, stop_event=None, pause_event=None, log_callback=None, recovery_config=None) -> bool:
    deadline = time.monotonic() + max(1.0, float(timeout_ms or 1000) / 1000.0)
    while time.monotonic() < deadline:
        if should_stop(stop_event):
            return False
        if wait_if_paused(pause_event, stop_event):
            return False
        if _current_page_is_profile(page, username):
            try:
                page.wait_for_selector(
                    'div[data-testid="UserName"], div[data-testid="UserDescription"], article[data-testid="tweet"], article',
                    timeout=3000,
                )
                return True
            except Exception:
                if not wait_for_x_page_recovery(
                    page,
                    log_callback=log_callback,
                    page_timeout=timeout_ms,
                    stop_event=stop_event,
                    pause_event=pause_event,
                    context_label=f"作者主页 @{username}",
                    recovery_config=recovery_config,
                ):
                    return False
                try:
                    page.wait_for_selector(
                        'div[data-testid="UserName"], div[data-testid="UserDescription"], article[data-testid="tweet"], article',
                        timeout=3000,
                    )
                    return True
                except Exception:
                    pass
        if interruptible_sleep(0.5, stop_event):
            return False
    return _current_page_is_profile(page, username)

def _wait_for_exact_profile_result(page, username: str, timeout_ms: int) -> bool:
    timeout_ms = max(1000, int(timeout_ms or 1000))
    try:
        page.wait_for_function(
            _EXACT_PROFILE_LINK_JS,
            {"username": username, "click": False},
            timeout=timeout_ms,
        )
        return True
    except Exception:
        try:
            return _has_exact_profile_result(page, username)
        except Exception:
            return False

def _click_exact_profile_result(page, username: str) -> bool:
    return bool(page.evaluate(_EXACT_PROFILE_LINK_JS, {"username": username, "click": True}))

def _has_exact_profile_result(page, username: str) -> bool:
    return bool(page.evaluate(_EXACT_PROFILE_LINK_JS, {"username": username, "click": False}))

def build_profile_search_url(username: str) -> str:
    import urllib.parse
    query = f"@{username.strip().lstrip('@')}"
    return f"https://x.com/search?q={urllib.parse.quote(query)}&src=typed_query&f=user"

def navigate_to_profile_direct(
    page,
    profile_url: str,
    log_callback,
    page_timeout=None,
    stop_event=None,
    pause_event=None,
    initial_delay=None,
    recovery_config=None,
) -> bool:
    """Open the profile URL directly and wait for the profile shell to stabilize."""
    if page_timeout is None:
        page_timeout = PAGE_LOAD_TIMEOUT
    if initial_delay is None:
        initial_delay = INITIAL_LOAD_DELAY

    normalized_url = clean_profile_url(profile_url)
    username = extract_profile_username(normalized_url)
    if not username:
        log_warn(log_callback, f"  无效的 X 博主主页链接：{profile_url}")
        return False
    if _current_page_is_profile(page, username):
        return True

    log_line(log_callback, f"  直接打开作者主页：@{username}")
    try:
        page.goto(normalized_url, wait_until="domcontentloaded", timeout=page_timeout)
    except Exception as exc:
        log_warn(log_callback, f"  作者主页直连加载异常，继续等待已加载内容：{exc}")
    if interruptible_sleep(initial_delay, stop_event):
        return False
    if wait_if_paused(pause_event, stop_event):
        return False
    return _wait_until_profile_ready(
        page,
        username,
        min(int(page_timeout), 20000),
        stop_event=stop_event,
        pause_event=pause_event,
        log_callback=log_callback,
        recovery_config=recovery_config,
    )

def navigate_to_profile_via_search(
    page,
    profile_url: str,
    log_callback,
    page_timeout=None,
    stop_event=None,
    pause_event=None,
    initial_delay=None,
    recovery_config=None,
) -> bool:
    """Enter a profile by searching the handle and clicking the user result."""
    if page_timeout is None:
        page_timeout = PAGE_LOAD_TIMEOUT
    if initial_delay is None:
        initial_delay = INITIAL_LOAD_DELAY

    normalized_url = clean_profile_url(profile_url)
    username = extract_profile_username(normalized_url)
    if not username:
        log_warn(log_callback, f"  无效的 X 博主主页链接：{profile_url}")
        return False
    if _current_page_is_profile(page, username):
        return True

    search_url = build_profile_search_url(username)
    log_line(log_callback, f"  通过搜索页进入作者主页：@{username}")
    try:
        page.goto(search_url, wait_until="domcontentloaded", timeout=page_timeout)
    except Exception as exc:
        log_warn(log_callback, f"  搜索页加载异常，继续尝试读取已加载结果：{exc}")

    try:
        page.wait_for_selector('main, div[data-testid="primaryColumn"], a[href]', timeout=min(int(page_timeout), 10000))
    except Exception:
        pass
    if interruptible_sleep(initial_delay, stop_event):
        return False
    if wait_if_paused(pause_event, stop_event):
        return False

    search_deadline = time.monotonic() + max(12.0, min(float(page_timeout) / 1000.0, 45.0))
    attempt = 0
    while time.monotonic() < search_deadline:
        attempt += 1
        if should_stop(stop_event):
            return False
        if wait_if_paused(pause_event, stop_event):
            return False
        remaining_ms = max(1000, int((search_deadline - time.monotonic()) * 1000))
        wait_ms = min(6000, remaining_ms)
        if not _wait_for_exact_profile_result(page, username, wait_ms):
            if not wait_for_x_page_recovery(
                page,
                log_callback=log_callback,
                page_timeout=page_timeout,
                stop_event=stop_event,
                pause_event=pause_event,
                context_label="X 搜索页",
                recovery_config=recovery_config,
            ):
                return False
            try:
                page.keyboard.press("End")
            except Exception:
                pass
            interruptible_sleep(1.0, stop_event)
            continue

        try:
            clicked = _click_exact_profile_result(page, username)
        except Exception:
            clicked = False
        if clicked:
            log_line(log_callback, f"  已点击搜索结果，等待作者主页稳定：@{username}")
            if _wait_until_profile_ready(
                page,
                username,
                min(int(page_timeout), 20000),
                stop_event=stop_event,
                pause_event=pause_event,
                log_callback=log_callback,
                recovery_config=recovery_config,
            ):
                return True
            log_warn(log_callback, f"  搜索结果已点击但主页尚未稳定，重试：@{username}")
            try:
                page.goto(search_url, wait_until="domcontentloaded", timeout=page_timeout)
            except Exception:
                pass

        if attempt % 3 == 0:
            try:
                page.reload(wait_until="domcontentloaded", timeout=page_timeout)
                interruptible_sleep(max(1.0, float(initial_delay)), stop_event)
            except Exception:
                pass

    log_warn(log_callback, f"  未能从搜索结果进入作者主页：@{username}")
    return False

def navigate_to_profile(
    page,
    profile_url: str,
    log_callback,
    page_timeout=None,
    stop_event=None,
    pause_event=None,
    initial_delay=None,
    recovery_config=None,
    use_search_entry: bool = False,
) -> bool:
    if use_search_entry:
        return navigate_to_profile_via_search(
            page,
            profile_url,
            log_callback,
            page_timeout=page_timeout,
            stop_event=stop_event,
            pause_event=pause_event,
            initial_delay=initial_delay,
            recovery_config=recovery_config,
        )
    return navigate_to_profile_direct(
        page,
        profile_url,
        log_callback,
        page_timeout=page_timeout,
        stop_event=stop_event,
        pause_event=pause_event,
        initial_delay=initial_delay,
        recovery_config=recovery_config,
    )

def normalize_scroll_delay_range(config: dict | None, fallback: float = SCROLL_DELAY) -> tuple[float, float]:
    config = config or {}
    fallback_value = float(config.get("scroll_interval", fallback) or fallback)
    min_delay = float(config.get("scroll_interval_min", fallback_value) or fallback_value)
    max_delay = float(config.get("scroll_interval_max", fallback_value) or fallback_value)
    min_delay = max(0.1, min_delay)
    max_delay = max(0.1, max_delay)
    if min_delay > max_delay:
        min_delay, max_delay = max_delay, min_delay
    return min_delay, max_delay

def use_profile_search_entry(config: dict | None) -> bool:
    value = (config or {}).get("profile_entry_mode", "直接打开")
    text = str(value or "").strip().lower()
    return text in {"搜索页进入", "搜索进入", "search", "search_page", "search-page", "是", "true", "1", "yes"}
def handle_empty_profile_tweets_recovery(
    page,
    username: str,
    log_callback=None,
    page_timeout=None,
    stop_event=None,
    pause_event=None,
    recovery_config=None,
    transient_skip_state: dict[str, int] | None = None,
    network_checker=None,
    transient_retry: bool = False,
) -> bool:
    matched_phrase = detect_x_transient_error(page)
    if not matched_phrase:
        return True

    resolved_config = resolve_x_page_recovery_config(recovery_config)
    checker = network_checker or check_network_reachable
    if resolved_config.network_check_enabled:
        network_ok, network_detail = checker(resolved_config)
        if not network_ok:
            log_warn(
                log_callback,
                f"  @{username} 遇到 X 载入重试/错误页且网络检查未通过（{network_detail}），优先执行网络重试逻辑...",
            )
            return wait_for_x_page_recovery(
                page,
                log_callback=log_callback,
                page_timeout=page_timeout,
                stop_event=stop_event,
                pause_event=pause_event,
                context_label=f"主页载入重试 @{username}",
                recovery_config=recovery_config,
                network_checker=checker,
            )
    else:
        network_detail = "未启用网络检测"

    if transient_skip_state is None:
        return wait_for_x_page_recovery(
            page,
            log_callback=log_callback,
            page_timeout=page_timeout,
            stop_event=stop_event,
            pause_event=pause_event,
            context_label=f"主页载入重试 @{username}",
            recovery_config=recovery_config,
            network_checker=checker,
        )

    threshold = max(1, int(transient_skip_state.get("skip_before_wait") or DEFAULT_X_TRANSIENT_SKIP_BEFORE_WAIT))
    skip_count = int(transient_skip_state.get("skip_count") or 0) + 1
    transient_skip_state["skip_count"] = skip_count
    log_warn(
        log_callback,
        (
            f"  @{username} 主页未刷出推文且检测到 X 出错（{network_detail}），"
            f"{'将在稍后第二次进入时重试该作者' if transient_retry else '已加入第二次重试队列'}"
            f"，当前跳过计数 {skip_count}/{threshold}。"
        ),
    )

    if skip_count >= threshold:
        transient_skip_state["skip_count"] = 0
        log_warn(log_callback, f"  X 出错/跳过次数已达到阈值 {threshold}，执行一次全局防风控长等待机制。")
        if not wait_for_x_page_recovery(
            page,
            log_callback=log_callback,
            page_timeout=page_timeout,
            stop_event=stop_event,
            pause_event=pause_event,
            context_label=f"主页载入重试 @{username}",
            recovery_config=recovery_config,
            network_checker=checker,
        ):
            raise RuntimeError("X 长等待刷新未恢复，停止执行")

    raise XTransientProfileSkipped(
        f"X 出错/跳过 {username} 详情爬取",
        retry_after_success=not transient_retry,
    )

