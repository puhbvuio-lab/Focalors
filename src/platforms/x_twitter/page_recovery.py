from __future__ import annotations

import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

from src.core import interruptible_sleep, log_line, log_warn, should_stop, wait_if_paused


X_TRANSIENT_ERROR_PHRASES = (
    "問題が発生しました。再読み込みしてください。",
    "問題が発生しました",
    "再読み込みしてください",
    "やりなおす",
    "Something went wrong. Try reloading.",
    "Something went wrong",
    "Try reloading",
    "出错了",
    "发生错误",
    "请重新加载",
    "再試行",
)
X_RECOVERY_BACKOFF_SECONDS = (120.0, 180.0, 240.0)
X_NETWORK_CHECK_URL = "https://www.youtube.com/generate_204"
X_NETWORK_CHECK_TIMEOUT = 8.0
X_NETWORK_ISSUE_WAIT_SECONDS = 120.0
X_READY_SELECTOR = (
    'article[data-testid="tweet"], '
    'div[data-testid="UserName"], '
    'div[data-testid="UserDescription"], '
    'div[data-testid="primaryColumn"], '
    '[data-testid="cellInnerDiv"]'
)


@dataclass(frozen=True)
class XPageRecoveryConfig:
    backoff_seconds: tuple[float, ...] = X_RECOVERY_BACKOFF_SECONDS
    network_check_enabled: bool = True
    network_check_url: str = X_NETWORK_CHECK_URL
    network_check_timeout: float = X_NETWORK_CHECK_TIMEOUT
    network_issue_wait_seconds: float = X_NETWORK_ISSUE_WAIT_SECONDS


def _bool_from_config(value, default: bool = True) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "y", "on", "是", "启用", "开启"}:
        return True
    if text in {"0", "false", "no", "n", "off", "否", "禁用", "关闭"}:
        return False
    return default


def _float_from_config(value, default: float, minimum: float = 0.0) -> float:
    try:
        return max(minimum, float(value))
    except (TypeError, ValueError):
        return default


def resolve_x_page_recovery_config(config=None, backoff_seconds: tuple[float, ...] | None = None) -> XPageRecoveryConfig:
    if isinstance(config, XPageRecoveryConfig):
        if backoff_seconds is None:
            return config
        return XPageRecoveryConfig(
            backoff_seconds=tuple(max(0.0, float(v)) for v in backoff_seconds),
            network_check_enabled=config.network_check_enabled,
            network_check_url=config.network_check_url,
            network_check_timeout=config.network_check_timeout,
            network_issue_wait_seconds=config.network_issue_wait_seconds,
        )

    values = config if isinstance(config, dict) else {}
    if backoff_seconds is not None:
        resolved_backoff = tuple(max(0.0, float(v)) for v in backoff_seconds)
    else:
        resolved_backoff = (
            _float_from_config(values.get("x_recovery_wait_1"), X_RECOVERY_BACKOFF_SECONDS[0]),
            _float_from_config(values.get("x_recovery_wait_2"), X_RECOVERY_BACKOFF_SECONDS[1]),
            _float_from_config(values.get("x_recovery_wait_later"), X_RECOVERY_BACKOFF_SECONDS[2]),
        )
    return XPageRecoveryConfig(
        backoff_seconds=resolved_backoff or X_RECOVERY_BACKOFF_SECONDS,
        network_check_enabled=_bool_from_config(values.get("x_network_check_enabled"), default=True),
        network_check_url=str(values.get("x_network_check_url") or X_NETWORK_CHECK_URL).strip() or X_NETWORK_CHECK_URL,
        network_check_timeout=_float_from_config(values.get("x_network_check_timeout"), X_NETWORK_CHECK_TIMEOUT, minimum=1.0),
        network_issue_wait_seconds=_float_from_config(values.get("x_network_issue_wait"), X_NETWORK_ISSUE_WAIT_SECONDS),
    )


def is_x_transient_error_text(text: str) -> str:
    raw_text = str(text or "")
    normalized = re.sub(r"\s+", " ", raw_text).strip()
    for phrase in X_TRANSIENT_ERROR_PHRASES:
        if phrase in raw_text or phrase in normalized:
            return phrase
    return ""


def detect_x_transient_error(page) -> str:
    try:
        text = page.evaluate(
            """() => {
                const body = document.body;
                return body ? (body.innerText || body.textContent || '') : '';
            }"""
        )
    except Exception:
        return ""
    return is_x_transient_error_text(text)


# 推文不存在 / 用户封禁 / 页面不存在 的文案特征（多语言）
X_NOT_FOUND_PHRASES = (
    "this tweet is unavailable",
    "this tweet has been deleted",
    "this page doesn't exist",
    "hmm...this page doesn't exist",
    "this account doesn't exist",
    "try searching for another",
    "account not found",
    "account not exist",
    "no account found",
    "account suspended",
    "your account is suspended",
    "not permitted to access this feature",
    "this account has been suspended",
    "account frozen",
    "frozen account",
    "account is frozen",
    "has been frozen",
    "that post has been deleted",
    "this quoted post is unavailable",
    "post is unavailable",
    "no tweets available",
    "已被暂停",
    "已被封禁",
    "账号已被",
    "被冻结",
    "已冻结",
    "账号冻结",
    "账户冻结",
    "冻结的账号",
    "该推文不可用",
    "该推文不存在",
    "推文已被删除",
    "此推文不可用",
    "此推文不存在",
    "该页面不存在",
    "页面不存在",
    "请尝试搜索别的内容",
    "搜索别的内容",
    "ツイートは利用できません",
    "ツイートは存在しません",
    "このページは存在しません",
    "アカウントは停止されました",
    "このアカウントは存在しません",
    "アカウントが凍結",
    "tweet is unavailable",
    "post is unavailable",
    "該推文已刪除",
    "該推文不存在",
)
# 空状态容器：推文不存在/封禁页常渲染 empty state 而非 tweet 卡片
X_EMPTY_STATE_SELECTOR = (
    '[data-testid="emptyState"], '
    '[data-testid="empty_state"], '
    '[data-testid="emptyStateMessage"], '
    '[data-testid="errorToast"], '
    '[data-testid="tweetNotFound"], '
    '[data-testid="error-detail"], '
    '[data-testid="errorDetail"], '
    'div[data-testid="empty_state_button_text"]'
)


def _page_body_text(page) -> str:
    try:
        text = page.evaluate(
            """() => {
                const body = document.body;
                return body ? (body.innerText || body.textContent || '') : '';
            }"""
        )
    except Exception:
        return ""
    return re.sub(r"\s+", " ", str(text or "")).strip()


def classify_x_page_failure(page, wait_seconds: float = 2.0, stop_event=None) -> str:
    """判断目标推文 DOM 缺失时的失败原因。

    X 的"推文不存在/封禁"是 SPA 渲染的，错误状态可能比推文 DOM 超时更晚出现，
    因此在兜底判定"未加载"之前会轮询等待 error state / 文案短暂出现。

    Returns:
        "限流"   — 检测到 X 临时错误/风控提示文案（Something went wrong 等）
        "不存在" — 检测到推文不可用/删除/账号封禁/页面不存在等文案或 empty state，
                   或原 status URL 被重定向走（目标推文已不存在）
        "未登录" — 页面跳转到登录/账号页
        "未加载" — 页面正常但目标推文未渲染出来（兜底）
    """
    try:
        current_url = page.url
    except Exception:
        current_url = ""
    url_lower = (current_url or "").lower()
    if url_lower and ("login" in url_lower or "account" in url_lower):
        return "未登录"

    # 目标 status URL 被重定向走（URL 中不再包含 /status/）→ 推文已不存在/被删/无权限
    if current_url and "/status/" not in url_lower and "i/" not in url_lower and "/search" not in url_lower:
        return "不存在"

    try:
        if detect_x_transient_error(page):
            return "限流"
    except Exception:
        pass

    # 轮询等待 SPA 错误状态渲染（最多 wait_seconds 秒）
    deadline = time.monotonic() + max(0.0, float(wait_seconds or 0.0))
    while True:
        try:
            if page.locator(X_EMPTY_STATE_SELECTOR).count() > 0:
                return "不存在"
        except Exception:
            pass

        text = _page_body_text(page)
        if text:
            normalized = text.lower()
            for phrase in X_NOT_FOUND_PHRASES:
                if phrase in normalized:
                    return "不存在"

        if time.monotonic() >= deadline:
            break
        if stop_event is not None and should_stop(stop_event):
            break
        if not interruptible_sleep(0.3, stop_event):
            break

    return "未加载"


def _format_wait(seconds: float) -> str:
    seconds = max(0.0, float(seconds or 0.0))
    if seconds >= 60 and seconds % 60 == 0:
        return f"{int(seconds // 60)} 分钟"
    return f"{seconds:.0f} 秒"


def check_network_reachable(config: XPageRecoveryConfig) -> tuple[bool, str]:
    request = urllib.request.Request(
        config.network_check_url,
        headers={"User-Agent": "SocialPlatformScraper/1.0"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=config.network_check_timeout) as response:
            status = int(getattr(response, "status", 0) or 0)
            if 200 <= status < 500:
                return True, f"HTTP {status}"
            return False, f"HTTP {status}"
    except urllib.error.HTTPError as exc:
        if 200 <= int(exc.code) < 500:
            return True, f"HTTP {exc.code}"
        return False, f"HTTP {exc.code}"
    except Exception as exc:
        return False, str(exc) or exc.__class__.__name__


def _sleep_with_pause(seconds: float, stop_event=None, pause_event=None) -> bool:
    deadline = time.monotonic() + max(0.0, float(seconds or 0.0))
    while time.monotonic() < deadline:
        if should_stop(stop_event):
            return False
        if wait_if_paused(pause_event, stop_event):
            return False
        chunk = min(1.0, max(0.0, deadline - time.monotonic()))
        if interruptible_sleep(chunk, stop_event, step=0.2):
            return False
    return not should_stop(stop_event)


def wait_for_x_page_recovery(
    page,
    log_callback=None,
    page_timeout: int | None = None,
    stop_event=None,
    pause_event=None,
    context_label: str = "X 页面",
    backoff_seconds: tuple[float, ...] | None = None,
    recovery_config=None,
    network_checker=None,
    ready_selector: str = X_READY_SELECTOR,
) -> bool:
    """Wait and reload while X shows a transient reload/error screen."""
    resolved_config = resolve_x_page_recovery_config(recovery_config, backoff_seconds=backoff_seconds)
    backoff = tuple(resolved_config.backoff_seconds or X_RECOVERY_BACKOFF_SECONDS) or X_RECOVERY_BACKOFF_SECONDS
    timeout = int(page_timeout or 30000)
    attempt = 0

    while True:
        if should_stop(stop_event) or wait_if_paused(pause_event, stop_event):
            return False

        matched_phrase = detect_x_transient_error(page)
        if not matched_phrase:
            if attempt:
                log_line(log_callback, f"  {context_label} 已恢复正常，继续采集。")
            return True

        if resolved_config.network_check_enabled:
            checker = network_checker or check_network_reachable
            network_ok, network_detail = checker(resolved_config)
            if not network_ok:
                wait_seconds = resolved_config.network_issue_wait_seconds
                log_warn(
                    log_callback,
                    (
                        f"  {context_label} 出现 X 错误，同时 YouTube 网络检测失败：{network_detail}；"
                        f"判断为网络问题，等待 {_format_wait(wait_seconds)} 后刷新重试。"
                    ),
                )
                if not _sleep_with_pause(wait_seconds, stop_event=stop_event, pause_event=pause_event):
                    return False
                try:
                    page.reload(wait_until="domcontentloaded", timeout=timeout)
                except Exception as exc:
                    log_warn(log_callback, f"  {context_label} 网络等待后刷新失败，将继续重试：{exc}")
                if interruptible_sleep(2.0, stop_event):
                    return False
                continue

        wait_seconds = backoff[min(attempt, len(backoff) - 1)]
        log_warn(
            log_callback,
            (
                f"  {context_label} 出现 X 临时错误/风控提示：{matched_phrase}；"
                f"等待 {_format_wait(wait_seconds)} 后刷新重试（第 {attempt + 1} 次）。"
            ),
        )
        if not _sleep_with_pause(wait_seconds, stop_event=stop_event, pause_event=pause_event):
            return False

        try:
            page.reload(wait_until="domcontentloaded", timeout=timeout)
        except Exception as exc:
            log_warn(log_callback, f"  {context_label} 刷新失败，将继续等待重试：{exc}")

        try:
            page.wait_for_selector(ready_selector, timeout=min(timeout, 15000))
        except Exception:
            pass
        if interruptible_sleep(2.0, stop_event):
            return False
        attempt += 1
