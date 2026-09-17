"""按 TikTok 视频链接直接采集详情。"""

from __future__ import annotations

import re
import time
from urllib.parse import urlparse

try:
    from playwright.sync_api import sync_playwright
except ModuleNotFoundError:
    sync_playwright = None

from src.core import (
    XlsxRowWriter,
    build_output_path,
    connect_existing_chromium,
    log_line,
    log_warn,
    sanitize_csv_row,
    should_stop,
    wait_if_paused,
)
from src.platforms.tiktok.profile_videos import (
    DETAIL_DELAY_MAX_SECONDS,
    DETAIL_DELAY_MIN_SECONDS,
    DETAIL_LOAD_TIMEOUT,
    extract_video_detail,
    normalize_video_url,
    wait_after_detail,
)


CSV_FIELDS = [
    "序号",
    "视频链接",
    "CC主页链接",
    "CC粉丝量",
    "视频播放量",
    "发布日期",
    "视频简介",
    "点赞数",
    "评论数",
    "收藏量",
    "分享数",
    "采集状态",
    "失败原因",
]
_TIKTOK_HOST_RE = re.compile(r"(^|\.)tiktok\.com$", re.IGNORECASE)


def clean_video_input(value: str) -> str:
    """清理单个输入，并只接受 TikTok 正式链接或分享短链。"""
    url = (value or "").strip()
    if not url:
        return ""
    if url.startswith("//"):
        url = "https:" + url
    elif not re.match(r"^https?://", url, re.IGNORECASE):
        url = "https://" + url
    parsed = urlparse(url)
    if not _TIKTOK_HOST_RE.search(parsed.hostname or ""):
        return ""
    return url.split("#", 1)[0].rstrip("/")


def parse_video_urls(txt_path: str) -> list[str]:
    """读取每行一个链接的输入，保留顺序并基于原始链接去重。"""
    urls: list[str] = []
    seen: set[str] = set()
    with open(txt_path, "r", encoding="utf-8-sig") as file:
        for line in file:
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            url = clean_video_input(stripped.split()[0])
            if url and url not in seen:
                seen.add(url)
                urls.append(url)
    return urls


def resolve_video_url(page, source_url: str, timeout: int) -> str:
    """把分享短链展开为标准视频链接；标准链接不额外访问。"""
    normalized = normalize_video_url(source_url)
    if normalized:
        return normalized
    page.goto(source_url, wait_until="domcontentloaded", timeout=timeout)
    return normalize_video_url(getattr(page, "url", ""))


def _detail_has_content(detail: dict[str, str]) -> bool:
    return any(detail.get(key) for key in ("desc", "published_at", "plays", "likes", "comments", "collects", "shares"))


def run_tiktok_video_details_spider(
    txt_path: str,
    cdp_port_or_url: str,
    log_callback,
    finish_callback,
    stop_event=None,
    pause_event=None,
    config=None,
):
    """对输入的 TikTok 视频链接逐条打开详情页并导出内容与互动指标。"""
    config = config or {}
    detail_load_timeout = int(config.get("detail_load_timeout", DETAIL_LOAD_TIMEOUT))
    delay_min = float(config.get("detail_delay_min", DETAIL_DELAY_MIN_SECONDS))
    delay_max = float(config.get("detail_delay_max", DETAIL_DELAY_MAX_SECONDS))
    output_path = None
    completed_path = None
    browser = None
    page = None

    try:
        if sync_playwright is None:
            log_warn(log_callback, "缺少依赖：playwright。请先安装 requirements.txt 中的依赖。")
            return

        source_urls = parse_video_urls(txt_path)
        if not source_urls:
            log_warn(log_callback, "没有找到有效的 TikTok 视频链接。请每行输入一个 tiktok.com 链接。")
            return

        output_path = build_output_path(
            "tiktok", f"tiktok_video_details_{time.strftime('%Y%m%d_%H%M%S')}.xlsx"
        )
        writer = XlsxRowWriter(output_path, CSV_FIELDS)

        log_line(log_callback, "正在连接已有 Chrome，不会关闭浏览器窗口...")
        from src.core import ensure_chrome_for_cdp

        ensure_chrome_for_cdp(cdp_port_or_url, log_callback=log_callback)
        with sync_playwright() as playwright:
            browser, context = connect_existing_chromium(playwright, cdp_port_or_url, log_callback=log_callback)
            page = context.new_page()

            for index, source_url in enumerate(source_urls, 1):
                if should_stop(stop_event) or wait_if_paused(pause_event, stop_event):
                    break
                row = {"序号": str(index), "视频链接": source_url, "采集状态": "失败", "失败原因": ""}
                try:
                    video_url = resolve_video_url(page, source_url, detail_load_timeout)
                    if not video_url:
                        row["失败原因"] = "链接未跳转到可识别的视频页面"
                        log_warn(log_callback, f"[{index}/{len(source_urls)}] 跳过：无法识别视频链接 {source_url}")
                    else:
                        detail = extract_video_detail(page, video_url, detail_load_timeout=detail_load_timeout)
                        row.update({
                            "视频链接": detail.get("video_url", video_url),
                            "CC主页链接": detail.get("creator_profile_url", ""),
                            "CC粉丝量": detail.get("creator_followers", ""),
                            "视频播放量": detail.get("plays", ""),
                            "发布日期": detail.get("published_at", ""),
                            "视频简介": detail.get("desc", ""),
                            "点赞数": detail.get("likes", ""),
                            "评论数": detail.get("comments", ""),
                            "收藏量": detail.get("collects", ""),
                            "分享数": detail.get("shares", ""),
                        })
                        if _detail_has_content(detail):
                            row["采集状态"] = "成功"
                            log_line(log_callback, f"[{index}/{len(source_urls)}] 已采集：{video_url}")
                        else:
                            row["采集状态"] = "数据不完整"
                            row["失败原因"] = "页面未返回可识别的视频详情，可能已删除、私密或受风控限制"
                            log_warn(log_callback, f"[{index}/{len(source_urls)}] 数据不完整：{video_url}")
                except Exception as exc:
                    row["失败原因"] = str(exc).replace("\n", " ")[:300]
                    log_warn(log_callback, f"[{index}/{len(source_urls)}] 采集失败：{source_url}，{exc}")
                writer.writerow(sanitize_csv_row(row))
                if wait_after_detail(log_callback, stop_event, pause_event, delay_min, delay_max):
                    break

        writer.save()
        completed_path = output_path
        log_line(log_callback, f"完成：共处理 {len(source_urls)} 条，已保存：{output_path}")
    finally:
        if page and not page.is_closed():
            try:
                page.close()
            except Exception:
                pass
        if browser:
            try:
                browser.close()
            except Exception:
                pass
        finish_callback(completed_path)
