from __future__ import annotations

import os
import sys
import time
import types
from dataclasses import dataclass
from datetime import datetime, timedelta

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

if "src.ui.config_dialog" not in sys.modules:
    stub_module = types.ModuleType("src.ui.config_dialog")

    @dataclass
    class ConfigParam:
        key: str
        label: str
        kind: str = "text"
        default: object = ""
        minimum: int | float | None = None
        maximum: int | float | None = None
        step: int | float | None = None
        decimals: int | None = None
        options: tuple[str, ...] = ()
        tooltip: str = ""

    stub_module.ConfigParam = ConfigParam
    sys.modules["src.ui.config_dialog"] = stub_module

from src.platforms.tiktok.keyword import (
    derive_publish_time_from_video_url,
    extract_publish_time,
    format_publish_time,
    in_date_range,
    normalize_publish_time_text,
    parse_publish_date,
)
from src.platforms.tiktok.profile_videos import parse_publish_date as parse_profile_video_publish_date


class _FakeLocator:
    def __init__(self, *, text: str = "", datetime_attr: str = "") -> None:
        self._text = text
        self._datetime_attr = datetime_attr

    @property
    def first(self):
        return self

    def count(self) -> int:
        return 1 if self._text or self._datetime_attr else 0

    def get_attribute(self, name: str):
        if name == "datetime":
            return self._datetime_attr
        return None

    def inner_text(self, timeout: int | None = None) -> str:
        return self._text


class _FakePage:
    def __init__(self, *, html: str = "", selector_map: dict[str, _FakeLocator] | None = None) -> None:
        self._html = html
        self._selector_map = selector_map or {}

    def content(self) -> str:
        return self._html

    def locator(self, selector: str) -> _FakeLocator:
        return self._selector_map.get(selector, _FakeLocator())


def test_parse_publish_date_supports_multiple_formats():
    assert parse_publish_date("2026/06/22 12:30") == datetime(2026, 6, 22)
    assert parse_publish_date("2026.06.23") == datetime(2026, 6, 23)
    assert parse_publish_date("2026年6月22日") == datetime(2026, 6, 22)

    current_year = datetime.now().year
    assert parse_publish_date("06-22") == datetime(current_year, 6, 22)
    assert parse_publish_date("6月22日") == datetime(current_year, 6, 22)
    assert parse_publish_date("6월 22일") == datetime(current_year, 6, 22)


def test_parse_publish_date_supports_relative_time():
    publish_dt = parse_publish_date("2 hours ago")
    assert publish_dt is not None
    assert datetime.now() - timedelta(hours=3) < publish_dt <= datetime.now()

    yesterday = parse_publish_date("昨天")
    assert yesterday is not None
    assert yesterday.date() == (datetime.now() - timedelta(days=1)).date()


def test_parse_publish_date_supports_localized_relative_time():
    before_now = datetime.now()
    japanese_days = parse_publish_date("3日前")
    assert japanese_days is not None
    assert before_now - timedelta(days=4) < japanese_days <= datetime.now()

    korean_hours = parse_publish_date("2시간 전")
    assert korean_hours is not None
    assert datetime.now() - timedelta(hours=3) < korean_hours <= datetime.now()

    japanese_now = parse_publish_date("たった今")
    assert japanese_now is not None
    assert before_now <= japanese_now <= datetime.now()

    assert parse_publish_date("昨日").date() == (datetime.now() - timedelta(days=1)).date()
    assert parse_publish_date("어제").date() == (datetime.now() - timedelta(days=1)).date()


def test_profile_videos_reuses_keyword_publish_date_parser():
    assert parse_profile_video_publish_date("3日前") is not None
    assert parse_profile_video_publish_date("6月22日") is not None


def test_format_publish_time_handles_millisecond_timestamp():
    timestamp_ms = 1719072000000
    expected = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(timestamp_ms // 1000))
    assert format_publish_time(str(timestamp_ms)) == expected


def test_normalize_publish_time_text_handles_iso_datetime():
    assert normalize_publish_time_text("2026-06-22T10:30:45Z") == "2026-06-22 10:30:45"


def test_derive_publish_time_from_video_url_uses_video_id_high_bits():
    unix_ts = 1719072000
    video_id = str((unix_ts << 32) + 12345)
    video_url = f"https://www.tiktok.com/@tester/video/{video_id}"
    expected = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(unix_ts))
    assert derive_publish_time_from_video_url(video_url) == expected


def test_extract_publish_time_prefers_time_datetime_attribute():
    page = _FakePage(
        selector_map={
            "time": _FakeLocator(datetime_attr="2026-06-22T10:30:45Z"),
        }
    )
    assert extract_publish_time(page) == "2026-06-22 10:30:45"


def test_in_date_range_accepts_month_day_format():
    current_year = datetime.now().year
    start_dt = datetime(current_year, 6, 1)
    end_dt = datetime(current_year, 6, 30)
    assert in_date_range("06-22", start_dt, end_dt) is True
