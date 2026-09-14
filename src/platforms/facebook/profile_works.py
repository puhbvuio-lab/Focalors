"""Facebook 博主作品采集核心模块。

两阶段架构：
- 阶段一：滚动主页 Feed，直接从 article 中提取帖子数据（文本/时间/指标/类型）
- 阶段二（可选）：打开帖子详情页，仅提取评论
"""
from __future__ import annotations

import re
import random
from datetime import datetime, timedelta
from typing import Any

from src.core import (
    MultiSheetXlsxWriter,
    connect_existing_chromium,
    interruptible_sleep,
    log_error,
    log_line,
    log_warn,
    should_stop,
    wait_if_paused,
    DEFAULT_X_CDP_URL,
    build_output_path,
)
from playwright.sync_api import sync_playwright

# ── 常量 ──────────────────────────────────────────────────────────────────────
IGNORE_WORDS = [
    "赞", "评论", "分享", "发送", "留言", "回复", "隐藏",
    "Like", "Comment", "Share", "Send", "Reply", "Hide",
]
PAGE_TIMEOUT_MS = 60000
SCROLL_DELAY_MS = 2000
NO_NEW_LIMIT = 5
SAVE_BATCH_SIZE = 10

# ── 帖子链接匹配模式（按优先级）───────────────────────────────────────────────
_POST_LINK_PATTERNS = [
    (r"/posts/(?!$)", 1),
    (r"/permalink\.php", 1),
    (r"story_fbid=", 1),
    (r"/reel/", 2),
    (r"/videos/", 2),
    (r"/watch/\?v=", 2),
    (r"/watch/", 2),
    (r"/photo\.php\?fbid=", 3),
    (r"/photo/\?fbid=", 3),
    (r"fbid=", 3),
]

# ── 工具函数 ──────────────────────────────────────────────────────────────────


def parse_date_range(start_date: str, end_date: str) -> tuple[datetime, datetime]:
    start_dt = datetime.strptime(start_date.strip(), "%Y-%m-%d")
    end_dt = datetime.strptime(end_date.strip(), "%Y-%m-%d")
    if start_dt > end_dt:
        raise ValueError("开始日期不能晚于结束日期。")
    return start_dt, end_dt


def parse_fb_time_string(time_str: str) -> datetime | None:
    text = (time_str or "").strip().lower()
    if not text:
        return None
    now = datetime.now()

    match = re.search(r'(\d+)\s*(小?时|分钟|天|周|min|hr|hour|day|week|month|year)s?\s*(前|ago)?', text)
    if match:
        val = int(match.group(1))
        unit = match.group(2)
        if unit in ('分钟', 'min'):
            return now - timedelta(minutes=val)
        elif unit in ('时', '小时', 'hr', 'hour'):
            return now - timedelta(hours=val)
        elif unit in ('天', 'day'):
            return now - timedelta(days=val)
        elif unit in ('周', 'week'):
            return now - timedelta(weeks=val)
        elif unit in ('month',):
            return now - timedelta(days=val * 30)
        elif unit in ('year',):
            return now - timedelta(days=val * 365)

    if "昨天" in text or "yesterday" in text:
        return now - timedelta(days=1)

    match = re.search(r'(?:(?:20)?(\d{2})年)?\s*(\d{1,2})月(\d{1,2})日', text)
    if match:
        year_str, month_str, day_str = match.groups()
        year = int("20" + year_str) if year_str else now.year
        try:
            return datetime(year, int(month_str), int(day_str))
        except ValueError:
            pass

    match = re.search(r'(\d{4})[-/年](\d{1,2})[-/月](\d{1,2})', text)
    if match:
        try:
            return datetime(int(match.group(1)), int(match.group(2)), int(match.group(3)))
        except ValueError:
            pass

    return None


def in_date_range(publish_dt: datetime | None, start_dt: datetime, end_dt: datetime) -> bool:
    if not publish_dt:
        return False
    return start_dt.date() <= publish_dt.date() <= end_dt.date()


def _get_output_path(profile_url: str) -> str:
    username = profile_url.rstrip("/").split("/")[-1].split("?")[0]
    if not username:
        username = "profile"
    date_str = datetime.now().strftime("%Y%m%d_%H%M%S")
    return build_output_path("facebook", f"facebook_{username}_{date_str}.xlsx", channel="profile_works")


def clean_fb_url(url: str) -> str:
    if not url:
        return ""
    if url.startswith("/"):
        url = "https://www.facebook.com" + url
    url = re.sub(r'[&?]__cft__[^&]*', '', url)
    url = re.sub(r'[&?]__tn__[^&]*', '', url)
    url = re.sub(r'[&?]locale=[^&]*', '', url)
    url = re.sub(r'[&?]paipv=[^&]*', '', url)
    url = re.sub(r'[&?]eav=[^&]*', '', url)
    url = re.sub(r'[&?]comment_id=[^&]*', '', url)
    url = re.sub(r'[&?]reply_comment_id=[^&]*', '', url)
    url = re.sub(r'[&?]notif_id=[^&]*', '', url)
    url = re.sub(r'[&?]ref=[^&]*', '', url)
    url = re.sub(r'[&]{2,}', '&', url)
    url = re.sub(r'\?&', '?', url)
    return url.rstrip('/').rstrip('?').rstrip('&')


# ── Excel 行映射 ──────────────────────────────────────────────────────────────

FIELDNAMES = [
    "序号", "主页链接", "帖子链接", "发布时间", "帖子内容",
    "点赞数", "评论数", "分享数", "类型",
]


def row_from_post(index: int, post: dict[str, Any], profile_url: str) -> dict[str, Any]:
    return {
        "序号": str(index),
        "主页链接": profile_url,
        "帖子链接": post.get("url", ""),
        "发布时间": post.get("published_at", ""),
        "帖子内容": post.get("content", ""),
        "点赞数": post.get("reactions", "0"),
        "评论数": post.get("comment_count", "0"),
        "分享数": post.get("shares", "0"),
        "类型": post.get("type", "4"),
    }


# ── DOM 提取工具 ──────────────────────────────────────────────────────────────

def _has_meaningful_images(container) -> bool:
    try:
        for img in container.locator('img').all():
            src = img.get_attribute("src") or ""
            if src and "emoji" not in src and "spacer" not in src and not src.startswith("data:image/svg+xml"):
                return True
    except Exception:
        pass
    return False


def _has_video_element(container) -> bool:
    try:
        return container.locator('video').count() > 0
    except Exception:
        return False


def _looks_like_metric(text: str) -> bool:
    stripped = text.strip()
    if not stripped:
        return True
    if re.fullmatch(r'[\d,\.]+[KkMm]?', stripped):
        return True
    if re.fullmatch(r'[\d,\.]+[KkMm]?\s*(个赞|条评论|次分享|次播放|likes?|comments?|shares?|views?|plays?|replies?)', stripped, re.IGNORECASE):
        return True
    words = stripped.split()
    if len(words) <= 6 and all(
        re.match(r'^[\d,\.]+[KkMm]?$', w) or w.lower() in (
            'likes', 'like', 'comments', 'comment', 'shares', 'share',
            'views', 'view', 'plays', 'play', 'replies', 'reply',
            '赞', '评论', '分享', '播放', '观看', '次', '個', '則', '条', '万', '億',
        ) for w in words
    ):
        return True
    return False


def _extract_text_content(article, page, ignore_words: list[str]) -> str:
    """从 article 中提取文本，并安全地点击 'See more/展开' 按钮展开全文。"""
    # 点击"展开/See more"（在主页 Feed 中通常是安全的，不会导航）
    try:
        buttons = article.locator('div[role="button"]').all()
        for b in buttons:
            t = b.inner_text() or ""
            if any(word in t for word in ["展开", "See more", "查看更多", "see more"]):
                try:
                    b.evaluate("node => node.click()")
                    page.wait_for_timeout(400)
                except Exception:
                    pass
    except Exception:
        pass

    text_blocks = []
    try:
        for elem in article.locator('div[dir="auto"], span[dir="auto"]').all():
            try:
                p_text = elem.inner_text().strip()
            except Exception:
                continue
            if not p_text or p_text in ignore_words:
                continue
            if _looks_like_metric(p_text):
                continue
            if not any(p_text in existing or existing in p_text for existing in text_blocks):
                text_blocks.append(p_text)
    except Exception:
        pass
    return " | ".join(text_blocks)


def _extract_time_from_article(article, page, force_exact: bool = False) -> str:
    """从主页 Feed 的 article 中提取发布时间。"""
    # 策略1：查找 a[role="link"] 中 href 含帖子特征的链接（时间戳链接）
    try:
        for link in article.locator('a[role="link"]').all():
            href_val = link.get_attribute("href") or ""
            text_val = link.inner_text().strip()
            is_time_link = (
                any(x in href_val for x in ("/posts/", "fbid=", "story_fbid=", "/watch", "/reel/", "/videos/"))
                and 1 <= len(text_val) < 20
            )
            if is_time_link:
                aria_label = link.get_attribute('aria-label') or ""
                if force_exact:
                    try:
                        link.hover()
                        page.wait_for_timeout(1000)
                        tooltip = page.locator('div[role="tooltip"]')
                        if tooltip.count() > 0:
                            return tooltip.last.inner_text().strip()
                    except Exception:
                        pass
                return aria_label or text_val
    except Exception:
        pass

    # 策略2：短文本 "2d"/"3h" 等相对时间
    try:
        for elem in article.locator('span, a').all():
            try:
                text = elem.inner_text().strip()
            except Exception:
                continue
            if re.fullmatch(r'\d+\s*[smhdw]', text, re.IGNORECASE):
                return text
            if re.fullmatch(r'\d+\s*(?:min|hr|hour|day|week|month|year|sec|second)s?\s*(?:ago|前)?', text, re.IGNORECASE):
                return text
    except Exception:
        pass

    # 策略3：搜索 aria-label 含日期关键词
    try:
        for elem in article.locator('[aria-label]').all():
            aria = (elem.get_attribute("aria-label") or "").strip()
            if not aria:
                continue
            if any(w in aria.lower() for w in (
                "月", "日", "年", "星期", "周",
                "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
                "january", "february", "march", "april", "may", "june",
                "july", "august", "september", "october", "november", "december",
            )):
                return aria
    except Exception:
        pass
    return "未获取"


def _extract_metrics(article, page) -> dict[str, str]:
    """从 article 中提取点赞/评论/分享数（多策略：role → aria → 全文正则）。"""

    def _via_role(role_name: str) -> str:
        try:
            node = article.locator(f'[data-ad-rendering-role="{role_name}"]')
            if node.count() > 0:
                num_span = node.first.locator('xpath=../..').locator('span[dir="auto"]')
                if num_span.count() > 0:
                    return num_span.first.inner_text().strip()
        except Exception:
            pass
        return ""

    def _via_aria(keywords: list[str]) -> str:
        try:
            for elem in article.locator('[aria-label]').all():
                try:
                    label = (elem.get_attribute("aria-label") or "").lower()
                except Exception:
                    continue
                if any(kw.lower() in label for kw in keywords):
                    m = re.search(r'([\d,\.]+[KkMm]?)', label)
                    if m:
                        return m.group(1)
                    try:
                        text = elem.inner_text().strip()
                        m = re.search(r'([\d,\.]+[KkMm]?)', text)
                        if m:
                            return m.group(1)
                    except Exception:
                        pass
            return ""
        except Exception:
            return ""

    def _via_article_text(keywords: list[str]) -> str:
        """全文正则搜索（Feed 中指标常以纯文本出现，如 '955 comments'）。"""
        try:
            all_text = article.inner_text() or ""
            for kw in keywords:
                m = re.search(rf'([\d,\.]+[KkMm]?)\s*{kw}', all_text, re.IGNORECASE)
                if m:
                    return m.group(1)
            return ""
        except Exception:
            return ""

    likes = _via_role("like_button") or _via_aria(["like", "赞", "讚"]) or _via_article_text(["likes", "like", "赞", "個讚", "讚"]) or "0"
    comments = _via_role("comment_button") or _via_aria(["comment", "评论", "留言"]) or _via_article_text(["comments", "comment", "条评论", "則留言", "留言"]) or "0"
    shares = _via_role("share_button") or _via_aria(["share", "分享"]) or _via_article_text(["shares", "share", "次分享"]) or "0"

    return {"reactions": likes, "comments": comments, "shares": shares}


def _detect_post_type(article, post_url: str, raw_content: str) -> str:
    """检测帖子类型码：0=视频+图片 1=图片 2=视频 3=纯文本 4=其它。"""
    url_lower = post_url.lower()
    url_suggests_video = any(x in url_lower for x in ("/videos/", "/watch", "/reel/", "v="))

    has_video = url_suggests_video or _has_video_element(article)
    has_image = _has_meaningful_images(article)

    if has_video and has_image:
        return "0"
    elif has_image:
        return "1"
    elif has_video:
        return "2"
    elif raw_content.strip():
        return "3"
    else:
        return "4"


def _build_content(has_video: bool, has_image: bool, raw_text: str) -> str:
    """构建带媒体占位符的最终帖子内容。"""
    parts = []
    if has_video:
        parts.append("[视频]")
    if has_image:
        parts.append("[图片]")
    if raw_text.strip():
        parts.append(raw_text.strip())
    return " ".join(parts) if parts else ""


def _extract_post_url_from_article(article) -> str | None:
    """从 article 中按优先级匹配帖子链接。"""
    try:
        links = article.locator('a[href]').all()
        if not links:
            links = article.locator('a').all()
        best_href = None
        best_prio = 99
        for link in links:
            try:
                href = (link.get_attribute("href") or "").strip()
            except Exception:
                continue
            if not href or href == "#":
                continue
            href_lower = href.lower()
            if any(skip in href_lower for skip in (
                "facebook.com/help", "facebook.com/policies",
                "facebook.com/privacy", "facebook.com/legal",
                "/groups/", "/hashtag/", "comment_id=", "reply_comment_id=",
                "share.php", "sharer.php", "dialog/share",
            )):
                continue
            for pattern, prio in _POST_LINK_PATTERNS:
                if prio >= best_prio:
                    break
                if re.search(pattern, href_lower):
                    best_href = href
                    best_prio = prio
                    break
            if best_prio == 1:
                break
        if best_href:
            return clean_fb_url(best_href)
    except Exception:
        pass

    # 策略B：时间戳链接（aria-label 含日期）
    try:
        for link in article.locator('a[aria-label]').all():
            try:
                aria = (link.get_attribute("aria-label") or "").strip()
            except Exception:
                continue
            if aria and any(w in aria.lower() for w in (
                "月", "日", "年", "星期", "周",
                "monday", "tuesday", "wednesday", "thursday",
                "friday", "saturday", "sunday",
                "january", "february", "march", "april", "may", "june",
                "july", "august", "september", "october", "november", "december",
            )):
                href = link.get_attribute("href") or ""
                if href and href != "#":
                    return clean_fb_url(href)
    except Exception:
        pass
    return None


# ── 阶段一：主页滚动 + 直接提取帖子数据 ──────────────────────────────────────

def _parse_article_on_feed(article, page, profile_url: str, force_exact: bool) -> dict[str, Any] | None:
    """在主页 Feed 中直接提取单个 article 的全部数据（除评论外）。"""
    post_url = _extract_post_url_from_article(article)
    if not post_url:
        return None

    # 提取文本（含"展开"点击）
    raw_text = _extract_text_content(article, page, IGNORE_WORDS)

    # 发布时间
    published_at = _extract_time_from_article(article, page, force_exact=force_exact)

    # 互动指标
    metrics = _extract_metrics(article, page)

    # 类型
    post_type = _detect_post_type(article, post_url, raw_text)

    # 媒体检测（用于内容占位符）
    url_lower = post_url.lower()
    url_suggests_video = any(x in url_lower for x in ("/videos/", "/watch", "/reel/", "v="))
    has_video = url_suggests_video or _has_video_element(article)
    has_image = _has_meaningful_images(article)
    content = _build_content(has_video, has_image, raw_text)

    return {
        "url": post_url,
        "published_at": published_at,
        "content": content[:5000],
        "type": post_type,
        "reactions": metrics["reactions"],
        "comment_count": metrics["comments"],
        "shares": metrics["shares"],
    }


def scroll_and_extract_posts(
    page,
    profile_url: str,
    max_scrolls: int,
    log_callback,
    stop_event,
    pause_event,
    scroll_delay_val: int,
    no_new_limit: int,
    max_posts: int,
    page_timeout: int = PAGE_TIMEOUT_MS,
    scroll_px: int = 800,
    force_exact: bool = False,
    post_writer=None,
    save_batch_size: int = 10,
    limit_time_bool: bool = False,
    start_dt: datetime | None = None,
    end_dt: datetime | None = None,
) -> list[dict[str, Any]]:
    """滚动主页 Feed 并直接提取帖子数据。

    若提供 post_writer，则每发现一条帖子立即写入 XLSX 并每 save_batch_size 条保存一次。
    若开启时间过滤 (limit_time_bool=True)，不符合时间范围的帖子会跳过写入但仍收集 URL。

    Returns:
        帖子列表（含 url 字段，供阶段二评论抓取使用）
    """
    log_line(log_callback, f"滚动主页收集帖子 - {profile_url}")

    # 导航
    clean_profile = profile_url.rstrip("/")
    if "/posts" not in clean_profile and "?" not in clean_profile:
        clean_profile = clean_profile + "/posts"
    page.goto(clean_profile, timeout=page_timeout, wait_until="domcontentloaded")
    page.wait_for_timeout(3000)

    # 初始滚动触发首屏内容加载
    try:
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        page.wait_for_timeout(1500)
        page.evaluate("window.scrollTo(0, 0)")
        page.wait_for_timeout(1500)
    except Exception:
        pass

    # 确认页面加载
    for sel in ['div[role="article"]', 'div[role="feed"]',
                '[aria-label*="帖子" i]', '[aria-label*="posts" i]']:
        try:
            page.wait_for_selector(sel, timeout=8000)
            log_line(log_callback, f"  ✓ 页面已加载，选择器: {sel}")
            break
        except Exception:
            continue

    posts: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    no_new_count = 0
    write_count = 0

    scroll_methods = [
        lambda: page.evaluate("window.scrollTo(0, document.body.scrollHeight)"),
        lambda: page.keyboard.press("End"),
        lambda: page.mouse.wheel(0, scroll_px),
        lambda: page.keyboard.press("PageDown"),
    ]

    for scroll_idx in range(max_scrolls):
        if should_stop(stop_event):
            break
        if wait_if_paused(pause_event, stop_event):
            break
        if len(posts) >= max_posts:
            log_line(log_callback, f"已达目标数量 ({len(posts)}/{max_posts})，停止收集。")
            break

        articles = page.locator('div[role="article"]').all()
        if not articles:
            log_warn(log_callback, f"  [!] 第 {scroll_idx + 1} 轮未找到任何 article")
        added = 0

        for article in articles:
            if len(posts) >= max_posts:
                break
            # 将 article 滚动到视口内以触发 Facebook 的懒加载
            try:
                article.evaluate("node => node.scrollIntoView({block: 'center'})")
                page.wait_for_timeout(600)
            except Exception:
                pass
            # 跳过空壳 article（内容尚未加载）
            try:
                inner = article.inner_text().strip()
                if not inner or inner == "Loading...":
                    continue
            except Exception:
                continue
            post_url = _extract_post_url_from_article(article)
            if not post_url or post_url in seen_urls:
                continue
            seen_urls.add(post_url)

            try:
                post_data = _parse_article_on_feed(article, page, profile_url, force_exact)
                if post_data:
                    # 时间过滤
                    if limit_time_bool and start_dt and end_dt:
                        pub_dt = parse_fb_time_string(post_data.get("published_at", ""))
                        if pub_dt and not in_date_range(pub_dt, start_dt, end_dt):
                            # 不符合时间范围，跳过写入但记录 URL
                            posts.append(post_data)
                            continue
                        if pub_dt:
                            post_data["published_at"] = pub_dt.strftime("%Y-%m-%d %H:%M:%S")

                    posts.append(post_data)
                    added += 1
                    # 边滚动边写入 XLSX，防止中途崩溃数据丢失
                    if post_writer is not None:
                        row = row_from_post(write_count + 1, post_data, profile_url)
                        post_writer.writerow("帖子内容", row)
                        log_line(log_callback, f"    [{write_count + 1}] {post_data.get('url', '')[:80]}")
                        write_count += 1
            except Exception:
                pass

        if added > 0:
            log_line(log_callback, f"  第 {scroll_idx + 1} 轮：扫描 {len(articles)} article → 新增 {added} 条 (累计 {len(posts)})")
            no_new_count = 0
        else:
            no_new_count += 1
            log_line(log_callback, f"  第 {scroll_idx + 1} 轮：{len(articles)} article → 无新增 (连续 {no_new_count}/{no_new_limit})")
            if no_new_count >= no_new_limit:
                log_line(log_callback, f"连续 {no_new_limit} 轮未发现新帖子，结束。")
                break

        # 轮换滚动方式
        try:
            scroll_methods[scroll_idx % len(scroll_methods)]()
            page.wait_for_timeout(scroll_delay_val)
        except Exception:
            page.wait_for_timeout(scroll_delay_val)

    log_line(log_callback, f"收集完成：共 {len(posts)} 条帖子。")
    return posts[:max_posts]


# ── 阶段二：评论提取（仅打开帖子详情页）──────────────────────────────────────

def _extract_comments_from_detail(page, post_url: str, max_comments: int) -> list[dict[str, Any]]:
    """打开帖子详情页，提取主楼评论（不含嵌套回复）。"""
    page.goto(post_url, timeout=PAGE_TIMEOUT_MS)
    page.wait_for_load_state("domcontentloaded")
    page.wait_for_timeout(3500)

    # 定位容器
    dialog = page.locator('div[role="dialog"]')
    if dialog.count() > 0:
        container = dialog.last
    else:
        container = page.locator('div[role="main"]')
        if container.count() == 0:
            container = page.locator('body')

    # 滚动加载评论
    try:
        container.evaluate("node => { node.setAttribute('tabindex', '-1'); node.focus(); }")
        page.wait_for_timeout(300)
        page.keyboard.press("PageDown")
        page.wait_for_timeout(800)
        page.mouse.wheel(0, 2000)
        page.wait_for_timeout(1200)
    except Exception:
        pass

    comments_list = []
    try:
        all_articles = container.locator('div[role="article"]').all()
        if len(all_articles) > 1:
            for comment in all_articles[1:]:
                if len(comments_list) >= max_comments:
                    break
                # 跳过嵌套回复
                try:
                    is_nested = comment.evaluate("""
                        node => {
                            let p = node.parentElement;
                            while (p) {
                                if (p.getAttribute && p.getAttribute('role') === 'article') return true;
                                p = p.parentElement;
                            }
                            return false;
                        }
                    """)
                    if is_nested:
                        continue
                except Exception:
                    pass
                # 提取评论文本
                text_parts = []
                for p_elem in comment.locator('div[dir="auto"], span[dir="auto"]').all():
                    try:
                        t = p_elem.inner_text().strip()
                    except Exception:
                        continue
                    if t and t not in IGNORE_WORDS and not _looks_like_metric(t):
                        if not any(t in e for e in text_parts):
                            text_parts.append(t)
                c_text = " | ".join(text_parts)
                if c_text:
                    comments_list.append({
                        "原帖链接": post_url,
                        "评论内容": c_text,
                        "抓取时间": datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                        "是否主楼": "是",
                    })
    except Exception:
        pass
    return comments_list


# ── 主入口 ────────────────────────────────────────────────────────────────────

def run_facebook_profile_works_spider(
    profile_urls_text: str,
    limit_time_str: str,
    start_date_str: str,
    end_date_str: str,
    force_exact_time_str: str,
    log_callback,
    finish_callback,
    stop_event,
    pause_event,
    **config
) -> None:
    urls = [u.strip() for u in profile_urls_text.splitlines() if u.strip()]
    if not urls:
        log_warn(log_callback, "未提供任何主页链接")
        if finish_callback:
            finish_callback(None)
        return

    limit_time_bool = (limit_time_str == "是")
    start_dt = None
    end_dt = None
    if limit_time_bool:
        start_dt, end_dt = parse_date_range(start_date_str, end_date_str)

    page_timeout = int(config.get("page_load_timeout", PAGE_TIMEOUT_MS))
    scroll_delay_val = int(config.get("scroll_delay", SCROLL_DELAY_MS))
    no_new_limit = int(config.get("no_new_scroll_limit", NO_NEW_LIMIT))
    max_scrolls = int(config.get("max_scrolls", 200))
    save_batch_size = int(config.get("save_batch_size", SAVE_BATCH_SIZE))
    scroll_px = int(config.get("scroll_px", 800))
    max_posts = int(config.get("max_posts", 100))
    collect_comments_bool = (config.get("collect_comments", "否") == "是")
    comment_top_limit = int(config.get("comment_top_limit", 100))
    cooldown_min_val = float(config.get("cooldown_min", 1.0))
    cooldown_max_val = float(config.get("cooldown_max", 3.0))
    force_exact = (force_exact_time_str == "是")

    browser = None
    playwright_context = None
    page = None
    try:
        with sync_playwright() as p:
            try:
                browser, playwright_context = connect_existing_chromium(p, DEFAULT_X_CDP_URL, log_callback=log_callback)
                if not browser:
                    log_error(log_callback, "无法连接到本地浏览器，请确保以调试模式启动 Chrome。")
                    return

                page = playwright_context.new_page()

                for profile_index, profile_url in enumerate(urls, 1):
                    if should_stop(stop_event):
                        break

                    log_line(log_callback, f"[{profile_index}/{len(urls)}] 读取主页：{profile_url}")

                    # ── 先初始化 XLSX（滚动前就建好，边滚边写防丢数据）────
                    output_path = _get_output_path(profile_url)
                    sheets_fields = {"帖子内容": FIELDNAMES}
                    if collect_comments_bool:
                        sheets_fields["评论详情"] = ["原帖链接", "评论内容", "抓取时间", "是否主楼"]
                    writer = MultiSheetXlsxWriter(output_path, sheets_fields)

                    # ── 阶段一：主页滚动 + 即时写入帖子 ──────────────────
                    posts = scroll_and_extract_posts(
                        page=page,
                        profile_url=profile_url,
                        max_scrolls=max_scrolls,
                        log_callback=log_callback,
                        stop_event=stop_event,
                        pause_event=pause_event,
                        scroll_delay_val=scroll_delay_val,
                        no_new_limit=no_new_limit,
                        max_posts=max_posts,
                        page_timeout=page_timeout,
                        scroll_px=scroll_px,
                        force_exact=force_exact,
                        post_writer=writer,
                        save_batch_size=save_batch_size,
                        limit_time_bool=limit_time_bool,
                        start_dt=start_dt,
                        end_dt=end_dt,
                    )

                    # 统计已写入行数（不含被时间过滤剔除的）
                    total_written = len([p for p in posts
                                         if not (limit_time_bool and start_dt and end_dt
                                                 and parse_fb_time_string(p.get("published_at", ""))
                                                 and not in_date_range(parse_fb_time_string(p.get("published_at", "")), start_dt, end_dt))])

                    if not posts:
                        log_warn(log_callback, f"未抓到任何帖子: {profile_url}")
                        continue

                    # ── 阶段二：评论（仅打开帖子详情页）─────────────────
                    comments_written = 0
                    if collect_comments_bool:
                        for post_data in posts:
                            if should_stop(stop_event):
                                break
                            if wait_if_paused(pause_event, stop_event):
                                break
                            # 被时间过滤的帖子也跳过评论
                            if limit_time_bool and start_dt and end_dt:
                                pub_dt = parse_fb_time_string(post_data.get("published_at", ""))
                                if pub_dt and not in_date_range(pub_dt, start_dt, end_dt):
                                    continue
                            try:
                                comments = _extract_comments_from_detail(page, post_data["url"], comment_top_limit)
                                for c_row in comments:
                                    writer.writerow("评论详情", c_row)
                                    comments_written += 1
                                if comments:
                                    log_line(log_callback, f"  评论: {len(comments)} 条 → {post_data['url'][:60]}")
                            except Exception as e:
                                log_error(log_callback, f"  评论提取失败: {e}")
                            delay = random.uniform(cooldown_min_val, cooldown_max_val)
                            interruptible_sleep(delay, stop_event)

                    msg = f"完成 {profile_url}：帖子 {total_written} 条"
                    if collect_comments_bool:
                        msg += f"，评论 {comments_written} 条"
                    log_line(log_callback, msg)
                    if finish_callback:
                        finish_callback(output_path)
            finally:
                if page:
                    try:
                        page.close()
                    except Exception:
                        pass
                if playwright_context:
                    try:
                        playwright_context.close()
                    except Exception:
                        pass
                if browser:
                    try:
                        browser.close()
                    except Exception:
                        pass
    except Exception as e:
        log_error(log_callback, f"运行异常: {e}")
        raise e
    finally:
        if finish_callback:
            finish_callback(output_path)


# ── 兼容 keyword_search.py 的包装器 ──────────────────────────────────────────

def collect_profile_urls(
    page, profile_url: str, max_scrolls: int,
    limit_time_bool: bool, start_dt, end_dt,
    log_callback, stop_event, pause_event,
    scroll_delay_val: int, no_new_limit: int,
    max_posts: int, skip_navigation: bool = False,
    page_timeout: int = PAGE_TIMEOUT_MS,
    scroll_px: int = 800,
) -> list[str]:
    """[兼容] 仅返回 URL 列表，供 keyword_search 使用。"""
    posts = scroll_and_extract_posts(
        page=page, profile_url=profile_url, max_scrolls=max_scrolls,
        log_callback=log_callback, stop_event=stop_event, pause_event=pause_event,
        scroll_delay_val=scroll_delay_val, no_new_limit=no_new_limit,
        max_posts=max_posts, page_timeout=page_timeout, scroll_px=scroll_px,
    )
    return [p["url"] for p in posts]


def parse_deep_post(page, url: str, collect_comments: bool = False,
                    ignore_words=None, max_comments: int = 100,
                    page_timeout: int = PAGE_TIMEOUT_MS,
                    force_exact: bool = False) -> dict[str, Any]:
    """[兼容] 打开帖子详情页提取数据，供 keyword_search 使用。"""
    page.goto(url, timeout=page_timeout)
    page.wait_for_load_state("domcontentloaded")
    page.wait_for_timeout(3500)

    dialog = page.locator('div[role="dialog"]')
    container = dialog.last if dialog.count() > 0 else page.locator('body')

    articles = container.locator('div[role="article"]').all()
    article = articles[0] if articles else container

    raw_text = _extract_text_content(article, page, ignore_words or IGNORE_WORDS)

    url_lower = url.lower()
    has_video = any(x in url_lower for x in ("/videos/", "/watch", "/reel/", "v=")) or _has_video_element(article)
    has_image = _has_meaningful_images(article)
    content = _build_content(has_video, has_image, raw_text)
    post_type = _detect_post_type(article, url, raw_text)

    published_at = _extract_time_from_article(article, page, force_exact=force_exact)
    metrics = _extract_metrics(article, page)

    comment_list = []
    if collect_comments:
        comment_list = _extract_comments_from_detail(page, url, max_comments)

    return {
        "url": url,
        "published_at": published_at,
        "content": content[:5000],
        "type": post_type,
        "reactions": metrics["reactions"],
        "comment_count": metrics["comments"],
        "shares": metrics["shares"],
        "views": "0",
        "comment_list": comment_list,
    }


# 导出供 keyword_search 使用
__all__ = [
    "log_line", "log_warn", "log_error", "parse_date_range", "parse_fb_time_string", "in_date_range",
    "row_from_post", "collect_profile_urls", "parse_deep_post",
    "PAGE_TIMEOUT_MS", "SCROLL_DELAY_MS", "NO_NEW_LIMIT", "SAVE_BATCH_SIZE",
    "run_facebook_profile_works_spider",
]
