"""
TikTok 视频评论采集模块。
该模块包含以下核心功能：
1. 拦截与主动模拟 API 请求：通过浏览器运行时抓取本地 msToken 等安全 Cookie 状态，并调用 JavaScript 加密库 `window.byted_acrawler.frontierSign` 对拼接的 TikTok 接口请求进行动态签名（生成合法的 X-Bogus），实现无验证码绕过的主动评论 API 分页抓取。
2. 备用 DOM 滚动提取：在 API 受到风控或不可用时，降级为模拟鼠标滚动并提取可见 DOM 元素中的评论数据，支持灵活解析频繁变更的类名与点赞数字段。
3. 人机验证与风控阻断检测：在页面跳转后，自动扫描 URL 和特定卡片元素以捕获人机交互阻断页，提示风控。
4. 队列冷却：批处理中设置随机间隔时间与休眠，防止请求过频触发封禁。
"""

from __future__ import annotations

import json
import re
import time
import threading
import random
from concurrent.futures import ThreadPoolExecutor, as_completed
from functools import partial
from typing import Any
from urllib.parse import urlencode, urlparse

# 尝试导入 Playwright 同步库
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
    expand_compact_number,
    interruptible_sleep,
    log_error,
    log_line,
    log_warn,
    sanitize_csv_row,
    sanitize_csv_rows,
    should_stop,
    wait_if_paused,
)


CSV_FIELDS = ["编号", "视频链接", "评论的点赞量", "评论内容", "发布时间"]
TOP_COMMENT_LIMIT = 100            # 每个视频默认导出的高点赞评论行数上限
DEFAULT_SCAN_LIMIT = 500           # 每个视频默认扫描的评论最大行数（包括风控丢弃的评论）
PAGE_LOAD_TIMEOUT = 45000          # 视频页面最大加载超时（毫秒）
COMMENT_WAIT_TIMEOUT = 12000       # 评论 DOM 元素渲染最大超时
SCROLL_PAUSE = 1.4                 # DOM 滚动模式下，两次滚动的间隔缓冲时长（秒）
NO_NEW_SCROLL_LIMIT = 8            # 连续多少次滚动没有新评论则判定到底部并终止
MAX_SCROLL_ROUNDS = 80             # 单个视频的最大滚动轮数
VIDEO_BATCH_COOLDOWN_EVERY = 3     # 每处理 N 个视频进行一次强制冷却，防止风控
VIDEO_BATCH_COOLDOWN_MIN = 4.0     # 随机强制冷却的最小秒数
VIDEO_BATCH_COOLDOWN_MAX = 9.0     # 随机强制冷却的最大秒数



def clean_url(url: str) -> str:
    """
    清洗 TikTok 视频 URL，确保前缀规范并剔除 URL hash 片段。
    """
    value = (url or "").strip()
    if not value:
        return ""
    if value.startswith("//"):
        value = "https:" + value
    if value.startswith("/"):
        value = "https://www.tiktok.com" + value
    if not value.startswith("http"):
        value = "https://" + value
    return value.split("#")[0].rstrip("/")


def extract_video_id(url: str) -> str:
    """
    从 TikTok 视频 URL 中，正则提取纯数字组成的视频 ID (video_id)。
    """
    match = re.search(r"/video/(\d+)", url or "")
    return match.group(1) if match else ""


def parse_video_entries(txt_path: str) -> list[dict[str, str]]:
    """
    解析存储视频 URL 的 TXT 文件。
    过滤掉以 # 开头的注释行与空白行，对视频 ID 进行唯一去重，返回有序的实体字典列表。
    """
    entries: list[dict[str, str]] = []
    seen_video_ids: set[str] = set()
    with open(txt_path, "r", encoding="utf-8-sig") as file:
        for line in file:
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            video_url = clean_url(stripped.split()[0])
            video_id = extract_video_id(video_url)
            if not video_id or video_id in seen_video_ids:
                continue
            seen_video_ids.add(video_id)
            entries.append({"编号": str(len(entries) + 1), "视频链接": video_url, "视频ID": video_id})
    return entries


def count_to_int(value: Any) -> int:
    """
    强转与解析统计数据（点赞、播放）为整型。
    支持处理缩写形式（如 1.2M -> 1200000），使用 expand_compact_number 辅助函数进行处理。
    """
    if value is None or isinstance(value, bool):
        return 0
    if isinstance(value, (int, float)):
        return max(0, int(value))
    text = expand_compact_number(str(value)).replace(",", "").strip()
    match = re.search(r"\d+", text)
    return int(match.group(0)) if match else 0


def detect_non_text_type(comment: dict[str, Any]) -> str:
    """
    通过扫描 JSON 字段，识别非纯文本评论的媒体类型（如贴纸、GIF、图片或视频回复）。
    """
    try:
        blob = json.dumps(comment, ensure_ascii=False).lower()
    except Exception:
        blob = " ".join(str(key).lower() for key in comment.keys())
    if "sticker" in blob or "sticker_text" in blob:
        return "贴纸"
    if "gif" in blob:
        return "GIF"
    if "image" in blob or "photo" in blob or "picture" in blob:
        return "图片"
    if "video" in blob:
        return "视频"
    return "非文本"


def normalize_comment_text(comment: dict[str, Any]) -> str:
    """
    规范化提取到的评论文本内容，过滤掉回车换行。
    若该评论不包含文本（例如纯贴纸、GIF），则使用 [类型描述] 作为占位文本。
    """
    text = str(comment.get("text") or comment.get("comment") or comment.get("content") or "").replace("\r", " ").replace("\n", " ").strip()
    if text:
        return text
    return f"[{detect_non_text_type(comment)}]"


def _format_timestamp(value) -> str:
    """
    将时间戳转换为格式化的日期时间字符串。
    支持处理 13 位毫秒级时间戳以及各种非标准空值。
    """
    if value is None:
        return ""
    try:
        ts = int(value)
        if ts > 0:
            if ts > 10_000_000_000:
                ts = ts // 1000
            return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))
    except Exception:
        pass
    text = str(value).strip()
    if text and text not in {"0", "None", "null", "undefined"}:
        return text
    return ""


def comment_like_count(comment: dict[str, Any]) -> int:
    """
    适配多种可能出现的键名以安全读取评论点赞数（如 digg_count, likeCount 等）。
    """
    for key in ("digg_count", "diggCount", "like_count", "likeCount"):
        if key in comment:
            return count_to_int(comment.get(key))
    return 0


def is_comment_list_response(url: str) -> bool:
    """
    检查拦截到的网络请求 URL 是否为 TikTok 的主楼评论列表接口（/api/comment/list），
    排除二级回复请求（包含 reply 的 URL）。
    """
    url_lower = url.lower()
    if "reply" in url_lower:
        return False
    parsed = urlparse(url)
    path = parsed.path.lower()
    return path.rstrip("/") == "/api/comment/list"


def has_more_comments(value: Any) -> bool:
    """
    判断接口中是否还有更多未拉取的评论数据。
    """
    return str(value).strip().lower() in {"1", "true"}



class CommentCollector:
    """
    评论收集与去重管理器。
    - 针对 API 响应数据和 页面 DOM 爬取的文本进行统一去重与清洗。
    - 仅保留顶级评论（主楼评论），自动识别并剔除二级回复评论。
    """
    def __init__(self, max_scan_comments: int, log_callback, comment_top_limit: int | None = None) -> None:
        self.comment_top_limit = comment_top_limit if comment_top_limit is not None else TOP_COMMENT_LIMIT
        self.max_scan_comments = max(self.comment_top_limit, int(max_scan_comments or DEFAULT_SCAN_LIMIT))
        self.log_callback = partial(log_line, log_callback)
        self.comments: list[dict[str, Any]] = []          # 最终的评论列表存储
        self.seen_ids: set[str] = set()                   # API 评论 ID 去重集合
        self.seen_dom_fingerprints: set[str] = set()       # DOM 评论特征去重集合
        self.last_has_more: int | None = None             # 记录最近一次 API 的 has_more 状态
        self.response_count = 0                           # 成功处理的 API 响应次数

    def _text_fingerprint(self, text: str) -> str:
        """
        生成文本内容特征指纹（去除空白差异）。
        """
        return re.sub(r"\s+", " ", str(text or "")).strip()

    def _has_existing_non_dom_text(self, text: str) -> bool:
        """
        验证是否存在已被 API 收集过的同文本内容的顶级评论。
        """
        fingerprint = self._text_fingerprint(text)
        return any(
            item.get("source") != "dom"
            and self._text_fingerprint(item.get("text", "")) == fingerprint
            for item in self.comments
        )

    def _dom_fingerprint(self, comment_id: str, text: str, like_count: Any) -> str:
        """
        针对 DOM 渲染的无 ID 评论，根据 [作者名称 + 文本内容] 生成复合去重指纹。
        """
        parts = str(comment_id or "").split("|", 3)
        if len(parts) >= 4 and parts[0] == "dom":
            author_key = parts[1].strip()
            if author_key:
                return f"{author_key}|{self._text_fingerprint(text)}"
        return ""

    def add_comment(self, comment_id: str, like_count: Any, text: str, source: str, create_time: str = "") -> bool:
        """
        向管理器增量添加单条评论记录。
        - 针对 comment_id 存在的一级去重：若 ID 相同但本次获取的点赞数更高，则更新点赞数和文本。
        - 针对 DOM 模式的二级去重：若 DOM 评论与已有的 API 评论内容发生碰撞，或与已保存的 DOM 指纹碰撞，则丢弃。
        """
        comment_id = str(comment_id or "").strip()
        text = str(text or "").strip()
        like_val = count_to_int(like_count)

        # 一级去重
        if comment_id and comment_id in self.seen_ids:
            for c in self.comments:
                if c["id"] == comment_id and like_val > c["like_count"]:
                    c["like_count"] = like_val
                    c["text"] = text or c["text"]
            return False

        if not comment_id:
            comment_id = f"{source}|{text[:120]}|{like_val}"

        # DOM 特征去重过滤
        is_dom_comment = source == "dom" or comment_id.startswith("dom|")
        if is_dom_comment:
            fingerprint = self._dom_fingerprint(comment_id, text, like_val)
            if (fingerprint and fingerprint in self.seen_dom_fingerprints) or self._has_existing_non_dom_text(text):
                return False
            if fingerprint:
                self.seen_dom_fingerprints.add(fingerprint)

        self.seen_ids.add(comment_id)
        self.comments.append(
            {
                "id": comment_id,
                "like_count": like_val,
                "text": text or "[非文本]",
                "order": len(self.comments),
                "source": source,
                "create_time": str(create_time or "").strip(),
            }
        )
        return True

    @staticmethod
    def _is_reply_comment(comment: dict[str, Any]) -> bool:
        """
        通过字段分析，甄别单条评论是否为非顶级的“楼中楼二级回复评论”。
        - 检测 reply_comment_id 等回复 ID 标识；
        - 对比 root_comment_id 与自身 cid 是否一致；
        - 检测 comment_type 等角色标示符；
        - 正则匹配是否以 @作者 或 '回复' 文本作为开头。
        """
        for key in ("reply_comment_id", "parent_comment_id", "reply_to_comment_cid",
                     "reply_to_comment_id", "reply_id",
                     "reply_to_user_id", "reply_to_username", "reply_to_user_name",
                     "reply_owner_id", "is_reply"):
            val = comment.get(key)
            if val is not None and str(val) not in ("", "0", "false", "False"):
                return True
        root_comment_id = str(comment.get("root_comment_id") or "").strip()
        own_comment_id = str(comment.get("cid") or comment.get("id") or "").strip()
        if root_comment_id and root_comment_id not in ("0", "false", "False") and root_comment_id != own_comment_id:
            return True
        
        ctype = comment.get("comment_type") or comment.get("comment_role") or comment.get("comment_source")
        if ctype is not None and str(ctype) not in ("", "0", "1"):
            return True
        
        text = str(comment.get("text") or comment.get("comment") or "")
        if text.strip():
            stripped = text.strip()
            if stripped.startswith("@") or stripped.startswith("回复") or stripped.startswith("Replying to"):
                return True
        return False

    def add_comments_from_payload(self, data: dict[str, Any], source: str) -> int:
        """
        解析 API 响应的 JSON 对象，将其中的 comments 列表遍历并添加。
        """
        if len(self.comments) >= self.max_scan_comments or not isinstance(data, dict):
            return 0
        self.response_count += 1
        self.last_has_more = data.get("has_more")
        comments = data.get("comments") or []
        if not isinstance(comments, list):
            return 0

        added = 0
        skipped_non_top = 0
        for comment in comments:
            if len(self.comments) >= self.max_scan_comments:
                break
            if not isinstance(comment, dict):
                continue
            if self._is_reply_comment(comment):
                skipped_non_top += 1
                continue
            comment_text = normalize_comment_text(comment)
            comment_id = str(comment.get("cid") or comment.get("id") or "").strip()
            if not comment_id:
                comment_id = f"{comment.get('create_time', '')}|{comment_text[:120]}"
            create_time = _format_timestamp(comment.get("create_time") or comment.get("createTime"))
            if self.add_comment(comment_id, comment_like_count(comment), comment_text, source, create_time):
                added += 1
        if skipped_non_top:
            self.log_callback(f"  接口返回 {skipped_non_top} 条非主楼评论对象，已忽略。")
        return added

    def handle_response(self, response) -> None:
        """
        事件监听器：在 Playwright 页面网络请求响应时触发。
        自动匹配 URL，捕获后台自动触发或被动滑动的 `/api/comment/list` 响应并提取评论。
        """
        if len(self.comments) >= self.max_scan_comments:
            return
        try:
            if not is_comment_list_response(response.url):
                return
            data = response.json()
            added = self.add_comments_from_payload(data, "api")
            if added:
                self.log_callback(f"  接口返回新增主楼评论 {added} 条，累计 {len(self.comments)} 条。")
        except Exception:
            import logging
            _logger = logging.getLogger(__name__)
            _logger.warning("handle_response failed for %s (will continue silently)", response.url)
            _logger.debug("handle_response failed", exc_info=True)
            return



def looks_blocked_or_captcha(page) -> bool:
    """
    检查页面当前是否处于风控验证码页面、登录拦截，或检测到验证滑块 iframe。
    """
    try:
        current_url = page.url.lower()
        if "captcha" in current_url or "verify" in current_url:
            return True
        if page.locator("div[id^='captcha'], iframe[src*='captcha']").count() > 0:
            return True
        return page.get_by_text(re.compile(r"verify|verification|验证码", re.I)).count() > 0
    except Exception:
        return False


def open_comment_panel(page, stop_event=None) -> bool:
    """
    备用 DOM 提取的前置条件：点击视频页面上的评论图标，展开右侧评论抽屉面板。
    - 遍历多种已知的图标/文本元素选择器并尝试点击；
    - 若没有匹配到选择器，则执行内置 JavaScript 评估算法，根据特征及坐标位置在页面内评分排序定位最可能是评论按钮的节点并触发点击。
    """
    selectors = [
        "[data-e2e='comment-icon']",
        "[data-e2e='comment-count']",
        "button[aria-label*='comment' i]",
        "button[aria-label*='评论']",
        "[aria-label*='评论']",
        "button:has-text('评论')",
    ]
    for selector in selectors:
        try:
            locator = page.locator(selector).first
            if locator.count() > 0:
                locator.click(timeout=2500)
                interruptible_sleep(1.2, stop_event)
                return True
        except Exception:
            continue
    try:
        # 使用自定义 JS 在 DOM 中扫描符合条件且最接近右侧评论位置的交互按钮
        clicked = page.evaluate(
            """() => {
                const visible = el => {
                    const rect = el.getBoundingClientRect();
                    const style = getComputedStyle(el);
                    return rect.width > 0 && rect.height > 0 &&
                        style.visibility !== 'hidden' && style.display !== 'none';
                };
                const score = el => {
                    const text = `${el.getAttribute('aria-label') || ''} ${el.innerText || ''}`.toLowerCase();
                    if (!/(comment|评论)/i.test(text)) return -1;
                    const rect = el.getBoundingClientRect();
                    let value = 0;
                    if (el.tagName === 'BUTTON' || el.getAttribute('role') === 'button') value += 10;
                    if (rect.left > window.innerWidth * 0.45) value += 5;
                    if (/\\d/.test(text)) value += 2;
                    return value;
                };
                const candidates = Array.from(document.querySelectorAll('button,[role="button"],a,div,span'))
                    .filter(visible)
                    .map(el => ({el, score: score(el)}))
                    .filter(item => item.score >= 0)
                    .sort((a, b) => b.score - a.score);
                if (!candidates.length) return false;
                candidates[0].el.click();
                return true;
            }"""
        )
        if clicked:
            interruptible_sleep(1.2, stop_event)
            return True
    except Exception:
        pass
    return False


def scroll_comments(page) -> None:
    """
    DOM 滚动辅助函数：在右侧评论列表中模拟向下滚动，以触发更多 DOM 渲染与分页加载。
    通过 JavaScript 自动定位当前具有 overflow-y 滚动属性且尺寸最大的右侧容器节点，并修改 scrollTop 距离以派发 scroll 事件。
    """
    try:
        page.evaluate(
            """() => {
                const isScrollable = el => {
                    if (!el) return false;
                    const style = getComputedStyle(el);
                    return el.scrollHeight > el.clientHeight + 80 &&
                        ['auto', 'scroll', 'overlay'].includes(style.overflowY);
                };
                const rightSide = el => {
                    const rect = el.getBoundingClientRect();
                    return rect.left > window.innerWidth * 0.45 && rect.width > 180 && rect.height > 180;
                };
                const commentNodes = Array.from(document.querySelectorAll(
                    '[data-e2e="comment-level-1"], [data-e2e="browse-comment-list"], [data-e2e="comment-list"], [class*="CommentList"], [class*="comment-list"]'
                ));
                const candidates = [];
                for (const commentNode of commentNodes) {
                    let current = commentNode;
                    while (current && current !== document.body && current !== document.documentElement) {
                        if (isScrollable(current) && rightSide(current)) {
                            candidates.push(current);
                            break;
                        }
                        current = current.parentElement;
                    }
                }
                const target = candidates
                    .sort((a, b) => (b.clientHeight * b.clientWidth) - (a.clientHeight * a.clientWidth))[0];
                if (!target) {
                    return false;
                }
                target.scrollTop = Math.min(target.scrollHeight, target.scrollTop + Math.max(500, Math.floor(target.clientHeight * 0.85)));
                target.dispatchEvent(new Event('scroll', {bubbles: true}));
                return true;
            }"""
        )
    except Exception:
        pass


def collect_visible_dom_comments(page, collector: CommentCollector, log_callback) -> int:
    """
    降级使用的 DOM 评论提取方法。
    执行复杂 JS，获取当前浏览器视口内渲染的所有主楼评论：
    - 精确过滤二级回复（如 replySelector 及以 '回复 @' 等开头的文本节点）；
    - 处理各种非纯文本表情媒体；
    - 从不断混淆变更的 DOM 中遍历与模糊提取点赞数字段；
    - 抓取评论发布时间及作者唯一 key 并生成 cid，回传至 python 端进行去重存储。
    """
    try:
        items = page.evaluate(
            """() => {
                const normalize = value => (value || '').replace(/[\\r\\n\\u2028\\u2029]+/g, ' ').replace(/\\s+/g, ' ').trim();
                const unique = nodes => Array.from(new Set(nodes.filter(Boolean)));
                const rawNodes = unique([
                    ...document.querySelectorAll('[data-e2e="comment-level-1"]'),
                    ...document.querySelectorAll('[class*="CommentItemContainer"]'),
                    ...document.querySelectorAll('[class*="DivCommentItemContainer"]')
	                ]);
	                const replySelector = [
	                    '[data-e2e="comment-level-2"]',
	                    '[data-e2e*="comment-level-2"]',
	                    '[data-e2e*="reply"]',
	                    '[class*="ReplyComment"]',
	                    '[class*="reply-comment"]',
	                    '[class*="SubComment"]',
	                    '[class*="sub-comment"]',
	                    '[class*="ChildComment"]',
	                    '[class*="ReplyItem"]',
	                    '[class*="reply-item"]'
	                ].join(', ');
	                const containerSelector = '[class*="CommentItemContainer"], [class*="DivCommentItemContainer"]';
	                const mainTextSelector = [
	                    '[data-e2e="comment-level-1"]',
	                    '[data-e2e="comment-text"]',
	                    'p[data-e2e="comment-level-1"]',
	                    '[class*="CommentText"]',
	                    '[class*="comment-text"]'
	                ].join(', ');
	                const result = [];
	                for (const rawNode of rawNodes) {
	                    // 跳过回复列表等非顶级元素
	                    if (!rawNode) continue;
	                    if (rawNode.matches(replySelector) || rawNode.closest(replySelector)) {
	                        continue;
	                    }
                    // 过滤以回复开头的残留元素
                    const quickText = normalize(rawNode.innerText || rawNode.textContent || '').substring(0, 30);
                    if (/^(回复\\s*@|Reply\\s*@|Replying to)/i.test(quickText)) {
                        continue;
                    }
	                    const node = rawNode.closest(containerSelector) || rawNode;
	                    const rawTextNodes = rawNode.matches('[data-e2e="comment-level-1"]')
	                        ? [rawNode]
	                        : unique(Array.from(node.querySelectorAll(mainTextSelector)).filter(el => {
	                            if (!el || el.closest(replySelector)) {
	                                return false;
	                            }
	                            const closestContainer = el.closest(containerSelector);
	                            if (closestContainer && closestContainer !== node) {
	                                return false;
	                            }
	                            const text = normalize(el.innerText || el.textContent || '');
	                            if (/^(回复|Reply|查看\\s*\\d+\\s*条回复|View\\s+\\d+\\s+repl)/i.test(text)) {
	                                return false;
	                            }
	                            return true;
	                        })).slice(0, 1);
	                    const textParts = [];
	                    for (const textNode of rawTextNodes) {
	                        if (!textNode) {
	                            continue;
	                        }
	                        if (textNode !== rawNode && textNode.closest(replySelector)) {
	                            continue;
	                        }
	                        const nestedTopComment = rawNode.matches('[data-e2e="comment-level-1"]')
	                            ? null
	                            : textNode.closest('[data-e2e="comment-level-1"]');
	                        if (nestedTopComment && nestedTopComment !== rawNode) {
	                            continue;
	                        }
	                        const text = normalize(textNode.innerText || textNode.textContent || '');
	                        if (!text) {
	                            continue;
                        }
                        if (/^(回复|Reply|查看\\s*\\d+\\s*条回复|View\\s+\\d+\\s+repl)/i.test(text)) {
                            continue;
                        }
                        if (/^\\d{1,2}-\\d{1,2}$/.test(text)) {
                            continue;
                        }
                        if (textParts.includes(text)) {
                            continue;
                        }
                        textParts.push(text);
                    }
                    let text = textParts.join(' ').trim();
                    let type = '';
                    if (!text) {
                        if (node.querySelector('img[src*="sticker"], [class*="Sticker"]')) {
                            type = '贴纸';
                        } else if (node.querySelector('img[src*="gif"], [class*="Gif"], [aria-label*="GIF" i]')) {
                            type = 'GIF';
                        } else if (node.querySelector('img, picture')) {
                            type = '图片';
                        } else if (node.querySelector('video')) {
                            type = '视频';
                        } else {
                            type = '非文本';
                        }
                        text = `[${type}]`;
                    }
                    let likeText = '';
                    // Broad selector search — TikTok changes class names frequently
                    const likeNodes = unique([
                        ...node.querySelectorAll('[data-e2e*="like"]'),
                        ...node.querySelectorAll('button[aria-label*="like" i]'),
                        ...node.querySelectorAll('button[aria-label*="赞" i]'),
                        ...node.querySelectorAll('[aria-label*="like" i]'),
                        ...node.querySelectorAll('[aria-label*="赞" i]'),
                        ...node.querySelectorAll('span[data-e2e*="like"]'),
                        ...node.querySelectorAll('[class*="Like"] strong, [class*="Like"] span'),
                        ...node.querySelectorAll('[class*="like"] strong, [class*="like"] span'),
	                    ]);
	                    for (const likeNode of likeNodes) {
	                        if (likeNode.closest(replySelector)) {
	                            continue;
	                        }
	                        const candidate = normalize(likeNode.innerText || likeNode.textContent || likeNode.getAttribute('aria-label') || '');
	                        if (candidate && /\\d/.test(candidate) && candidate.length < 30) {
	                            likeText = candidate;
                            break;
                        }
                    }
	                    if (!likeText) {
	                        // Scan all leaf elements for number candidates near like/heart icons
	                        const allLeaves = Array.from(node.querySelectorAll('span, strong, p, button, time, small, b, i, em'))
	                            .filter(el => !el.closest(replySelector))
	                            .filter(el => !el.children.length || el.querySelector('svg, img, [data-e2e*="like"]'));
                        const numberCandidates = [];
                        for (const leaf of allLeaves) {
                            const txt = normalize(leaf.innerText || leaf.textContent || '');
                            if (/^\\d+(?:[,.]\\d+)?\\s*(?:K|M|B|万|萬|亿|億)?$/i.test(txt)) {
                                const rect = leaf.getBoundingClientRect();
                                numberCandidates.push({text: txt, x: rect.left + rect.width / 2});
                            }
                        }
                        if (numberCandidates.length > 0) {
                            // Pick the rightmost number (likes are on the right side of comment rows)
                            numberCandidates.sort((a, b) => b.x - a.x);
                            likeText = numberCandidates[0].text;
                        } else {
                            likeText = '0';
                        }
                    }
	                    const authorLink = node.querySelector('a[href^="/@"], a[href*="tiktok.com/@"]');
	                    const authorKey = normalize(authorLink?.getAttribute('href') || authorLink?.textContent || '')
	                        .replace(/\\|/g, ' ')
	                        .slice(0, 80) || `row-${result.length}`;
	                    let timeText = '';
                    const timeEl = node.querySelector('time, [datetime], [data-e2e*="time"], span:last-child');
                    if (timeEl) {
                        timeText = normalize(timeEl.getAttribute('datetime') || timeEl.getAttribute('title') || timeEl.innerText || timeEl.textContent || '');
                    }
                    if (!timeText) {
                        const timeMatch = (node.innerText || node.textContent || '').match(/(\\d{1,2}-\\d{1,2}|\\d+\\s*(?:天|d|h|m|s|小时|分钟|秒|ago|前))/i);
                        timeText = timeMatch ? timeMatch[1] : '';
                    }
                    const cid = node.getAttribute('data-id') ||
	                        node.getAttribute('id') ||
	                        node.querySelector('a[href*="/comment/"]')?.getAttribute('href') ||
	                        `dom|${authorKey}|${text.slice(0, 120).replace(/\\|/g, ' ')}|${likeText}`;
                    result.push({id: cid, text, like_count: likeText, create_time: timeText});
                }
                return result;
            }"""
        )
    except Exception:
        return 0

    added = 0
    for item in items if isinstance(items, list) else []:
        if len(collector.comments) >= collector.max_scan_comments:
            break
        if not isinstance(item, dict):
            continue
        if collector.add_comment(item.get("id", ""), item.get("like_count", 0), item.get("text", ""), "dom", item.get("create_time", "")):
            added += 1
    if added:
        log_line(log_callback, f"  页面可见评论新增主楼评论 {added} 条，累计 {len(collector.comments)} 条。")
    return added


def build_comment_api_url(video_id: str, cursor: Any, count: int) -> str:
    """
    拼接标准的 TikTok 评论 API 获取 URL，包含必要的分页游标 cursor 与页大小 count 以及各种 aid, device_platform 等元参数。
    """
    params = {
        "aweme_id": video_id,
        "item_id": video_id,
        "count": str(count),
        "cursor": str(cursor or 0),
        "from_page": "video",
        "aid": "1988",
        "app_name": "tiktok_web",
        "device_platform": "web_pc",
        "channel": "tiktok_web",
        "browser_platform": "Win32",
        "browser_language": "zh-CN",
        "is_page_visible": "true",
        "focus_state": "true",
        "root_referer": "https://www.tiktok.com/",
    }
    return "https://www.tiktok.com/api/comment/list/?" + urlencode(params)


def build_comment_api_candidates(video_id: str, cursor: Any, count: int) -> list[str]:
    """
    构造多套候选 API 路径结构（包括精简参数版、全参数版、以及相对路径版），
    以防 TikTok 升级后单一接口 URL 被屏蔽或参数校验失败。
    """
    compact_params = {
        "aweme_id": video_id,
        "count": str(min(20, count)),
        "cursor": str(cursor or 0),
        "from_page": "video",
    }
    browser_params = {
        "aweme_id": video_id,
        "count": str(min(20, count)),
        "cursor": str(cursor or 0),
        "aid": "1988",
        "app_language": "zh-Hans",
        "app_name": "tiktok_web",
        "browser_language": "zh-CN",
        "browser_name": "Mozilla",
        "browser_online": "true",
        "browser_platform": "Win32",
        "browser_version": "5.0 (Windows)",
        "channel": "tiktok_web",
        "cookie_enabled": "true",
        "device_platform": "web_pc",
        "focus_state": "true",
        "from_page": "video",
        "history_len": "2",
        "is_fullscreen": "false",
        "is_page_visible": "true",
        "os": "windows",
        "priority_region": "",
        "referer": "",
        "region": "SG",
        "screen_height": "1080",
        "screen_width": "1920",
        "tz_name": "Asia/Shanghai",
        "webcast_language": "zh-Hans",
    }
    urls = [
        build_comment_api_url(video_id, cursor, count),
        "https://www.tiktok.com/api/comment/list/?" + urlencode(compact_params),
        "https://www.tiktok.com/api/comment/list/?" + urlencode(browser_params),
        "/api/comment/list/?" + urlencode(compact_params),
        "/api/comment/list/?" + urlencode(browser_params),
    ]
    return list(dict.fromkeys(urls))


def wait_for_tiktok_runtime(page) -> None:
    """
    等待 TikTok 页面内置的 JS 签名运行库 `byted_acrawler` 加载就绪。
    """
    try:
        page.wait_for_function(
            "() => window.byted_acrawler && (typeof window.byted_acrawler.frontierSign === 'function' || typeof window.byted_acrawler.sign === 'function')",
            timeout=5000,
        )
    except Exception:
        pass


def fetch_comments_via_page_api(page, video_id: str, collector: CommentCollector, log_callback, stop_event=None, pause_event=None, max_scroll_rounds: int | None = None) -> int:
    """
    核心 API 模拟请求函数（拦截代理与 X-Bogus/msToken 动态签名）。
    - 运行在 Playwright 页面上下文中，利用页面上的 JS 环境与当前登录态 Cookie 权限发起异步 fetch 请求。
    - 从 Cookie 中提取 `msToken`，从页面的数据源节点 `__UNIVERSAL_DATA_FOR_REHYDRATION__` 中提取 `web_id (wid)`、`region`、`appId`。
    - 核心风控绕过：调用 `window.byted_acrawler.frontierSign`（或老的 `window.byted_acrawler.sign`），将待发送的 API 链接进行动态加密签名，得到必需的安全凭证 `X-Bogus` (和 `X-Gnarly`)，拼接入请求参数。
    - 发送 fetch 异步请求，带上 Cookie 并伪造 Sec-SDK 的 CSRF 头信息。
    - 对抓取的响应 JSON 进行解析，并直接交由 `collector` 管理器存入 Python 数据区。
    """
    if not video_id:
        return 0

    wait_for_tiktok_runtime(page)
    cursor: Any = 0
    total_added = 0
    _max_rounds = max_scroll_rounds if max_scroll_rounds is not None else MAX_SCROLL_ROUNDS
    for _ in range(_max_rounds):
        if should_stop(stop_event) or len(collector.comments) >= collector.max_scan_comments:
            break
        if wait_if_paused(pause_event, stop_event):
            break

        count = min(50, collector.max_scan_comments - len(collector.comments))
        data = None
        last_error = ""
        for url in build_comment_api_candidates(video_id, cursor, count):
            try:
                # 在浏览器上下文中异步调用 fetch，防止跨域并自动继承当前登录态 Cookie
                result = page.evaluate(
                    """async (url) => {
                        const absoluteUrl = url.startsWith('/') ? `${location.origin}${url}` : url;
                        const getCookie = name => {
                            const item = document.cookie.split('; ').find(row => row.startsWith(`${name}=`));
                            return item ? decodeURIComponent(item.split('=').slice(1).join('=')) : '';
                        };
                        const urlObj = new URL(absoluteUrl);
                        const msToken = getCookie('msToken');
                        if (msToken && !urlObj.searchParams.has('msToken')) {
                            urlObj.searchParams.set('msToken', msToken);
                        }
                        try {
                            const node = document.getElementById('__UNIVERSAL_DATA_FOR_REHYDRATION__');
                            const data = node ? JSON.parse(node.textContent || '{}') : {};
                            const ctx = data?.__DEFAULT_SCOPE__?.['webapp.app-context'] || {};
                            if (ctx.wid && !urlObj.searchParams.has('web_id')) urlObj.searchParams.set('web_id', ctx.wid);
                            if (ctx.appId && !urlObj.searchParams.has('aid')) urlObj.searchParams.set('aid', String(ctx.appId));
                            if (ctx.region && !urlObj.searchParams.has('region')) urlObj.searchParams.set('region', ctx.region);
                            if (ctx.language && !urlObj.searchParams.has('app_language')) urlObj.searchParams.set('app_language', ctx.language);
                            if (ctx.language && !urlObj.searchParams.has('webcast_language')) urlObj.searchParams.set('webcast_language', ctx.language);
                        } catch (_) {}
                        const urlToSign = urlObj.toString();
                        let requestUrl = urlToSign;
                        try {
                            // 动态签名生成 X-Bogus 凭证，绕过后端签名校验
                            if (window.byted_acrawler && typeof window.byted_acrawler.frontierSign === 'function') {
                                const signResult = await window.byted_acrawler.frontierSign(urlToSign);
                                if (signResult && signResult['X-Bogus']) {
                                    const joiner = urlToSign.includes('?') ? '&' : '?';
                                    requestUrl = `${urlToSign}${joiner}X-Bogus=${encodeURIComponent(signResult['X-Bogus'])}`;
                                    if (signResult['X-Gnarly']) {
                                        requestUrl += `&X-Gnarly=${encodeURIComponent(signResult['X-Gnarly'])}`;
                                    }
                                }
                            } else if (window.byted_acrawler && typeof window.byted_acrawler.sign === 'function') {
                                const signed = await window.byted_acrawler.sign({url: urlToSign});
                                if (signed) requestUrl = signed;
                            }
                        } catch (_) {}
                        const response = await fetch(requestUrl, {
                            credentials: 'include',
                            headers: {
                                accept: 'application/json, text/plain, */*',
                                'x-secsdk-csrf-request': '1',
                                'x-secsdk-csrf-version': '1.2.22'
                            },
                            referrer: location.href,
                            referrerPolicy: 'strict-origin-when-cross-origin'
                        });
                        return {
                            ok: response.ok,
                            status: response.status,
                            text: await response.text()
                        };
                    }""",
                    url,
                )
            except Exception as exc:
                last_error = f"request error: {exc}"
                continue

            if not isinstance(result, dict):
                last_error = "empty result"
                continue
            if not result.get("ok"):
                last_error = f"HTTP {result.get('status', 'unknown')}"
                continue

            try:
                parsed = json.loads(result.get("text") or "{}")
            except Exception:
                last_error = "not JSON"
                continue

            if not isinstance(parsed, dict):
                last_error = "JSON is not object"
                continue

            has_comments_key = isinstance(parsed.get("comments"), list)
            comments = parsed.get("comments") or []
            status_code = parsed.get("status_code")
            status_msg = parsed.get("status_msg") or parsed.get("statusMsg") or ""
            if comments or (has_comments_key and status_code in (0, "0")):
                data = parsed
                break
            last_error = f"status_code={status_code}, status_msg={status_msg}"

        if data is None:
            log_error(log_callback, f"  主动评论接口失败：{last_error or '所有候选接口均失败'}")
            break

        added = collector.add_comments_from_payload(data, "api-fetch")
        total_added += added
        if added:
            log_line(log_callback, f"  主动评论接口新增主楼评论 {added} 条，累计 {len(collector.comments)} 条。")
        else:
            status_code = data.get("status_code")
            status_msg = data.get("status_msg") or data.get("statusMsg") or ""
            log_line(log_callback, f"  主动评论接口未获得主楼评论：status_code={status_code} status_msg={status_msg}")

        next_cursor = data.get("cursor") or data.get("nextCursor") or data.get("next_cursor")
        if not has_more_comments(data.get("has_more")) or not next_cursor or str(next_cursor) == str(cursor):
            break
        cursor = next_cursor
        interruptible_sleep(0.4, stop_event)

    return total_added


def collect_video_comments(page, video_url: str, max_scan_comments: int, log_callback, stop_event=None, pause_event=None, comment_top_limit: int | None = None, page_load_timeout: int | None = None, scroll_pause: float | None = None, max_scroll_rounds: int | None = None, comment_wait_timeout: int | None = None, no_new_scroll_limit: int | None = None) -> list[dict[str, Any]]:
    """
    单个视频评论收集主调度程序。
    1. 页面监听：给 Playwright 页面注册 'response' 事件处理器，拦截页面自加载或滚动的 API 请求。
    2. 跳转至视频，检查是否被风控/登录验证码阻断。
    3. 运行主动签名接口模式 fetch_comments_via_page_api。
    4. 若主动接口无返回，则降级为 DOM 模拟滚动模式：
       - 调用 open_comment_panel 打开右侧抽屉；
       - 若没打开或接口为空，尝试在抽屉中利用 `scroll_comments` 滚动并利用 `collect_visible_dom_comments` 抓取 DOM。
    5. 返回最终去重收集并排序的评论列表。
    """
    collector = CommentCollector(max_scan_comments, log_callback, comment_top_limit=comment_top_limit)
    video_id = extract_video_id(video_url)
    _page_timeout = page_load_timeout if page_load_timeout is not None else PAGE_LOAD_TIMEOUT
    _scroll_pause = scroll_pause if scroll_pause is not None else SCROLL_PAUSE
    _max_scroll_rounds = max_scroll_rounds if max_scroll_rounds is not None else MAX_SCROLL_ROUNDS
    _comment_wait_timeout = comment_wait_timeout if comment_wait_timeout is not None else COMMENT_WAIT_TIMEOUT
    _no_new_scroll_limit = no_new_scroll_limit if no_new_scroll_limit is not None else NO_NEW_SCROLL_LIMIT
    page.on("response", collector.handle_response)
    try:
        page.goto(video_url, wait_until="domcontentloaded", timeout=_page_timeout)
        interruptible_sleep(2.5, stop_event)
        if looks_blocked_or_captcha(page):
            log_warn(log_callback, "  跳过：疑似验证码或风控页面。")
            return []

        api_added = fetch_comments_via_page_api(page, video_id, collector, log_callback, stop_event=stop_event, pause_event=pause_event, max_scroll_rounds=_max_scroll_rounds)
        opened = False
        if len(collector.comments) == 0:
            opened = open_comment_panel(page, stop_event=stop_event)
        if not opened and api_added == 0 and len(collector.comments) < collector.max_scan_comments:
            log_line(log_callback, "  评论入口未能打开，改用页面上下文评论接口。")
            fetch_comments_via_page_api(page, video_id, collector, log_callback, stop_event=stop_event, pause_event=pause_event, max_scroll_rounds=_max_scroll_rounds)
        if opened:
            log_line(log_callback, "  已点击评论入口。")

        try:
            if opened:
                page.wait_for_selector("[data-e2e='comment-level-1'], [data-e2e='browse-comment-list']", timeout=_comment_wait_timeout)
        except PlaywrightTimeoutError:
            log_line(log_callback, "  未等到评论 DOM，继续通过接口响应和滚动尝试。")

        use_dom_fallback = len(collector.comments) == 0
        if use_dom_fallback:
            collect_visible_dom_comments(page, collector, log_callback)
        if opened and api_added == 0 and len(collector.comments) < collector.max_scan_comments:
            fetch_comments_via_page_api(page, video_id, collector, log_callback, stop_event=stop_event, pause_event=pause_event, max_scroll_rounds=_max_scroll_rounds)
            use_dom_fallback = len(collector.comments) == 0

        no_new_rounds = 0
        last_count = len(collector.comments)
        for round_index in range(_max_scroll_rounds):
            if should_stop(stop_event):
                log_line(log_callback, "  任务已停止。")
                break
            if wait_if_paused(pause_event, stop_event):
                break
            if len(collector.comments) >= collector.max_scan_comments:
                break
            if collector.last_has_more is not None and not has_more_comments(collector.last_has_more) and collector.response_count > 0:
                break
            if not use_dom_fallback:
                break

            scroll_comments(page)
            interruptible_sleep(_scroll_pause, stop_event)
            collect_visible_dom_comments(page, collector, log_callback)

            current_count = len(collector.comments)
            if current_count == last_count:
                no_new_rounds += 1
                if no_new_rounds >= _no_new_scroll_limit:
                    log_warn(log_callback, f"  连续 {_no_new_scroll_limit} 次滚动没有新增主楼评论，停止当前视频。")
                    break
            else:
                no_new_rounds = 0
                last_count = current_count

            if round_index and round_index % 10 == 0:
                log_line(log_callback, f"  已滚动 {round_index} 轮，累计主楼评论 {len(collector.comments)} 条。")

        return collector.comments
    finally:
        try:
            page.remove_listener("response", collector.handle_response)
        except Exception:
            pass


def build_top_rows(video_index: str, video_url: str, comments: list[dict[str, Any]], comment_top_limit: int | None = None) -> list[dict[str, str]]:
    """
    根据点赞数降序对收集到的顶级评论进行排序，并截取前 top_limit 条转换成 Excel 行字典。
    """
    top_limit = comment_top_limit if comment_top_limit is not None else TOP_COMMENT_LIMIT
    top_comments = sorted(comments, key=lambda item: (-int(item.get("like_count", 0) or 0), int(item.get("order", 0) or 0)))
    return [
        {
            "编号": video_index,
            "视频链接": video_url,
            "评论的点赞量": str(comment.get("like_count", 0)),
            "评论内容": str(comment.get("text") or ""),
            "发布时间": str(comment.get("create_time") or ""),
        }
        for comment in top_comments[:top_limit]
    ]


def empty_video_row(video_index: str, video_url: str) -> dict[str, str]:
    """
    无评论或超时跳过的视频占位行生成。
    """
    return {"编号": video_index, "视频链接": video_url, "评论的点赞量": "", "评论内容": "该视频无评论", "发布时间": ""}


def run_tiktok_top_comments_spider(
    txt_path: str,
    cdp_port_or_url: str,
    max_scan_comments: int,
    log_callback,
    finish_callback,
    stop_event=None,
    pause_event=None,
    config=None,
):
    """
    TikTok 视频高点赞评论爬虫主任务入口。
    1. 从 TXT 中读取并解析去重后的视频链接列表。
    2. 新建或接管本地已登录 Chrome 的 CDP 会话。
    3. 并发对视频调用 `collect_video_comments` 采集主楼评论并根据点赞降序保存至 Excel。
    """
    if config is None:
        config = {}
    comment_top_limit = int(config.get("comment_top_limit", TOP_COMMENT_LIMIT))
    config_page_load_timeout = int(config.get("page_load_timeout", PAGE_LOAD_TIMEOUT))
    config_scroll_pause = float(config.get("scroll_interval", SCROLL_PAUSE))
    config_max_scroll_rounds = int(config.get("max_scroll_rounds", MAX_SCROLL_ROUNDS))
    comment_wait_timeout_val = int(config.get("comment_wait_timeout", COMMENT_WAIT_TIMEOUT))
    no_new_scroll_limit_val = int(config.get("no_new_scroll_limit", NO_NEW_SCROLL_LIMIT))
    max_parallel_tabs = int(config.get("max_parallel_tabs", 3))

    video_batch_cooldown_every = int(config.get("video_batch_cooldown_every", VIDEO_BATCH_COOLDOWN_EVERY))
    video_batch_cooldown_min = float(config.get("video_batch_cooldown_min", VIDEO_BATCH_COOLDOWN_MIN))
    video_batch_cooldown_max = float(config.get("video_batch_cooldown_max", VIDEO_BATCH_COOLDOWN_MAX))

    completed_path = None
    try:
        if sync_playwright is None:
            log_line(log_callback, "缺少依赖：playwright。请先在当前运行环境执行 pip install -r requirements.txt，并执行 python -m playwright install chromium。")
            return

        entries = parse_video_entries(txt_path)
        if not entries:
            log_warn(log_callback, "TXT 中没有找到有效的 TikTok 视频链接。")
            return

        max_scan_comments = max(comment_top_limit, int(max_scan_comments or DEFAULT_SCAN_LIMIT))
        output_path = build_output_path("tiktok", f"tiktok_top_comments_{time.strftime('%Y%m%d_%H%M%S')}.xlsx", channel="top_comments")
        writer = XlsxRowWriter(output_path, CSV_FIELDS)
        writer_lock = threading.Lock()
        log_lock = threading.Lock()

        log_line(log_callback, f"输出文件：{output_path}")
        log_line(log_callback, f"最多扫描主楼评论数：{max_scan_comments}，每个视频输出点赞量前 {comment_top_limit} 条。")
        log_line(log_callback, "正在连接并拉起 Chrome...")

        # 启动多线程前，预先启动 Chrome，建立 CDP 调试端口连接
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

        def worker(entry, progress_index):
            if should_stop(stop_event):
                return
            if wait_if_paused(pause_event, stop_event):
                return

            video_index = entry["编号"]
            video_url = entry["视频链接"]
            thread_log = make_thread_log(log_callback, log_lock, f"视频 {video_index}")
            thread_log(f"[{progress_index}/{len(entries)}] 开始读取评论：{video_url}")

            browser = None
            page = None
            try:
                with sync_playwright() as playwright:
                    browser, context = connect_existing_chromium(playwright, cdp_port_or_url, log_callback=thread_log)
                    page = context.new_page()

                    comments = collect_video_comments(
                        page,
                        video_url,
                        max_scan_comments,
                        thread_log,
                        stop_event,
                        pause_event=pause_event,
                        comment_top_limit=comment_top_limit,
                        page_load_timeout=config_page_load_timeout,
                        scroll_pause=config_scroll_pause,
                        max_scroll_rounds=config_max_scroll_rounds,
                        comment_wait_timeout=comment_wait_timeout_val,
                        no_new_scroll_limit=no_new_scroll_limit_val,
                    )
                    rows = build_top_rows(video_index, video_url, comments, comment_top_limit=comment_top_limit)
                    if not rows:
                        rows = [empty_video_row(video_index, video_url)]
                    with writer_lock:
                        writer.writerows(sanitize_csv_rows(rows))
                    written_count = len([row for row in rows if row.get("评论内容") and row.get("评论内容") != "该视频无评论"])
                    thread_log(f"完成：扫描主楼评论 {len(comments)} 条，写入 {written_count} 条。")
            except PlaywrightTimeoutError:
                with writer_lock:
                    writer.writerow(sanitize_csv_row(empty_video_row(video_index, video_url)))
                    writer.save()
                thread_log("[WARN] 跳过：页面加载超时，已写入空评论占位行并保存。")
            except Exception as exc:
                with writer_lock:
                    writer.writerow(sanitize_csv_row(empty_video_row(video_index, video_url)))
                    writer.save()
                thread_log(f"[WARN] 跳过：{exc}，已写入空评论占位行并保存。")
            finally:
                try:
                    if page and not page.is_closed():
                        page.close()
                except Exception:
                    pass
                try:
                    if browser:
                        browser.close()
                except Exception:
                    pass

                # Cooldown logic
                with completed_lock:
                    nonlocal completed_count
                    completed_count += 1
                    current_completed = completed_count

                if current_completed % video_batch_cooldown_every == 0 and current_completed < len(entries):
                    cooldown_time = random.uniform(video_batch_cooldown_min, video_batch_cooldown_max)
                    log_line(log_callback, f"已处理 {current_completed} 个视频，触发批量冷却，休眠 {cooldown_time:.1f} 秒...")
                    interruptible_sleep(cooldown_time, stop_event)

        with ThreadPoolExecutor(max_workers=max_parallel_tabs) as executor:
            futures = [executor.submit(worker, entry, idx) for idx, entry in enumerate(entries, 1)]
            for future in as_completed(futures):
                if should_stop(stop_event):
                    for f in futures:
                        f.cancel()
                    break
                try:
                    future.result()
                except Exception as exc:
                    log_error(log_callback, f"线程执行异常: {exc}")

        completed_path = output_path
        with writer_lock:
            writer.save()
        log_line(log_callback, f"完成，已保存：{output_path}")
    finally:
        finish_callback(completed_path)

