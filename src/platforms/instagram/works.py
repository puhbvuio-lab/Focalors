from __future__ import annotations

from datetime import datetime
import random
import re
import time
from urllib.parse import urlparse

try:
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
    from playwright.sync_api import sync_playwright
except ModuleNotFoundError:
    PlaywrightTimeoutError = TimeoutError
    sync_playwright = None

from src.core import (
    DEFAULT_X_CDP_URL,
    XlsxRowWriter,
    build_output_path,
    connect_existing_chromium,
    expand_compact_number,
    interruptible_random_sleep,
    interruptible_sleep,
    log_error,
    log_line,
    log_warn,
    sanitize_csv_cell,
    should_stop,
    wait_if_paused,
)


CSV_FIELDS = ["序号", "作品ID", "作品链接", "发布时间", "作品内容", "评论数", "点赞数"]
PAGE_LOAD_TIMEOUT = 45000
INITIAL_LOAD_DELAY = 3.5
SCROLL_DELAY = 3.0
SCROLL_PX = 2600
NO_NEW_SCROLL_LIMIT = 8
DEFAULT_MAX_WORKS = 10000
DEFAULT_MAX_SCROLLS = 160
SAVE_BATCH_SIZE = 10
COOLDOWN_MIN_SECONDS = 10.0
COOLDOWN_MAX_SECONDS = 25.0
DETAIL_DELAY_MIN_SECONDS = 3.0
DETAIL_DELAY_MAX_SECONDS = 7.0
BEFORE_DETAIL_DELAY_MIN_SECONDS = 6.0
BEFORE_DETAIL_DELAY_MAX_SECONDS = 12.0
RATE_LIMIT_RETRY_DELAY_MIN_SECONDS = 180.0
RATE_LIMIT_RETRY_DELAY_MAX_SECONDS = 300.0
RATE_LIMIT_MAX_RETRIES = 1

BLOCKED_USERNAMES = {
    "accounts",
    "explore",
    "p",
    "reel",
    "reels",
    "stories",
    "tv",
}


class InstagramRateLimitError(RuntimeError):
    pass


class InstagramUnavailableWorkError(RuntimeError):
    pass


class InstagramStoppedError(RuntimeError):
    pass


def clean_profile_url(url: str) -> str:
    value = (url or "").strip()
    if not value:
        return ""
    if value.startswith("//"):
        value = "https:" + value
    if value.startswith("/"):
        value = "https://www.instagram.com" + value
    if not value.startswith("http"):
        value = "https://" + value

    parsed = urlparse(value)
    host = (parsed.netloc or "").lower()
    if "instagram.com" not in host:
        return ""

    parts = [part for part in parsed.path.split("/") if part]
    if not parts:
        return ""
    username = parts[0].strip().strip("@")
    if not username or username.lower() in BLOCKED_USERNAMES:
        return ""
    return f"https://www.instagram.com/{username}/"


def extract_username(profile_url: str) -> str:
    match = re.match(r"https?://(?:www\.)?instagram\.com/([^/?#]+)/?", clean_profile_url(profile_url), re.I)
    if not match:
        return ""
    username = match.group(1).strip().strip("@")
    return "" if username.lower() in BLOCKED_USERNAMES else username


def parse_profile_urls(text: str) -> list[str]:
    urls: list[str] = []
    seen = set()
    for line in (text or "").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        url = clean_profile_url(stripped.split()[0])
        if url and url not in seen:
            urls.append(url)
            seen.add(url)
    return urls


def normalize_work_url(href: str) -> str:
    value = (href or "").strip().replace("\\/", "/").replace("\\u002F", "/")
    if not value:
        return ""
    if value.startswith("//"):
        value = "https:" + value
    if value.startswith("/"):
        value = "https://www.instagram.com" + value
    match = re.search(r"(https://www\.instagram\.com/(?:[A-Za-z0-9_.]+/)?(?:p|reel|tv)/[^/?#]+)", value, re.I)
    return match.group(1).rstrip("/") + "/" if match else value.split("?")[0].split("#")[0].rstrip("/") + "/"


def extract_work_id(work_url: str) -> str:
    match = re.search(r"instagram\.com/(?:[A-Za-z0-9_.]+/)?(?:p|reel|tv)/([^/?#]+)/?", normalize_work_url(work_url), re.I)
    return match.group(1) if match else ""


def normalize_metric_text(text: str, default: str = "") -> str:
    value = re.sub(r"\s+", " ", text or "").strip()
    if not value:
        return default
    match = re.search(r"(\d[\d,.]*(?:\.\d+)?\s*(?:K|M|B|万|萬|亿|億)?)", value, flags=re.I)
    return expand_compact_number(match.group(1).strip(), default=default) if match else default


def format_instagram_time(raw_time: str) -> str:
    value = (raw_time or "").strip()
    if not value:
        return ""
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return value


def looks_like_instagram_auto_alt(text: str) -> bool:
    value = re.sub(r"\s+", " ", text or "").strip()
    if not value:
        return False
    return bool(
        re.match(r"^Photo by .+ on [A-Z][a-z]+ \d{1,2}, \d{4}\.?(?: May be .*)?$", value, re.I)
        or re.match(r"^Photo shared by .+ on [A-Z][a-z]+ \d{1,2}, \d{4}\.?(?: May be .*)?$", value, re.I)
    )


def build_content(caption: str, media_type: str) -> str:
    text = (caption or "").strip()
    if looks_like_instagram_auto_alt(text):
        text = ""
    title = text.splitlines()[0].strip() if text else ""
    mt = (media_type or "").lower()
    if mt in ("reel", "video"):
        label = "[视频]"
    elif mt == "carousel":
        label = "[轮播]"
    else:
        label = "[图片]"
    return f"{title}{label}" if title else label


def extract_visible_work_links(page) -> list[dict[str, str]]:
    return page.evaluate(
        """() => {
            const results = [];
            const seen = new Set();
            const normalize = href => {
                if (!href) return '';
                href = String(href).replaceAll('\\\\/', '/').replaceAll('\\u002F', '/');
                try {
                    const url = new URL(href, location.origin);
                    const match = url.pathname.match(/^\\/(?:([A-Za-z0-9_.]+)\\/)?(p|reel|tv)\\/([A-Za-z0-9_-]+)\\/?$/i);
                    if (!match) return '';
                    const prefix = match[1] ? `${match[1]}/` : '';
                    return `https://www.instagram.com/${prefix}${match[2]}/${match[3]}/`;
                } catch (error) {
                    const match = href.match(/^\\/(?:([A-Za-z0-9_.]+)\\/)?(p|reel|tv)\\/([A-Za-z0-9_-]+)\\/?$/i);
                    if (!match) return '';
                    const prefix = match[1] ? `${match[1]}/` : '';
                    return `https://www.instagram.com/${prefix}${match[2]}/${match[3]}/`;
                }
            };
            const mediaTypeFromUrl = href => {
                if (/\\/reel\\//i.test(href)) return 'reel';
                if (/\\/tv\\//i.test(href)) return 'video';
                return '';
            };
            const detectType = (href, link) => {
                const fromUrl = mediaTypeFromUrl(href);
                if (fromUrl) return fromUrl;
                if (!link) return 'image';
                const root = link.closest('article, main, div') || link;
                const text = (root.innerText || '').toLowerCase();
                if (root.querySelector('svg[aria-label*="Clip"], svg[aria-label*="Reel"], video') || text.includes('reel')) return 'video';
                if (root.querySelector('svg[aria-label*="Carousel"], svg[aria-label*="轮播"], svg[aria-label*="帖子"]')) return 'carousel';
                return 'image';
            };
            const add = (href, mediaType, link = null) => {
                href = normalize(href);
                if (!href || seen.has(href)) return;
                seen.add(href);
                results.push({ link: href, mediaType: mediaType || detectType(href, link) });
            };
            const isRenderedLink = link => {
                const rect = link.getBoundingClientRect();
                if (!rect || (rect.width <= 0 && rect.height <= 0)) return false;
                const style = window.getComputedStyle(link);
                return style.display !== 'none' && style.visibility !== 'hidden' && Number(style.opacity || 1) !== 0;
            };
            const root = document.querySelector('main') || document;
            const links = Array.from(root.querySelectorAll('a[href*="/p/"], a[href*="/reel/"], a[href*="/tv/"]'));
            for (const link of links) {
                if (isRenderedLink(link)) add(link.getAttribute('href') || link.href || '', '', link);
            }
            if (!results.length) {
                for (const link of links) add(link.getAttribute('href') || link.href || '', '', link);
            }
            return results;
        }"""
    )


def instagram_page_debug_info(page) -> dict[str, str | int]:
    return page.evaluate(
        """() => ({
            url: location.href,
            title: document.title || '',
            bodyText: (document.body ? document.body.innerText : '').slice(0, 240),
            linkCount: document.querySelectorAll('a').length,
            postLinkCount: document.querySelectorAll('a[href*="/p/"], a[href*="/reel/"], a[href*="/tv/"]').length,
        })"""
    )


def is_instagram_rate_limited_page(page, response=None) -> bool:
    try:
        if response is not None and getattr(response, "status", None) == 429:
            return True
    except Exception:
        pass
    try:
        info = instagram_page_debug_info(page)
    except Exception:
        return False
    text = " ".join(
        str(info.get(key, "") or "").lower()
        for key in ("title", "bodyText")
    )
    return (
        "http error 429" in text
        or "too many requests" in text
        or "this page isn't working" in text
        or "this page isn’t working" in text
    )


def is_instagram_unavailable_page(page) -> bool:
    try:
        info = instagram_page_debug_info(page)
    except Exception:
        return False
    text = " ".join(
        str(info.get(key, "") or "").lower()
        for key in ("title", "bodyText")
    )
    return (
        "sorry, this page isn't available" in text
        or "sorry, this page isn’t available" in text
        or "the link you followed may be broken" in text
        or "page may have been removed" in text
        or "此页面无法显示" in text
        or "链接可能已损坏" in text
        or "页面已被移除" in text
    )


def collect_profile_work_links(page, profile_url: str, max_works: int, max_scrolls: int, log_callback, stop_event=None, pause_event=None, page_timeout=None, scroll_delay=None, scroll_px=None, no_new_limit=None, initial_load_delay=None) -> list[dict[str, str]]:
    if page_timeout is None:
        page_timeout = PAGE_LOAD_TIMEOUT
    if scroll_delay is None:
        scroll_delay = SCROLL_DELAY
    if scroll_px is None:
        scroll_px = SCROLL_PX
    if no_new_limit is None:
        no_new_limit = NO_NEW_SCROLL_LIMIT
    if initial_load_delay is None:
        initial_load_delay = INITIAL_LOAD_DELAY

    username = extract_username(profile_url)
    if not username:
        raise ValueError(f"无效的 Instagram 作者主页链接：{profile_url}")

    page.goto(clean_profile_url(profile_url), wait_until="domcontentloaded", timeout=page_timeout)
    interruptible_sleep(initial_load_delay, stop_event)
    try:
        page.wait_for_selector('main, article, a[href*="/p/"], a[href*="/reel/"], a[href*="/tv/"]', timeout=12000)
    except PlaywrightTimeoutError:
        log_warn(log_callback, "  未等到主页内容区域，继续滚动尝试。")

    works: list[dict[str, str]] = []
    seen = set()
    no_new_count = 0
    max_works = max(1, int(max_works or DEFAULT_MAX_WORKS))
    max_scrolls = max(1, int(max_scrolls or DEFAULT_MAX_SCROLLS))
    log_line(log_callback, f"  开始收集 @{username} 主页作品链接，最多 {max_works} 条。")

    for scroll_index in range(max_scrolls):
        if should_stop(stop_event) or len(works) >= max_works:
            break
        if wait_if_paused(pause_event, stop_event):
            break
        added = 0
        for item in extract_visible_work_links(page):
            link = normalize_work_url(item.get("link", ""))
            work_id = extract_work_id(link)
            if not link or not work_id or link in seen:
                continue
            seen.add(link)
            works.append({"id": work_id, "link": link, "media_type": item.get("mediaType", "")})
            added += 1
            if len(works) >= max_works:
                break

        if added:
            log_line(log_callback, f"  滚动 {scroll_index + 1}/{max_scrolls}：新增 {added} 条，累计 {len(works)} 条。")
            no_new_count = 0
        else:
            no_new_count += 1
            if scroll_index == 0:
                info = instagram_page_debug_info(page)
                log_line(
                    log_callback,
                    f"  第一次扫描未发现作品链接：url={info.get('url')} title={info.get('title')} "
                    f"links={info.get('linkCount')} postLinks={info.get('postLinkCount')}",
                )
            if no_new_count >= no_new_limit:
                log_line(log_callback, f"  连续 {no_new_limit} 次没有新增作品，停止滚动。")
                break

        page.evaluate(f"window.scrollBy(0, {scroll_px})")
        interruptible_sleep(scroll_delay, stop_event)

    return works


def extract_detail_from_page(page, fallback_media_type: str) -> dict[str, str]:
    return page.evaluate(
        """({ fallbackMediaType }) => {
            const text = node => node ? (node.innerText || node.textContent || '').trim() : '';
            const meta = name => {
                const node = document.querySelector(`meta[property="${name}"], meta[name="${name}"]`);
                return node ? (node.getAttribute('content') || '').trim() : '';
            };
            const decodeJsonText = value => {
                if (!value) return '';
                try { return JSON.parse(`"${value}"`); }
                catch (error) {
                    return String(value)
                        .replace(/\\\\n/g, '\\n')
                        .replace(/\\\\t/g, ' ')
                        .replace(/\\\\"/g, '"')
                        .replace(/\\\\u0026/g, '&')
                        .replace(/\\\\\\//g, '/');
                }
            };
            const shortcode = (() => {
                const match = location.pathname.match(/\\/(p|reel|tv)\\/([^/?#]+)/i);
                return match ? match[2] : '';
            })();
            const pageText = () => {
                const html = document.documentElement ? document.documentElement.innerHTML : '';
                return html.replaceAll('\\\\/', '/').replaceAll('\\u002F', '/');
            };
            const jsonWindow = () => {
                const full = pageText();
                if (!shortcode) return full;
                const index = full.indexOf(shortcode);
                if (index < 0) return full;
                return full.slice(Math.max(0, index - 50000), Math.min(full.length, index + 120000));
            };
            const jsonText = jsonWindow();
            const firstMatch = patterns => {
                for (const pattern of patterns) {
                    const match = jsonText.match(pattern);
                    if (match && match[1] !== undefined && match[1] !== null) return match[1];
                }
                return '';
            };
            const parseEpoch = value => {
                if (!value) return '';
                const number = Number(value);
                if (!Number.isFinite(number) || number <= 0) return '';
                const millis = number > 100000000000 ? number : number * 1000;
                try { return new Date(millis).toISOString(); }
                catch (error) { return ''; }
            };
            const parseJsonScripts = () => {
                const roots = [];
                // Search ALL script tags for data containing the shortcode
                for (const script of document.querySelectorAll('script')) {
                    const raw = script.textContent || '';
                    if (!raw || raw.length > 800000) continue;
                    if (shortcode && !raw.includes(shortcode)) continue;
                    try { roots.push(JSON.parse(raw)); } catch (error) {}
                }
                // Fallback: parse application/json and ld+json scripts without shortcode filter
                if (!roots.length) {
                    for (const script of document.querySelectorAll('script[type="application/json"], script[type="application/ld+json"]')) {
                        const raw = script.textContent || '';
                        if (!raw) continue;
                        try { roots.push(JSON.parse(raw)); } catch (error) {}
                    }
                }
                return roots;
            };
            const walkFind = (root, predicate, maxNodes = 12000) => {
                const stack = [root];
                let scanned = 0;
                while (stack.length && scanned < maxNodes) {
                    const node = stack.pop();
                    scanned += 1;
                    if (!node || typeof node !== 'object') continue;
                    if (predicate(node)) return node;
                    if (Array.isArray(node)) {
                        for (const child of node) stack.push(child);
                    } else {
                        for (const value of Object.values(node)) {
                            if (value && typeof value === 'object') stack.push(value);
                        }
                    }
                }
                return null;
            };
            const walkValue = (root, names, maxNodes = 12000) => {
                const wanted = new Set(names);
                const stack = [root];
                let scanned = 0;
                while (stack.length && scanned < maxNodes) {
                    const node = stack.pop();
                    scanned += 1;
                    if (!node || typeof node !== 'object') continue;
                    if (Array.isArray(node)) {
                        for (const child of node) stack.push(child);
                        continue;
                    }
                    for (const [key, value] of Object.entries(node)) {
                        if (wanted.has(key) && value !== undefined && value !== null && value !== '') {
                            if (typeof value === 'object' && value.count !== undefined) return String(value.count);
                            if (typeof value === 'object' && value.text !== undefined) return String(value.text);
                            if (typeof value === 'object' && value.created_at !== undefined) return String(value.created_at);
                            return String(value);
                        }
                        if (value && typeof value === 'object') stack.push(value);
                    }
                }
                return '';
            };
            const structuredRoots = parseJsonScripts();
            const structuredMedia = (() => {
                if (!shortcode) return null;
                for (const root of structuredRoots) {
                    const found = walkFind(root, node => (
                        String(node.shortcode || '') === shortcode ||
                        String(node.code || '') === shortcode
                    ));
                    if (found) return found;
                }
                return null;
            })();
            const valueFromStructured = names => {
                if (structuredMedia) {
                    const value = walkValue(structuredMedia, names);
                    if (value) return value;
                }
                for (const root of structuredRoots) {
                    const value = walkValue(root, names);
                    if (value) return value;
                }
                return '';
            };
            const captionFromJson = () => {
                const direct = valueFromStructured(['caption_text']);
                if (direct) return direct;
                if (structuredMedia) {
                    const captionObjectText = walkValue(structuredMedia, ['text']);
                    if (captionObjectText) return captionObjectText;
                }
                const raw = firstMatch([
                    /"edge_media_to_caption"\\s*:\\s*\\{\\s*"edges"\\s*:\\s*\\[\\s*\\{\\s*"node"\\s*:\\s*\\{\\s*"text"\\s*:\\s*"((?:\\\\.|[^"\\\\])*)"/,
                    /"caption"\\s*:\\s*\\{[^{}]*"text"\\s*:\\s*"((?:\\\\.|[^"\\\\])*)"/,
                    /"caption_text"\\s*:\\s*"((?:\\\\.|[^"\\\\])*)"/,
                ]);
                return decodeJsonText(raw);
            };
            const metricFromJson = names => {
                const structured = valueFromStructured(names);
                if (structured) return structured;
                const patterns = [];
                for (const name of names) {
                    patterns.push(new RegExp(`"${name}"\\\\s*:\\\\s*"?([0-9][0-9,.]*[KMBkmb]?)"?`, 'i'));
                    patterns.push(new RegExp(`"${name}"\\\\s*:\\\\s*\\\\{\\\\s*"count"\\\\s*:\\\\s*"?([0-9][0-9,.]*[KMBkmb]?)"?`, 'i'));
                }
                return firstMatch(patterns);
            };
            const cleanCaption = value => {
                value = (value || '').replace(/\\s+/g, ' ').trim();
                value = value.replace(/^\\d[\\d,.KM万萬亿億]*\\s+likes?,\\s*\\d[\\d,.KM万萬亿億]*\\s+comments?\\s+-\\s*[^:]+:\\s*/i, '').trim();
                value = value.replace(/^\\d[\\d,.KM万萬亿億]*\\s+赞、\\s*\\d[\\d,.KM万萬亿億]*\\s+评论\\s*-\\s*[^:：]+[:：]\\s*/i, '').trim();
                value = value.replace(/^.*? on Instagram:\\s*/i, '').trim();
                value = value.replace(/^Instagram photo by .*?:\\s*/i, '').trim();
                return value.replace(/^["“]|["”]$/g, '').trim();
            };
            const looksLikeAutoAlt = value => (
                /^Photo by .+ on [A-Z][a-z]+ \\d{1,2}, \\d{4}\\.?(\\s+May be .*)?$/i.test(value || '') ||
                /^Photo shared by .+ on [A-Z][a-z]+ \\d{1,2}, \\d{4}\\.?(\\s+May be .*)?$/i.test(value || '')
            );
            // Expand truncated caption by clicking "... more"
            const moreTexts = ['more', '...more', '…more', '更多', 'さらに表示', '더 보기'];
            for (const el of document.querySelectorAll('article span, article div[role="button"]')) {
                const t = (el.textContent || '').trim().toLowerCase();
                if (moreTexts.some(mt => t === mt || t.endsWith(mt))) {
                    try { el.click(); } catch (_) {}
                    break;
                }
            }
            const lines = Array.from(document.querySelectorAll('article h1, article h2, article span, article div[dir="auto"]'))
                .map(node => text(node))
                .filter(Boolean);
            const caption = cleanCaption(
                [text(document.querySelector('article h1')),
                lines.find(line => line.length > 20 && !/^\\d+[\\d,.KM万萬亿億]*\\s/.test(line)),
                captionFromJson(),
                meta('og:description')]
                    .map(cleanCaption)
                    .find(line => line && !looksLikeAutoAlt(line)) ||
                ''
            );
            const timeNode = document.querySelector('time[datetime]');
            const bodyText = document.body ? (document.body.innerText || '') : '';
            const lineWith = patterns => {
                const bodyLines = [
                    ...bodyText.split('\\n'),
                    ...Array.from(document.querySelectorAll('[aria-label], [title]')).flatMap(node => [
                        node.getAttribute('aria-label') || '',
                        node.getAttribute('title') || '',
                    ]),
                ].map(line => line.trim()).filter(Boolean);
                const matches = bodyLines.filter(line => patterns.some(pattern => pattern.test(line)));
                return matches.find(line => /\\d/.test(line)) || matches[0] || '';
            };
            let mediaType = fallbackMediaType || '';
            const article = document.querySelector('article') || document;
            if (location.pathname.includes('/reel/') || location.pathname.includes('/tv/')) mediaType = 'reel';
            else if (!mediaType && article.querySelector('video')) mediaType = 'video';
            else if (article.querySelectorAll('img').length > 1) mediaType = mediaType || 'carousel';
            else mediaType = mediaType || 'image';
            const publishedAt = (
                (timeNode ? (timeNode.getAttribute('datetime') || '') : '') ||
                meta('article:published_time') ||
                parseEpoch(valueFromStructured(['taken_at_timestamp', 'taken_at', 'date', 'created_time', 'created_at'])) ||
                parseEpoch(firstMatch([/"taken_at_timestamp"\\s*:\\s*([0-9]+)/, /"taken_at"\\s*:\\s*([0-9]+)/, /"date"\\s*:\\s*([0-9]{10})/, /"created_time"\\s*:\\s*([0-9]{10})/]))
            );
            const comments = (
                metricFromJson(['comment_count', 'comments_count', 'edge_media_to_comment', 'edge_media_preview_comment', 'edge_media_to_parent_comment']) ||
                lineWith([/comments?/i, /评论/, /留言/])
            );
            const likes = (
                metricFromJson(['like_count', 'likes_count', 'edge_media_preview_like', 'edge_liked_by', 'preview_like_count']) ||
                lineWith([/likes?/i, /赞/, /讚/, /次赞/])
            );

            return {
                caption,
                mediaType,
                publishedAt,
                comments,
                likes,
            };
        }""",
        {"fallbackMediaType": fallback_media_type or ""},
    )


def enrich_work_detail(page, work: dict[str, str], log_callback, stop_event=None, page_timeout=None, initial_load_delay=None) -> dict[str, str]:
    if page_timeout is None:
        page_timeout = PAGE_LOAD_TIMEOUT
    if initial_load_delay is None:
        initial_load_delay = INITIAL_LOAD_DELAY
    last_rate_limit = False
    for attempt in range(RATE_LIMIT_MAX_RETRIES + 1):
        if should_stop(stop_event):
            raise InstagramStoppedError("任务已停止")
        response = page.goto(work["link"], wait_until="domcontentloaded", timeout=page_timeout)
        try:
            page.wait_for_selector('article, section', timeout=8000)
        except Exception:
            pass
        interruptible_sleep(initial_load_delay, stop_event)
        if should_stop(stop_event):
            raise InstagramStoppedError("任务已停止")
        if not is_instagram_rate_limited_page(page, response):
            last_rate_limit = False
            break
        last_rate_limit = True
        if attempt >= RATE_LIMIT_MAX_RETRIES:
            break
        interruptible_random_sleep(
            RATE_LIMIT_RETRY_DELAY_MIN_SECONDS,
            RATE_LIMIT_RETRY_DELAY_MAX_SECONDS,
            log_callback,
            stop_event,
            reason="Instagram 返回 429，暂停后重试",
        )
        if should_stop(stop_event):
            raise InstagramStoppedError("任务已停止")
    if last_rate_limit:
        raise InstagramRateLimitError("Instagram 返回 429，已触发访问频率限制")
    if is_instagram_unavailable_page(page):
        raise InstagramUnavailableWorkError("作品链接不可访问或已失效")
    detail = extract_detail_from_page(page, work.get("media_type", ""))
    return {
        "id": work.get("id", ""),
        "link": work.get("link", ""),
        "published_at": format_instagram_time(detail.get("publishedAt", "")),
        "content": build_content(detail.get("caption", ""), detail.get("mediaType", "")),
        "comments": normalize_metric_text(detail.get("comments", "")),
        "likes": normalize_metric_text(detail.get("likes", "")),
    }


def row_from_work(index: int, work: dict[str, str]) -> dict[str, str]:
    return {
        "序号": str(index),
        "作品ID": sanitize_csv_cell(work.get("id", "")),
        "作品链接": sanitize_csv_cell(work.get("link", "")),
        "发布时间": sanitize_csv_cell(work.get("published_at", "")),
        "作品内容": sanitize_csv_cell(work.get("content", "")),
        "评论数": sanitize_csv_cell(work.get("comments", "")),
        "点赞数": sanitize_csv_cell(work.get("likes", "")),
    }


def run_instagram_profile_works_spider(
    profile_urls_text: str,
    cdp_port_or_url: str = DEFAULT_X_CDP_URL,
    max_works: int = DEFAULT_MAX_WORKS,
    max_scrolls: int = DEFAULT_MAX_SCROLLS,
    log_callback=None,
    finish_callback=None,
    stop_event=None,
    config=None,
    pause_event=None,
):
    if config is None:
        config = {}
    page_timeout_val = int(config.get("page_load_timeout", PAGE_LOAD_TIMEOUT))
    scroll_delay_val = float(config.get("scroll_interval", SCROLL_DELAY))
    scroll_px_val = int(config.get("scroll_px", SCROLL_PX))
    no_new_limit_val = int(config.get("no_new_scroll_limit", NO_NEW_SCROLL_LIMIT))
    save_batch_val = int(config.get("save_batch_size", SAVE_BATCH_SIZE))
    cooldown_min_val = float(config.get("cooldown_min", COOLDOWN_MIN_SECONDS))
    cooldown_max_val = float(config.get("cooldown_max", COOLDOWN_MAX_SECONDS))
    detail_delay_min_val = float(config.get("detail_delay_min", DETAIL_DELAY_MIN_SECONDS))
    detail_delay_max_val = float(config.get("detail_delay_max", DETAIL_DELAY_MAX_SECONDS))
    initial_load_delay_val = float(config.get("initial_load_delay", INITIAL_LOAD_DELAY))
    before_detail_delay_min_val = float(config.get("before_detail_delay_min", BEFORE_DETAIL_DELAY_MIN_SECONDS))
    before_detail_delay_max_val = float(config.get("before_detail_delay_max", BEFORE_DETAIL_DELAY_MAX_SECONDS))
    max_scrolls = int(config.get("max_scrolls", max_scrolls))

    completed_path = None
    page = None
    try:
        if sync_playwright is None:
            log_error(log_callback, "缺少依赖：playwright。请先安装 requirements.txt 中的依赖。")
            return

        profile_urls = parse_profile_urls(profile_urls_text)
        if not profile_urls:
            log_warn(log_callback, "未读取到有效的 Instagram 作者主页链接。")
            return

        output_path = build_output_path("instagram", f"instagram_profile_works_{time.strftime('%Y%m%d_%H%M%S')}.xlsx", channel="profile_works")
        writer = XlsxRowWriter(output_path, CSV_FIELDS)
        serial_number = 1

        with sync_playwright() as playwright:
            log_line(log_callback, "正在连接本地 Chrome，请确认已登录 Instagram。")
            try:
                _, context = connect_existing_chromium(playwright, cdp_port_or_url, log_callback=log_callback)
            except Exception as exc:
                log_error(log_callback, f"无法连接浏览器：{exc}")
                log_error(log_callback, "连接失败：请确认 Chrome 已打开，并已登录 Instagram。")
                return

            page = context.new_page()

            for profile_index, profile_url in enumerate(profile_urls, 1):
                if should_stop(stop_event):
                    log_line(log_callback, "任务已停止。")
                    break
                if wait_if_paused(pause_event, stop_event):
                    break

                username = extract_username(profile_url)
                log_line(log_callback, f"[{profile_index}/{len(profile_urls)}] 读取作者主页：{profile_url}")
                written_count = 0
                batch_written = 0
                try:
                    links = collect_profile_work_links(page, profile_url, max_works, max_scrolls, log_callback, stop_event, pause_event, page_timeout=page_timeout_val, scroll_delay=scroll_delay_val, scroll_px=scroll_px_val, no_new_limit=no_new_limit_val, initial_load_delay=initial_load_delay_val)
                    log_line(log_callback, f"  @{username} 共收集到 {len(links)} 条作品链接，开始读取详情。")
                    if links:
                        interruptible_random_sleep(
                            before_detail_delay_min_val,
                            before_detail_delay_max_val,
                            log_callback,
                            stop_event,
                            reason="主页链接收集完成，开始详情页前",
                        )
                        if should_stop(stop_event):
                            break
                    rate_limited = False
                    for item_index, work in enumerate(links, 1):
                        if should_stop(stop_event):
                            break
                        if wait_if_paused(pause_event, stop_event):
                            break
                        try:
                            detail = enrich_work_detail(page, work, log_callback, stop_event, page_timeout=page_timeout_val, initial_load_delay=initial_load_delay_val)
                            writer.writerow(row_from_work(serial_number, detail))
                            serial_number += 1
                            written_count += 1
                            batch_written += 1
                            log_line(log_callback, f"    [{item_index}/{len(links)}] 完成：{work['link']}")
                            if item_index < len(links):
                                seconds = random.uniform(detail_delay_min_val, detail_delay_max_val)
                                log_line(log_callback, f"    详情页读取完成，随机等待 {seconds:.1f} 秒。")
                                if interruptible_sleep(seconds, stop_event):
                                    break
                            if batch_written >= save_batch_val:
                                seconds = random.uniform(cooldown_min_val, cooldown_max_val)
                                log_line(log_callback, f"    已写入 {written_count} 条，随机等待 {seconds:.1f} 秒。")
                                if interruptible_sleep(seconds, stop_event):
                                    break
                                batch_written = 0
                        except InstagramRateLimitError as exc:
                            log_warn(log_callback, f"    停止当前作者详情读取：{exc}。已保存 {written_count} 条。")
                            rate_limited = True
                            break
                        except InstagramUnavailableWorkError as exc:
                            log_warn(log_callback, f"    跳过：{work['link']}：{exc}")
                        except InstagramStoppedError:
                            log_line(log_callback, "    任务已停止。")
                            break
                        except PlaywrightTimeoutError:
                            log_warn(log_callback, f"    跳过：作品详情页加载超时：{work['link']}")
                        except Exception as exc:
                            log_warn(log_callback, f"    跳过：{work['link']}：{exc}")

                    if rate_limited and not should_stop(stop_event):
                        interruptible_random_sleep(
                            RATE_LIMIT_RETRY_DELAY_MIN_SECONDS,
                            RATE_LIMIT_RETRY_DELAY_MAX_SECONDS,
                            log_callback,
                            stop_event,
                            reason="检测到 Instagram 限流，继续下一个作者前",
                        )
                    log_line(log_callback, f"  完成 @{username}：写入 {written_count} 条。")
                except PlaywrightTimeoutError:
                    log_warn(log_callback, "  跳过：主页加载超时，请确认链接可打开且账号已登录。")
                except Exception as exc:
                    log_warn(log_callback, f"  跳过：{exc}")

        completed_path = output_path
        writer.save()
        log_line(log_callback, f"完成，已保存：{output_path}")
    finally:
        try:
            if page and not page.is_closed():
                page.close()
        except Exception:
            pass
        if finish_callback:
            finish_callback(completed_path)
