from __future__ import annotations

from src.core import DEFAULT_X_CDP_URL
from src.ui.base import FieldSpec, SimpleToolWindow
from src.ui.config_dialog import ConfigParam


def _lines(value: str) -> list[str]:
    return [line.strip() for line in value.splitlines() if line.strip()]


class InstagramProfileWorksWindow(SimpleToolWindow):
    tool_id = "instagram_profile_works"

    def __init__(self) -> None:
        super().__init__(
            "Instagram 博主作品采集",
            [
                FieldSpec(
                    "profile_urls",
                    "博主主页链接，每行一个",
                    kind="text_or_file",
                    placeholder="https://www.instagram.com/username/",
                    required=True,
                ),
            ],
            height=620,
        )

    def validate_values(self, values):
        if not _lines(values["profile_urls"]):
            raise ValueError("至少需要输入一个 Instagram 博主主页链接。")

    def tool_config_params(self):
        return [
            ConfigParam("max_works", "最多作品数", kind="int", default=5000, minimum=1, maximum=100000),
            ConfigParam("detail_delay_min", "详情页间隔最小(秒)", kind="float", default=3.0, minimum=0.0, maximum=30.0, step=0.5, decimals=1),
            ConfigParam("detail_delay_max", "详情页间隔最大(秒)", kind="float", default=7.0, minimum=0.0, maximum=60.0, step=0.5, decimals=1),
            ConfigParam("initial_load_delay", "初始加载等待(秒)", kind="float", default=3.5, minimum=0.5, maximum=10.0, step=0.1, decimals=1),
            ConfigParam("before_detail_delay_min", "进入详情页前等待最小(秒)", kind="float", default=6.0, minimum=0.0, maximum=30.0, step=0.5, decimals=1),
            ConfigParam("before_detail_delay_max", "进入详情页前等待最大(秒)", kind="float", default=12.0, minimum=0.0, maximum=60.0, step=0.5, decimals=1),
        ]

    def run_task(self, values, log_callback, finish_callback, stop_event, pause_event):
        from src.platforms.instagram.works import run_instagram_profile_works_spider

        config = {k: v for k, v in values.items() if k in ("max_works", "page_load_timeout", "scroll_interval", "scroll_px", "no_new_scroll_limit", "max_scrolls", "save_batch_size", "cooldown_min", "cooldown_max", "detail_delay_min", "detail_delay_max", "initial_load_delay", "before_detail_delay_min", "before_detail_delay_max")}
        return run_instagram_profile_works_spider(
            values["profile_urls"],
            DEFAULT_X_CDP_URL,
            int(values.get("max_works", 5000)),
            int(values.get("max_scrolls", 200)),
            log_callback,
            finish_callback,
            stop_event,
            config=config,
            pause_event=pause_event,
        )
