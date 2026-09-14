from __future__ import annotations

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
    DEFAULT_X_CDP_URL,
    MultilineText,
    XlsxRowWriter,
    MultiSheetXlsxWriter,
    build_output_path,
    connect_existing_chromium,
    expand_compact_number,
    interruptible_sleep,
    log_error,
    log_line,
    log_warn,
    should_stop,
    wait_if_paused,
)
from src.platforms.x_twitter.comments import extract_comments
from src.platforms.x_twitter.keyword import _x_media_tag, get_media_label
from src.platforms.x_twitter.page_recovery import classify_x_page_failure
from src.platforms.x_twitter.tweet_media import (
    dedupe,
    extract_media_from_syndication,
    extract_quoted_tweet_id,
    fetch_tweet_payload,
    fetch_tweet_media,
    upgrade_twitter_image_url,
)


# 需要落地的媒体列：单元格内按行分隔，Excel 中开启自动换行显示
MEDIA_FIELD_IMAGE = "推文内的图片链接"
MEDIA_FIELD_VIDEO = "推文内的视频链接"
WRAP_FIELDS = (MEDIA_FIELD_IMAGE, MEDIA_FIELD_VIDEO)

CSV_FIELDS = [
    "序号",
    "推文链接",
    "推文的内容",
    "浏览量",
    "评论数",
    "点赞量",
    "转发量",
    "标签",
    MEDIA_FIELD_IMAGE,
    MEDIA_FIELD_VIDEO,
]
PAGE_LOAD_TIMEOUT = 30000
COOLDOWN_EVERY = 3
COOLDOWN_MIN_SECONDS = 3.0
COOLDOWN_MAX_SECONDS = 8.0
STATUS_RE = re.compile(r"/[^/?#]+/status/(\d+)")
NUMBER_RE = re.compile(r"(\d[\d,.]*(?:\.\d+)?\s*(?:[KkMmBb]|千|万|萬|亿|億)?)")
# 页面把长链接拆成「https://」+「域名」两行渲染，这里把被换行/空格打断的链接还原
_BROKEN_URL_RE = re.compile(r"(https?://)\s+(?=[\w-]+\.[a-z]{2,})", re.IGNORECASE)


def clean_tweet_url(url: str) -> str:
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


def parse_tweet_urls(txt_path: str) -> list[str]:
    urls: list[str] = []
    seen = set()
    with open(txt_path, "r", encoding="utf-8-sig") as f:
        for line in f:
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            url = clean_tweet_url(stripped.split()[0])
            if "/status/" in url and url not in seen:
                urls.append(url)
                seen.add(url)
    return urls


def extract_status_id(url: str) -> str:
    match = STATUS_RE.search(clean_tweet_url(url))
    return match.group(1) if match else ""


def normalize_metric_text(text: str, default: str = "") -> str:
    value = re.sub(r"\s+", " ", text or "").strip()
    if not value:
        return default
    match = NUMBER_RE.search(value)
    return expand_compact_number(match.group(1).strip(), default=default) if match else default


def normalize_interaction_metric(text: str) -> str:
    return normalize_metric_text(text, default="0")


def normalize_tweet_text(text: str) -> str:
    """
    还原推文正文里被排版拆断的链接。

    X 会把长链接渲染成「https://」与「hoyo.link/xxx」两行，innerText 出来就是
    ``https:// hoyo.link/xxx``，直接落库会得到不可用的地址。这里把协议头与
    域名之间被换行/空格拆开的部分重新拼回去。
    """
    value = text or ""
    if not value:
        return ""
    previous = None
    while previous != value:
        previous = value
        value = _BROKEN_URL_RE.sub(r"\1", value)
    return value


def article_has_status_id(article, status_id: str) -> bool:
    if not status_id:
        return False
    try:
        return bool(
            article.evaluate(
                """(article, statusId) => {
                    return Array.from(article.querySelectorAll('time')).some(time => {
                        const link = time.closest('a[href*="/status/"]');
                        return Boolean(link && link.href && link.href.includes(`/status/${statusId}`));
                    });
                }""",
                status_id,
            )
        )
    except Exception:
        return False


def find_target_article(page, status_id: str, page_timeout=None):
    if page_timeout is None:
        page_timeout = PAGE_LOAD_TIMEOUT
    try:
        page.wait_for_selector('article[data-testid="tweet"], article', timeout=page_timeout)
    except Exception:
        return None

    try:
        articles = page.locator('article[data-testid="tweet"], article').all()
    except Exception:
        return None

    for article in articles:
        if article_has_status_id(article, status_id):
            return article
    return None


def wait_for_article_media_ready(article, timeout: float = 6.0, poll: float = 0.5) -> dict[str, int]:
    """
    等待推文媒体渲染稳定后再提取。

    X 先插入媒体占位节点、稍后才写入 ``img.src``。若在 2.5s 的固定等待后就读，
    可能出现「标签判定为有图片，但图片地址为空」的矛盾结果（实测偶发）。
    这里轮询媒体节点数量，连续两次不变才认为渲染完成。
    """
    script = """(article) => {
        const isMediaSrc = src => Boolean(src) && (src.includes('pbs.twimg.com/media') || src.includes('amplify_video_thumb'));
        const images = Array.from(article.querySelectorAll('img')).filter(img => isMediaSrc(img.getAttribute('src') || ''));
        const videos = Array.from(article.querySelectorAll('video'))
            .filter(v => Boolean(v.getAttribute('poster') || v.getAttribute('src')));
        return {
            images: images.length,
            videos: videos.length,
            shells: article.querySelectorAll('[data-testid="tweetPhoto"], [data-testid="videoComponent"]').length,
        };
    }"""
    deadline = time.time() + max(0.0, timeout)
    last: dict[str, int] = {}
    stable = 0
    while True:
        try:
            signature = article.evaluate(script)
        except Exception:
            return last
        if signature == last:
            stable += 1
            if stable >= 2:
                return signature
        else:
            stable = 0
        last = signature
        if time.time() >= deadline:
            return last
        time.sleep(poll)


def extract_article_payload(article) -> dict[str, str]:
    return article.evaluate(
        """async (article) => {
            const firstText = selector => {
                const node = article.querySelector(selector);
                return node ? (node.innerText || node.textContent || '').trim() : '';
            };
            const firstMetric = selectors => {
                for (const selector of selectors) {
                    for (const node of article.querySelectorAll(selector)) {
                        const rawText = (node.innerText || node.textContent || '').trim();
                        const aria = (node.getAttribute('aria-label') || '').trim();
                        if (/\\d/.test(rawText)) return rawText;
                        if (/\\d/.test(aria)) return aria;
                    }
                }
                return '';
            };
            const nonTextContent = () => {
                const types = [];
                if (article.querySelector('[data-testid="tweetPhoto"], img[src*="/media/"]')) types.push('图片');
                if (article.querySelector('video')) types.push('视频');
                if ((article.innerText || '').split('\\n').some(line => line.trim().toLowerCase() === 'gif')) types.push('GIF');
                if (article.querySelector('[data-testid="card.wrapper"], [data-testid="card.layoutLarge.media"], [data-testid="card.layoutSmall.media"]')) types.push('卡片');
                return types.length ? `[${types.join('+')}]` : '[非文本]';
            };

            const tweetTextEl = article.querySelector('[data-testid="tweetText"]');
            if (tweetTextEl) {
                // Step 1: Revert auto-translation
                const revertTexts = ['view original', '查看原文', '原文を表示', 'show original', '原文を見る'];
                const allNodes = article.querySelectorAll('*');
                for (const node of allNodes) {
                    const nodeText = (node.textContent || '').trim().toLowerCase();
                    if (!nodeText || node.children.length > 0) continue;
                    if (revertTexts.includes(nodeText)) {
                        try { node.click(); } catch (_) {}
                        break;
                    }
                }
                // Step 2: Remove CSS truncation
                tweetTextEl.style.setProperty('max-height', 'none', 'important');
                tweetTextEl.style.setProperty('overflow', 'visible', 'important');
                tweetTextEl.style.setProperty('-webkit-line-clamp', 'unset', 'important');
                tweetTextEl.style.setProperty('display', 'block', 'important');
                tweetTextEl.style.setProperty('white-space', 'normal', 'important');
                // Step 3: Click "Show more" if present
                const expandTexts = ['show more', 'show more...', 'もっと見る', '더 보기'];
                for (const node of allNodes) {
                    const nodeText = (node.textContent || '').trim().toLowerCase();
                    if (!nodeText || node.children.length > 0) continue;
                    if (!expandTexts.includes(nodeText)) continue;
                    try { node.click(); } catch (_) {}
                    break;
                }
                // Wait for React to re-render with original text
                await new Promise(r => setTimeout(r, 400));

                // Step 4: 还原被排版拆断的链接
                // X 用块级 span 渲染长链接（"https://" 单独占一行），导致 innerText
                // 出现 "https://\\nhoyo.link/xxx"。把链接内部还原成纯文本即可拼回完整地址。
                for (const anchor of tweetTextEl.querySelectorAll('a')) {
                    const raw = (anchor.textContent || '').trim();
                    if (!raw) continue;
                    const looksLikeUrl = /^(https?:\\/\\/|www\\.)/i.test(raw)
                        || /^[\\w-]+(\\.[\\w-]+){1,}(\\/|$)/i.test(raw);
                    if (looksLikeUrl) {
                        anchor.textContent = raw.replace(/\\s+/g, '');
                    }
                }
            }
            const content = firstText('[data-testid="tweetText"]') || nonTextContent();

            // 只取外层推文自身的媒体，排除引用推文里的媒体
            const embedded = article.querySelector('[data-testid="quoteTweet"]')
                || article.querySelector('article[data-testid="tweet"]');
            const isEmbedded = el => Boolean(embedded && embedded.contains(el));
            const images = Array.from(article.querySelectorAll(
                '[data-testid="tweetPhoto"] img, img[src*="pbs.twimg.com/media"], img[src*="/media/"]'
            ))
                .filter(img => !isEmbedded(img))
                .map(img => img.getAttribute('src') || '')
                .filter(Boolean);
            const videoElements = Array.from(article.querySelectorAll('video')).filter(video => !isEmbedded(video));
            const videos = videoElements
                .map(video => video.getAttribute('src') || video.currentSrc || '')
                .filter(src => src && !src.startsWith('blob:'));
            return {
                content,
                images,
                videos,
                videoNodes: videoElements.length,
                views: firstMetric([
                    'a[href*="/analytics"]',
                    'div[data-testid="postViewCount"]',
                    '[aria-label*="Views"]',
                    '[aria-label*="views"]',
                    '[aria-label*="浏览"]',
                ]),
                replies: firstMetric(['[data-testid="reply"]']),
                likes: firstMetric(['[data-testid="like"]', '[data-testid="unlike"]']),
                reposts: firstMetric(['[data-testid="retweet"]', '[data-testid="unretweet"]']),
            };
        }"""
    )


def collect_tweet_metrics(page, tweet_url: str, page_timeout=None, page_ready_wait=2.5, stop_event=None, log_callback=None) -> dict[str, str]:
    if page_timeout is None:
        page_timeout = PAGE_LOAD_TIMEOUT
    normalized_url = clean_tweet_url(tweet_url)
    status_id = extract_status_id(normalized_url)
    if not status_id:
        raise ValueError("无法解析推文 ID")

    page.goto(normalized_url, wait_until="load", timeout=page_timeout)
    interruptible_sleep(page_ready_wait, stop_event)

    current_url = page.url
    if "login" in current_url.lower() or "account" in current_url.lower():
        log_warn(log_callback, f"  警告：当前页面疑似登录页：{current_url}")
        raise RuntimeError("页面跳转到登录页，请确认 Chrome 已登录 X/Twitter")

    article = find_target_article(page, status_id, page_timeout=page_timeout)
    if article is None:
        reason = classify_x_page_failure(page, stop_event=stop_event)
        log_warn(log_callback, f"  当前页面 URL：{current_url}，失败原因：{reason}")
        raise RuntimeError(f"未找到目标推文 DOM（{reason}）")

    payload = extract_article_payload(article)
    media_label = get_media_label(article)
    media_state = wait_for_article_media_ready(article)

    # 图片/视频地址：优先用 syndication 接口（原图 + 可下载 mp4），DOM 结果兜底
    dom_images = dedupe(upgrade_twitter_image_url(url) for url in payload.get("images") or [])
    dom_videos = dedupe(payload.get("videos") or [])
    api_payload = fetch_tweet_payload(status_id)
    api_images, api_videos = extract_media_from_syndication(api_payload)
    images = api_images or dom_images
    videos = api_videos or dom_videos

    # 引用推文：媒体挂被引用推文上，接口不会把它算在本条上。
    # 页面里确实出现了视频节点却没有拿到地址时，回退到被引用推文重新解析。
    if not videos and int(payload.get("videoNodes") or 0) > 0:
        quoted_id = extract_quoted_tweet_id(api_payload)
        if quoted_id:
            quoted_images, quoted_videos = fetch_tweet_media(quoted_id)
            if quoted_videos:
                videos = quoted_videos
                images = images or quoted_images
                log_line(log_callback, f"  视频来自被引用推文 {quoted_id}。")

    if not images and not videos and media_state.get("images"):
        # 接口与 DOM 都失手时再补一次 DOM 读取，避免「有媒体却无链接」
        media_state = wait_for_article_media_ready(article, timeout=3.0)
        dom_images = dedupe(upgrade_twitter_image_url(url) for url in extract_article_payload(article).get("images") or [])
        images = images or dom_images
    log_line(
        log_callback,
        f"  媒体解析：图片 {len(images)} 张，视频 {len(videos)} 个"
        f"（DOM 媒体节点 {media_state.get('images', 0)} 图 / {media_state.get('videos', 0)} 视频）。",
    )

    return {
        "推文链接": normalized_url,
        "推文的内容": normalize_tweet_text(payload.get("content", "")),
        "浏览量": normalize_metric_text(payload.get("views", "")),
        "评论数": normalize_interaction_metric(payload.get("replies", "")),
        "点赞量": normalize_interaction_metric(payload.get("likes", "")),
        "转发量": normalize_interaction_metric(payload.get("reposts", "")),
        "标签": _x_media_tag(media_label),
        MEDIA_FIELD_IMAGE: MultilineText("\n".join(images)),
        MEDIA_FIELD_VIDEO: MultilineText("\n".join(videos)),
    }


def _scrape_single_metric_task(
    index: int,
    tweet_url: str,
    cdp_port_or_url: str,
    get_comments_bool: bool,
    scan_limit: int,
    tweet_comment_top_limit: int,
    page_load_timeout_val: int,
    page_ready_wait_val: float,
    comment_options: dict,
    log_callback,
    stop_event,
    pause_event,
    writer,
    writer_lock,
    total_urls: int,
):
    from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError
    from src.platforms.x_twitter.tweet_metrics import clean_tweet_url, collect_tweet_metrics

    if should_stop(stop_event):
        return
    if wait_if_paused(pause_event, stop_event):
        return

    normalized_url = clean_tweet_url(tweet_url)
    row = {
        "序号": str(index),
        "推文链接": normalized_url,
        "推文的内容": "",
        "浏览量": "",
        "评论数": "",
        "点赞量": "",
        "转发量": "",
        "标签": "",
        MEDIA_FIELD_IMAGE: "",
        MEDIA_FIELD_VIDEO: "",
    }
    log_line(log_callback, f"[{index}/{total_urls}] 读取推文：{normalized_url}")

    playwright = None
    browser = None
    page = None
    try:
        playwright = sync_playwright().start()
        try:
            browser, context = connect_existing_chromium(playwright, cdp_port_or_url)
        except Exception as e:
            log_error(log_callback, f"[{index}/{total_urls}] 连接失败：{e}")
            return

        page = context.new_page()
        metrics_ok = False
        max_attempts = 2
        for attempt in range(1, max_attempts + 1):
            try:
                row.update(
                    collect_tweet_metrics(
                        page,
                        normalized_url,
                        page_timeout=page_load_timeout_val,
                        page_ready_wait=page_ready_wait_val,
                        stop_event=stop_event,
                        log_callback=log_callback,
                    )
                )
                metrics_ok = True
                break
            except Exception as exc:
                if attempt >= max_attempts:
                    row["标签"] = classify_x_page_failure(page, stop_event=stop_event)
                    log_error(log_callback, f"[{index}/{total_urls}] 处理失败，写入空指标行（{row['标签']}）：{exc}")
                    break
                # 标签页可能已被外部关闭或页面卡死，重建标签页后再试一次
                log_warn(log_callback, f"[{index}/{total_urls}] 第 {attempt} 次读取失败，2 秒后重试：{exc}")
                interruptible_sleep(2.0, stop_event)
                try:
                    if page is not None and not page.is_closed():
                        page.close()
                except Exception:
                    pass
                try:
                    page = context.new_page()
                except Exception as recreate_exc:
                    log_error(log_callback, f"[{index}/{total_urls}] 重建标签页失败：{recreate_exc}")
                    break

        if metrics_ok and get_comments_bool:
            try:
                comments = extract_comments(
                    page,
                    normalized_url,
                    scan_limit,
                    log_callback,
                    stop_event,
                    scroll_pause=comment_options.get("scroll_pause"),
                    scroll_pause_max=comment_options.get("scroll_pause_max"),
                    no_new_scroll_limit=comment_options.get("no_new_scroll_limit"),
                    render_wait=comment_options.get("render_wait"),
                    boundary_patience=comment_options.get("boundary_patience"),
                    pause_event=pause_event,
                )
                comments.sort(key=lambda item: int(item.get("likes", "0") or 0), reverse=True)
                comment_rows = []
                for comment in comments[:tweet_comment_top_limit]:
                    comment_row = {
                        "序号": row["序号"],
                        "推文链接": normalized_url,
                        "评论的点赞量": comment.get("likes", ""),
                        "评论内容": comment.get("content", ""),
                        "评论发布时间": comment.get("time", "")
                    }
                    comment_rows.append(comment_row)
                if comment_rows:
                    with writer_lock:
                        writer.writerows("评论信息", comment_rows)
            except Exception as exc:
                log_line(log_callback, f"[{index}/{total_urls}] 提取评论失败：{exc}")

        with writer_lock:
            if get_comments_bool:
                writer.writerow("推文信息", row)
            else:
                writer.writerow(row)
        log_line(log_callback, f"[{index}/{total_urls}] 完成：已写入缓冲区。")

    except Exception as exc:
        log_error(log_callback, f"[{index}/{total_urls}] 异常: {exc}")
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


def run_x_tweet_metrics_spider(
    txt_path: str,
    get_comments_str: str,
    max_comments: int,
    cdp_port_or_url: str = DEFAULT_X_CDP_URL,
    log_callback=None,
    finish_callback=None,
    stop_event=None,
    config=None,
    pause_event=None,
):
    if config is None:
        config = {}
    page_load_timeout_val = int(config.get("page_load_timeout", PAGE_LOAD_TIMEOUT))
    page_ready_wait_val = float(config.get("page_ready_wait", 2.5))
    tweet_comment_top_limit = int(config.get("comment_top_limit", 100))
    max_parallel_tabs = int(config.get("max_parallel_tabs", 3))

    # 评论抓取节奏：X 的回复是渐进渲染的，节奏太快会扫不到内容
    comment_scroll_min = float(config.get("comment_scroll_min", 2.0))
    comment_scroll_max = float(config.get("comment_scroll_max", 4.5))
    if comment_scroll_max < comment_scroll_min:
        comment_scroll_max = comment_scroll_min
    comment_options = {
        "scroll_pause": comment_scroll_min,
        "scroll_pause_max": comment_scroll_max,
        "no_new_scroll_limit": int(config.get("comment_no_new_scroll_limit", 6)),
        "render_wait": float(config.get("comment_render_wait", 8.0)),
        "boundary_patience": int(config.get("comment_boundary_patience", 3)),
    }

    completed_path = None
    try:
        from src.core import ensure_chrome_for_cdp
        try:
            ensure_chrome_for_cdp(cdp_port_or_url, log_callback=log_callback)
        except Exception:
            pass

        if sync_playwright is None:
            log_error(log_callback, "缺少依赖：playwright。请先安装 requirements.txt 中的依赖。")
            return

        tweet_urls = parse_tweet_urls(txt_path)
        if not tweet_urls:
            log_line(log_callback, "TXT 中没有有效的推文链接。")
            return

        get_comments_bool = get_comments_str == "是"
        scan_limit = max(int(max_comments), tweet_comment_top_limit)

        output_path = build_output_path("x", f"x_tweet_metrics_{time.strftime('%Y%m%d_%H%M%S')}.xlsx")
        if get_comments_bool:
            comment_fields = ["序号", "推文链接", "评论的点赞量", "评论内容", "评论发布时间"]
            writer = MultiSheetXlsxWriter(
                output_path,
                {"推文信息": CSV_FIELDS, "评论信息": comment_fields},
                autosave_every=5000,
                sheets_wrap_fields={"推文信息": WRAP_FIELDS},
            )
        else:
            writer = XlsxRowWriter(output_path, CSV_FIELDS, autosave_every=5000, wrap_fields=WRAP_FIELDS)

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
                        _scrape_single_metric_task,
                        index=index,
                        tweet_url=tweet_url,
                        cdp_port_or_url=cdp_port_or_url,
                        get_comments_bool=get_comments_bool,
                        scan_limit=scan_limit,
                        tweet_comment_top_limit=tweet_comment_top_limit,
                        page_load_timeout_val=page_load_timeout_val,
                        page_ready_wait_val=page_ready_wait_val,
                        comment_options=comment_options,
                        log_callback=safe_log_callback,
                        stop_event=stop_event,
                        pause_event=pause_event,
                        writer=writer,
                        writer_lock=writer_lock,
                        total_urls=len(tweet_urls),
                    )
                )

            for future in as_completed(futures):
                try:
                    future.result()
                except Exception as exc:
                    log_error(safe_log_callback, f"线程执行异常: {exc}")

        completed_path = output_path
        writer.save()
        log_line(log_callback, f"完成，已保存：{output_path}")
    finally:
        if finish_callback:
            finish_callback(completed_path)
