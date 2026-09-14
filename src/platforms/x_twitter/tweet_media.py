"""
推文媒体地址解析。

推文详情页的 DOM 只保证渲染首屏可见的媒体：图片会被缩略成 ``name=small``，
视频只会给出 ``blob:`` 地址（无法直接使用）。因此这里优先使用 X 的公开
syndication 接口拿到原图与可下载的 mp4 地址，DOM 结果仅作为兜底。

接口与 token 生成方式来自 X 网页端的公开行为，无登录态依赖。
"""

from __future__ import annotations

import math
import re
from typing import Any, Iterable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

try:
    import requests
except ModuleNotFoundError:  # pragma: no cover - 依赖缺失时退化为无法解析
    requests = None


SYNDICATION_URL = "https://cdn.syndication.twimg.com/tweet-result"
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36"
)
_DIGITS = "0123456789abcdefghijklmnopqrstuvwxyz"
_MP4_SIZE_RE = re.compile(r"/vid/(?:avc1/)?(\d+)x(\d+)/")
_IMAGE_EXT_RE = re.compile(r"\.(jpg|jpeg|png|webp|gif)$", re.IGNORECASE)


def build_syndication_token(tweet_id: str) -> str:
    """
    生成 syndication 接口所需的 token。

    规则：``(id / 1e15) * π`` 转 36 进制（含小数部分），再去掉 ``0`` 与小数点。
    """
    value = (int(tweet_id) / 1e15) * math.pi
    integer_part = int(value)
    fraction_part = value - integer_part

    encoded = ""
    number = integer_part
    while number:
        number, remainder = divmod(number, 36)
        encoded = _DIGITS[remainder] + encoded

    decimals = ""
    for _ in range(20):
        fraction_part *= 36
        digit = int(fraction_part)
        decimals += _DIGITS[digit]
        fraction_part -= digit

    return (encoded + decimals).replace("0", "")


def dedupe(items: Iterable[str]) -> list[str]:
    """按出现顺序去重，保持原有顺序。"""
    result: list[str] = []
    seen: set[str] = set()
    for item in items:
        value = (item or "").strip()
        if not value or value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def upgrade_twitter_image_url(url: str) -> str:
    """
    把推特图片缩略图地址升级为原图地址。

    两种常见形态要区别处理：
    - ``/media/XXX.jpg?name=small``：路径本身带扩展名，去掉查询串即原图；
    - ``/media/XXX?format=jpg&name=small``：路径没有扩展名，查询串不能丢，
      否则会 404（实测 ``/media/XXX`` 直接返回 404），只能把 ``name`` 换成 ``large``。
    """
    value = (url or "").strip()
    if not value:
        return ""
    parts = urlsplit(value)
    if "pbs.twimg.com" not in parts.netloc or "/media/" not in parts.path:
        return value
    if _IMAGE_EXT_RE.search(parts.path):
        return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
    params = dict(parse_qsl(parts.query, keep_blank_values=True))
    image_format = params.get("format") or "jpg"
    query = urlencode({"format": image_format, "name": "large"})
    return urlunsplit((parts.scheme, parts.netloc, parts.path, query, ""))


def pick_best_video_variant(variants: Iterable[dict[str, Any]]) -> str:
    """从视频 variants 中挑选分辨率最高的 mp4 地址。"""
    best_url = ""
    best_rank = (-1, -1)
    for variant in variants or []:
        url = str(variant.get("src") or "")
        if not url.startswith("http"):
            continue
        content_type = str(variant.get("type") or "")
        is_mp4 = "mp4" in content_type or url.split("?")[0].lower().endswith(".mp4")
        if not is_mp4:
            continue
        size_match = _MP4_SIZE_RE.search(url)
        if size_match:
            rank = (int(size_match.group(1)), int(size_match.group(2)))
        else:
            rank = (int(variant.get("bitrate") or 0), 0)
        if rank > best_rank:
            best_rank = rank
            best_url = url
    return best_url


def extract_media_from_syndication(payload: dict[str, Any] | None) -> tuple[list[str], list[str]]:
    """
    从 syndication 响应中解析媒体地址。

    Returns:
        (图片地址列表, 视频地址列表)
    """
    if not isinstance(payload, dict):
        return [], []

    images: list[str] = []
    videos: list[str] = []

    media_details = payload.get("mediaDetails")
    if not isinstance(media_details, list):
        media_details = payload.get("photos") if isinstance(payload.get("photos"), list) else []

    for media in media_details:
        if not isinstance(media, dict):
            continue
        media_type = str(media.get("type") or "").lower()
        media_url = str(media.get("media_url_https") or media.get("media_url") or "")
        video_info = media.get("video_info") or {}
        variants = video_info.get("variants") if isinstance(video_info, dict) else None
        if variants:
            video_url = pick_best_video_variant(variants)
            if video_url:
                videos.append(video_url)
            if media_url:
                images.append(media_url)
            continue
        if media_type in ("photo", "image", "") and media_url:
            images.append(media_url)

    top_video = payload.get("video")
    if isinstance(top_video, dict):
        video_url = pick_best_video_variant(top_video.get("variants") or [])
        if video_url:
            videos.append(video_url)

    return dedupe(upgrade_twitter_image_url(url) for url in images), dedupe(videos)


def extract_quoted_tweet_id(payload: dict[str, Any] | None) -> str:
    """取出被引用推文的 ID（引用推文的媒体挂在被引用推文上）。"""
    if not isinstance(payload, dict):
        return ""
    quoted = payload.get("quoted_tweet")
    if not isinstance(quoted, dict):
        return ""
    return str(quoted.get("id_str") or "").strip()


def fetch_tweet_payload(tweet_id: str, timeout: float = 20.0) -> dict[str, Any] | None:
    """拉取 syndication 原始响应；失败返回 None，不抛异常。"""
    if requests is None or not str(tweet_id or "").strip():
        return None
    try:
        token = build_syndication_token(str(tweet_id))
    except (TypeError, ValueError):
        return None

    try:
        response = requests.get(
            SYNDICATION_URL,
            params={"id": str(tweet_id), "lang": "en", "token": token},
            headers={"User-Agent": _USER_AGENT, "Accept": "application/json"},
            timeout=timeout,
        )
        if response.status_code != 200:
            return None
        data = response.json()
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def fetch_tweet_media(tweet_id: str, timeout: float = 20.0) -> tuple[list[str], list[str]]:
    """
    通过 syndication 接口拉取推文媒体地址。

    失败时返回空列表，由调用方回退到 DOM 解析，不抛异常。
    """
    return extract_media_from_syndication(fetch_tweet_payload(tweet_id, timeout=timeout))
