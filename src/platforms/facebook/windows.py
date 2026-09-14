from src.ui.base import SimpleToolWindow, FieldSpec
from src.ui.config_dialog import ConfigParam
from src.platforms.facebook.profile_works import run_facebook_profile_works_spider
from src.platforms.facebook.keyword_search import run_facebook_keyword_search_spider

DEFAULT_START_DATE = "2025-06-04"
DEFAULT_END_DATE = "2026-6-4"

class FacebookProfileWorksWindow(SimpleToolWindow):
    tool_id = "facebook_profile_works"

    def __init__(self):
        super().__init__(
            "Facebook博主作品采集",
            [
                FieldSpec(
                    "profile_urls",
                    "博主主页链接，每行一个",
                    kind="text_or_file",
                    placeholder="https://www.facebook.com/username",
                    required=True,
                ),
                FieldSpec("limit_time", "是否限制时间？", kind="combo", options=("是", "否"), default="否"),
                FieldSpec("start_date", "开始日期 YYYY-MM-DD", default=DEFAULT_START_DATE),
                FieldSpec("end_date", "结束日期 YYYY-MM-DD", default=DEFAULT_END_DATE),
                FieldSpec("collect_comments", "是否采集评论？", kind="combo", options=("是", "否"), default="否"),
                FieldSpec(
                    "force_exact_time",
                    "是否强制获取精准发布时间（将显著减慢采集速度）",
                    kind="combo",
                    options=("是", "否"),
                    default="否"
                ),
            ]
        )
        self.bind_field_visibility("limit_time", "是", ["start_date", "end_date"])

    def tool_config_params(self):
        return [
            ConfigParam("max_posts", "最大采集帖子数", kind="int", default=100, minimum=1, maximum=999999),
            ConfigParam("max_scrolls", "最大滚动次数", kind="int", default=200, minimum=1, maximum=999999),
            ConfigParam("page_load_timeout", "页面加载超时(毫秒)", kind="int", default=60000, minimum=10000, maximum=120000, step=1000),
            ConfigParam("scroll_delay", "滚动延迟(毫秒)", kind="int", default=2000, minimum=500, maximum=10000, step=100),
        ]

    def run_task(self, values: dict, log_callback, finish_callback, stop_event, pause_event):
        config = {
            k: v for k, v in values.items()
            if k in (
                "max_scrolls",
                "page_load_timeout",
                "scroll_delay",
                "no_new_scroll_limit",
                "scroll_px",
                "save_batch_size",
                "max_posts",
                "collect_comments",
                "comment_top_limit",
                "cooldown_min",
                "cooldown_max",
            )
        }
        return run_facebook_profile_works_spider(
            values["profile_urls"],
            values["limit_time"],
            values.get("start_date", DEFAULT_START_DATE),
            values.get("end_date", DEFAULT_END_DATE),
            values["force_exact_time"],
            log_callback,
            finish_callback,
            stop_event,
            pause_event,
            **config
        )

class FacebookKeywordSearchWindow(SimpleToolWindow):
    tool_id = "facebook_keyword_search"

    def __init__(self):
        super().__init__(
            "Facebook关键词搜索",
            [
                FieldSpec(
                    "keywords",
                    "搜索关键词，每行一个",
                    kind="text_or_file",
                    placeholder="例如：Petit Planet",
                    required=True,
                ),
                FieldSpec("limit_time", "是否限制时间？", kind="combo", options=("是", "否"), default="否"),
                FieldSpec("start_date", "开始日期 YYYY-MM-DD", default=DEFAULT_START_DATE),
                FieldSpec("end_date", "结束日期 YYYY-MM-DD", default=DEFAULT_END_DATE),
                FieldSpec("sort_recent", "[仅搜索可用] 是否强制按最新/近期排序", kind="combo", options=("否", "是"), default="否"),
                FieldSpec("collect_comments", "是否采集评论？", kind="combo", options=("是", "否"), default="否"),
            ]
        )
        self.bind_field_visibility("limit_time", "是", ["start_date", "end_date"])

    def tool_config_params(self):
        return [
            ConfigParam("max_posts", "最大采集帖子数", kind="int", default=100, minimum=1, maximum=999999),
            ConfigParam("max_scrolls", "最大滚动次数", kind="int", default=200, minimum=1, maximum=999999),
            ConfigParam("page_load_timeout", "页面加载超时(毫秒)", kind="int", default=60000, minimum=10000, maximum=120000, step=1000),
            ConfigParam("scroll_delay", "滚动延迟(毫秒)", kind="int", default=2000, minimum=500, maximum=10000, step=100),
        ]

    def run_task(self, values: dict, log_callback, finish_callback, stop_event, pause_event):
        config = {
            k: v for k, v in values.items()
            if k in (
                "max_scrolls",
                "page_load_timeout",
                "scroll_delay",
                "no_new_scroll_limit",
                "scroll_px",
                "save_batch_size",
                "max_posts",
                "collect_comments",
                "comment_top_limit",
                "cooldown_min",
                "cooldown_max",
            )
        }
        return run_facebook_keyword_search_spider(
            values["keywords"],
            values["limit_time"],
            values.get("start_date", DEFAULT_START_DATE),
            values.get("end_date", DEFAULT_END_DATE),
            values["sort_recent"],
            log_callback,
            finish_callback,
            stop_event,
            pause_event,
            **config
        )
