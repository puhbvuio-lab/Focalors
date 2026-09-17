from __future__ import annotations

from src.core import DEFAULT_TIKTOK_CDP_URL
from src.ui.base import FieldSpec, SimpleToolWindow
from src.ui.config_dialog import ConfigParam


DEFAULT_START_DATE = "2025-05-06"
DEFAULT_END_DATE = "2026-05-06"


def _lines(value: str) -> list[str]:
    return [line.strip() for line in value.splitlines() if line.strip()]


def _parallel_windows_param() -> ConfigParam:
    return ConfigParam(
        "parallel_windows",
        "作者主页并发窗口数",
        kind="int",
        default=1,
        minimum=1,
        maximum=4,
        tooltip="作者主页阶段同时开启几个浏览器窗口；断点运行时锁会避免多个窗口进入同一作者主页。",
    )


class TikTokKeywordWindow(SimpleToolWindow):
    tool_id = "tiktok_keyword_metrics"

    def __init__(self) -> None:
        super().__init__(
            "TikTok 关键词搜索",
            [
                FieldSpec("limit_time", "是否限制时间？", kind="combo", options=("是", "否"), default="是"),
                FieldSpec("start_date", "开始日期 YYYY-MM-DD", default=DEFAULT_START_DATE),
                FieldSpec("end_date", "结束日期 YYYY-MM-DD", default=DEFAULT_END_DATE),
                FieldSpec("keywords", "关键词，每行一个", kind="text_or_file", required=True, placeholder="每行一个关键词"),
                FieldSpec("get_comments", "是否获取视频评论信息？", kind="combo", options=("是", "否"), default="否"),
                FieldSpec("max_comments", "最多获取评论数", kind="int", default=100, minimum=10, maximum=10000),
            ],
        )
        self.bind_field_visibility("limit_time", "是", ["start_date", "end_date"])
        self.bind_field_visibility("get_comments", "是", ["max_comments"])

    def validate_values(self, values):
        from src.platforms.tiktok.keyword import parse_date_range

        if not _lines(values["keywords"]):
            raise ValueError("至少需要输入一个关键词。")
        if values.get("limit_time") == "是":
            parse_date_range(values["start_date"], values["end_date"])

    def tool_config_params(self):
        return [
            ConfigParam("max_parallel_tabs", "关键词爬取并行tab数", kind="int", default=1, minimum=1, maximum=3,
                        tooltip="同时处理几个关键词。1=顺序处理。最大3。"),
            ConfigParam("max_comment_tabs", "评论爬取并行tab数", kind="int", default=1, minimum=1, maximum=3,
                        tooltip="每个关键词同时用几个tab采集评论。1=顺序。最大3。"),
            ConfigParam("max_metrics_tabs", "详情页并行tab数", kind="int", default=1, minimum=1, maximum=3,
                        tooltip="同时用几个tab提取视频详情指标。1=串行最稳（默认），2~3更快但易触发风控。"),
            ConfigParam("max_queue_size", "评论队列最大长度", kind="int", default=5000, minimum=10, maximum=10000,
                        tooltip="待爬评论链接的缓冲上限。满了则暂停采集新视频。"),
            ConfigParam("max_videos", "最多搜索结果数", kind="int", default=1000, minimum=1, maximum=5000),
            ConfigParam("max_candidates", "最多检查数", kind="int", default=3000, minimum=1, maximum=20000),
            ConfigParam("max_search_scrolls", "最大滚动次数", kind="int", default=360, minimum=30, maximum=2000, step=10),
        ]

    def run_task(self, values, log_callback, finish_callback, stop_event, pause_event):
        from src.platforms.tiktok.keyword import run_tiktok_spider

        config = {k: v for k, v in values.items() if k.startswith("tiktok_") or k in ("max_videos", "max_candidates", "scroll_interval", "max_search_scrolls", "no_new_scroll_limit", "comment_top_limit", "max_parallel_tabs", "max_comment_tabs", "max_metrics_tabs", "max_queue_size", "cooldown_min", "cooldown_max")}
        return run_tiktok_spider(
            _lines(values["keywords"]),
            int(values.get("max_videos", 1000)),
            int(values.get("max_candidates", 3000)),
            values["limit_time"],
            values["start_date"],
            values["end_date"],
            values["get_comments"],
            int(values["max_comments"]),
            DEFAULT_TIKTOK_CDP_URL,
            log_callback,
            finish_callback,
            stop_event,
            pause_event=pause_event,
            config=config,
        )


class TikTokKeywordAuthorWorksWindow(SimpleToolWindow):
    tool_id = "tiktok_keyword_author_works"

    def __init__(self) -> None:
        super().__init__(
            "TikTok 关键词作者作品采集",
            [
                FieldSpec("limit_time", "是否限制关键词命中时间？", kind="combo", options=("是", "否"), default="是"),
                FieldSpec("start_date", "开始日期 YYYY-MM-DD", default=DEFAULT_START_DATE),
                FieldSpec("end_date", "结束日期 YYYY-MM-DD", default=DEFAULT_END_DATE),
                FieldSpec("keywords", "关键词，每行一个", kind="text_or_file", required=True, placeholder="每行一个关键词"),
                FieldSpec("quick_mode", "快速模式（作品最多取最新50条）？", kind="combo", options=("是", "否"), default="是"),
            ],
            height=720,
        )
        self.bind_field_visibility("limit_time", "是", ["start_date", "end_date"])

    def validate_values(self, values):
        from src.platforms.tiktok.keyword import parse_date_range

        if not _lines(values["keywords"]):
            raise ValueError("至少需要输入一个关键词。")
        if values.get("limit_time") == "是":
            parse_date_range(values["start_date"], values["end_date"])

    def tool_config_params(self):
        return [
            _parallel_windows_param(),
            ConfigParam("max_seed_works", "关键词入口最多检查作品数", kind="int", default=300, minimum=1, maximum=5000),
            ConfigParam("max_authors", "最多进入作者主页数", kind="int", default=100, minimum=1, maximum=1000),
            ConfigParam("max_profile_works_per_author", "非快速模式每个作者最多采集作品数", kind="int", default=50, minimum=1, maximum=2000),
            ConfigParam("max_search_scrolls", "关键词搜索最大滚动次数", kind="int", default=360, minimum=5, maximum=2000),
            ConfigParam("max_profile_scrolls", "作者主页最大滚动次数", kind="int", default=500, minimum=10, maximum=2000),
            ConfigParam("page_load_timeout", "页面加载超时(毫秒)", kind="int", default=45000, minimum=10000, maximum=120000, step=1000),
            ConfigParam("scroll_interval", "搜索滚动间隔(秒)", kind="float", default=0.7, minimum=0.2, maximum=10.0, step=0.1, decimals=1),
            ConfigParam("profile_scroll_interval", "作者主页滚动间隔(秒)", kind="float", default=2.5, minimum=0.5, maximum=15.0, step=0.1, decimals=1),
            ConfigParam("no_new_scroll_limit", "连续无新增停止阈值", kind="int", default=10, minimum=2, maximum=50),
        ]

    def run_task(self, values, log_callback, finish_callback, stop_event, pause_event):
        from src.platforms.tiktok.keyword_author_works import run_tiktok_keyword_author_works_spider

        config = {k: v for k, v in values.items() if k in ("quick_mode", "parallel_windows", "max_seed_works", "max_authors", "max_profile_works_per_author", "max_search_scrolls", "max_profile_scrolls", "page_load_timeout", "scroll_interval", "profile_scroll_interval", "no_new_scroll_limit", "scroll_px", "detail_load_timeout", "detail_delay_min", "detail_delay_max")}
        return run_tiktok_keyword_author_works_spider(
            _lines(values["keywords"]),
            values["limit_time"],
            values["start_date"],
            values["end_date"],
            DEFAULT_TIKTOK_CDP_URL,
            log_callback,
            finish_callback,
            stop_event,
            pause_event=pause_event,
            config=config,
        )


class TikTokHashtagAuthorWorksWindow(SimpleToolWindow):
    tool_id = "tiktok_hashtag_author_works"

    def __init__(self) -> None:
        super().__init__(
            "TikTok 话题作者作品采集",
            [
                FieldSpec("limit_time", "是否限制话题命中时间？", kind="combo", options=("是", "否"), default="是"),
                FieldSpec("start_date", "开始日期 YYYY-MM-DD", default=DEFAULT_START_DATE),
                FieldSpec("end_date", "结束日期 YYYY-MM-DD", default=DEFAULT_END_DATE),
                FieldSpec(
                    "hashtags",
                    "话题关键词，每行一个",
                    kind="text_or_file",
                    required=True,
                    placeholder="palworld\nmonster taming games\n#monsterhunter",
                ),
                FieldSpec("quick_mode", "快速模式（作品最多取最新50条）？", kind="combo", options=("是", "否"), default="是"),
            ],
            height=720,
        )
        self.bind_field_visibility("limit_time", "是", ["start_date", "end_date"])

    def validate_values(self, values):
        from src.platforms.tiktok.hashtag_author_works import parse_hashtag_sources
        from src.platforms.tiktok.keyword import parse_date_range

        if not parse_hashtag_sources(_lines(values["hashtags"]), skip_invalid=True):
            raise ValueError("至少需要输入一个有效的话题关键词。")
        if values.get("limit_time") == "是":
            parse_date_range(values["start_date"], values["end_date"])

    def tool_config_params(self):
        return [
            _parallel_windows_param(),
            ConfigParam("max_seed_works", "每个话题最多检查种子视频数", kind="int", default=300, minimum=1, maximum=5000),
            ConfigParam("max_authors", "每个话题最多进入作者主页数", kind="int", default=300, minimum=1, maximum=2000),
            ConfigParam("max_profile_works_per_author", "非快速模式每个作者最多采集作品数", kind="int", default=50, minimum=1, maximum=2000),
            ConfigParam("max_topic_scrolls", "话题页最大滚动次数", kind="int", default=360, minimum=5, maximum=2000),
            ConfigParam("max_profile_scrolls", "作者主页最大滚动次数", kind="int", default=500, minimum=10, maximum=2000),
            ConfigParam("page_load_timeout", "页面加载超时(毫秒)", kind="int", default=45000, minimum=10000, maximum=120000, step=1000),
            ConfigParam("scroll_interval", "话题页滚动间隔(秒)", kind="float", default=0.7, minimum=0.2, maximum=10.0, step=0.1, decimals=1),
            ConfigParam("profile_scroll_interval", "作者主页滚动间隔(秒)", kind="float", default=2.5, minimum=0.5, maximum=15.0, step=0.1, decimals=1),
            ConfigParam("no_new_scroll_limit", "连续无新增停止阈值", kind="int", default=10, minimum=2, maximum=50),
        ]

    def run_task(self, values, log_callback, finish_callback, stop_event, pause_event):
        from src.platforms.tiktok.hashtag_author_works import run_tiktok_hashtag_author_works_spider

        config = {k: v for k, v in values.items() if k in ("quick_mode", "parallel_windows", "max_seed_works", "max_authors", "max_profile_works_per_author", "max_topic_scrolls", "max_profile_scrolls", "page_load_timeout", "scroll_interval", "profile_scroll_interval", "no_new_scroll_limit", "scroll_px", "detail_load_timeout", "detail_delay_min", "detail_delay_max")}
        return run_tiktok_hashtag_author_works_spider(
            _lines(values["hashtags"]),
            values["limit_time"],
            values["start_date"],
            values["end_date"],
            DEFAULT_TIKTOK_CDP_URL,
            log_callback,
            finish_callback,
            stop_event,
            pause_event=pause_event,
            config=config,
        )


class TikTokProfilesWindow(SimpleToolWindow):
    tool_id = "tiktok_profile_directory"

    def __init__(self) -> None:
        super().__init__(
            "TikTok 博主信息",
            [FieldSpec("txt_path", "博主主页链接，每行一个", kind="text_or_file", required=True, placeholder="https://www.tiktok.com/@username")],
        )

    def tool_config_params(self):
        return [
            ConfigParam("captcha_wait", "验证码等待时间(秒)", kind="int", default=12, minimum=5, maximum=120),
            ConfigParam("cooldown_every", "冷却间隔(个)", kind="int", default=5, minimum=1, maximum=50),
            ConfigParam("max_parallel_tabs", "并发Tab数", kind="int", default=3, minimum=1, maximum=5, tooltip="同时打开浏览器Tab采样的线程/数量"),
        ]

    def run_task(self, values, log_callback, finish_callback, stop_event, pause_event):
        from src.platforms.tiktok.profiles import run_tiktok_profile_spider

        config = {k: v for k, v in values.items() if k in ("page_load_timeout", "captcha_wait", "cooldown_every", "cooldown_min", "cooldown_max", "max_parallel_tabs")}
        return run_tiktok_profile_spider(self._text_to_tempfile(values["txt_path"]), DEFAULT_TIKTOK_CDP_URL, log_callback, finish_callback, stop_event, pause_event=pause_event, config=config)


class TikTokProfileVideosWindow(SimpleToolWindow):
    tool_id = "tiktok_profile_videos"

    def __init__(self) -> None:
        super().__init__(
            "TikTok 博主视频采集",
            [
                FieldSpec("txt_path", "博主主页链接，每行一个", kind="text_or_file", required=True, placeholder="https://www.tiktok.com/@username"),
                FieldSpec("limit_time", "是否限制时间？", kind="combo", options=("是", "否"), default="是"),
                FieldSpec("start_date", "开始日期 YYYY-MM-DD", default=DEFAULT_START_DATE),
                FieldSpec("end_date", "结束日期 YYYY-MM-DD", default=DEFAULT_END_DATE),
                FieldSpec("fetch_play_counts", "是否爬取播放量？", kind="combo", options=("否", "是"), default="否"),
                FieldSpec("get_comments", "是否获取视频评论信息？", kind="combo", options=("是", "否"), default="否"),
                FieldSpec("max_comments", "最多获取评论数", kind="int", default=100, minimum=10, maximum=10000),
            ],
            height=820,
        )
        self.bind_field_visibility("limit_time", "是", ["start_date", "end_date"])
        self.bind_field_visibility("get_comments", "是", ["max_comments"])

    def validate_values(self, values):
        from src.platforms.tiktok.profile_videos import parse_date_range

        if values.get("limit_time") == "是":
            parse_date_range(values["start_date"], values["end_date"])

    def tool_config_params(self):
        return [
            ConfigParam("link_batch_size", "每批处理视频数", kind="int", default=50, minimum=5, maximum=200),
            ConfigParam("detail_load_timeout", "详情页加载超时(毫秒)", kind="int", default=30000, minimum=10000, maximum=120000, step=1000),
            ConfigParam("detail_delay_min", "详情页间隔最小(秒)", kind="float", default=2.0, minimum=0.0, maximum=30.0, step=0.5, decimals=1),
            ConfigParam("detail_delay_max", "详情页间隔最大(秒)", kind="float", default=5.0, minimum=0.0, maximum=60.0, step=0.5, decimals=1),
            ConfigParam("max_parallel_tabs", "并发Tab数", kind="int", default=3, minimum=1, maximum=5, tooltip="同时开启并发博主爬取的主页面数"),
        ]

    def run_task(self, values, log_callback, finish_callback, stop_event, pause_event):
        from src.platforms.tiktok.profile_videos import run_tiktok_profile_videos_spider

        config = {k: v for k, v in values.items() if k in ("page_load_timeout", "scroll_interval", "no_new_scroll_limit", "max_scrolls", "link_batch_size", "save_batch_size", "cooldown_min", "cooldown_max", "detail_load_timeout", "detail_delay_min", "detail_delay_max", "scroll_px", "max_parallel_tabs")}
        return run_tiktok_profile_videos_spider(
            self._text_to_tempfile(values["txt_path"]),
            values["start_date"],
            values["end_date"],
            values["limit_time"],
            int(values.get("max_scrolls", 200)),
            "是",
            values["get_comments"],
            int(values["max_comments"]),
            values.get("fetch_play_counts", "否"),
            DEFAULT_TIKTOK_CDP_URL,
            log_callback,
            finish_callback,
            stop_event,
            pause_event=pause_event,
            config=config,
        )


class TikTokVideoDetailsWindow(SimpleToolWindow):
    tool_id = "tiktok_video_details"

    def __init__(self) -> None:
        super().__init__(
            "TikTok 视频链接采集",
            [
                FieldSpec(
                    "txt_path",
                    "视频链接，每行一个",
                    kind="text_or_file",
                    required=True,
                    placeholder="https://www.tiktok.com/@username/video/123\nhttps://vm.tiktok.com/xxxx/",
                ),
            ],
        )

    def tool_config_params(self):
        return [
            ConfigParam("detail_load_timeout", "详情页加载超时(毫秒)", kind="int", default=30000, minimum=10000, maximum=120000, step=1000),
            ConfigParam("detail_delay_min", "详情页间隔最小(秒)", kind="float", default=2.0, minimum=0.0, maximum=30.0, step=0.5, decimals=1),
            ConfigParam("detail_delay_max", "详情页间隔最大(秒)", kind="float", default=5.0, minimum=0.0, maximum=60.0, step=0.5, decimals=1),
        ]

    def run_task(self, values, log_callback, finish_callback, stop_event, pause_event):
        from src.platforms.tiktok.video_details import run_tiktok_video_details_spider

        config = {key: values[key] for key in ("detail_load_timeout", "detail_delay_min", "detail_delay_max") if key in values}
        return run_tiktok_video_details_spider(
            self._text_to_tempfile(values["txt_path"]),
            DEFAULT_TIKTOK_CDP_URL,
            log_callback,
            finish_callback,
            stop_event,
            pause_event=pause_event,
            config=config,
        )


class TikTokProfilePlayCountsWindow(SimpleToolWindow):
    tool_id = "tiktok_profile_play_counts"

    def __init__(self) -> None:
        super().__init__(
            "TikTok 博主视频播放量",
            [
                FieldSpec("txt_path", "博主主页链接，每行一个", kind="text_or_file", required=True, placeholder="https://www.tiktok.com/@username"),
            ],
        )

    def tool_config_params(self):
        return [
            ConfigParam("max_parallel_tabs", "并发Tab数", kind="int", default=3, minimum=1, maximum=5, tooltip="同时并发获取播放量的博主主页数"),
        ]

    def run_task(self, values, log_callback, finish_callback, stop_event, pause_event):
        from src.platforms.tiktok.profile_play_counts import run_tiktok_profile_play_counts_spider

        config = {k: v for k, v in values.items() if k in ("page_load_timeout", "scroll_interval", "no_new_scroll_limit", "max_scrolls", "max_parallel_tabs")}
        return run_tiktok_profile_play_counts_spider(
            self._text_to_tempfile(values["txt_path"]),
            DEFAULT_TIKTOK_CDP_URL,
            int(values.get("max_scrolls", 200)),
            log_callback,
            finish_callback,
            stop_event,
            pause_event=pause_event,
            config=config,
        )


class TikTokContextWindow(SimpleToolWindow):
    tool_id = "tiktok_paired_context_metrics"

    def __init__(self) -> None:
        super().__init__(
            "TikTok 视频上下文数据",
            [FieldSpec("txt_path", "视频链接 + 博主主页，每行一对", kind="text_or_file", required=True, placeholder="视频链接 博主主页链接")],
        )

    def tool_config_params(self):
        return [
            ConfigParam("context_size", "目标视频前后各取几条", kind="int", default=5, minimum=1, maximum=20),
            ConfigParam("api_page_size", "每页条数", kind="int", default=35, minimum=10, maximum=100),
            ConfigParam("max_api_pages", "最多翻页数", kind="int", default=10, minimum=1, maximum=100),
            ConfigParam("max_profile_scrolls", "主页最大滚动次数", kind="int", default=80, minimum=10, maximum=500),
            ConfigParam("max_parallel_tabs", "并发Tab数", kind="int", default=3, minimum=1, maximum=5, tooltip="同时并发提取上下文信息的线程数"),
        ]

    def run_task(self, values, log_callback, finish_callback, stop_event, pause_event):
        from src.platforms.tiktok.context import run_scraper

        config = {k: v for k, v in values.items() if k in ("context_size", "api_page_size", "max_api_pages", "max_profile_scrolls", "scroll_interval", "max_parallel_tabs")}
        return run_scraper(self._text_to_tempfile(values["txt_path"]), DEFAULT_TIKTOK_CDP_URL, log_callback, finish_callback, stop_event, pause_event=pause_event, config=config)


class TikTokCommentsWindow(SimpleToolWindow):
    tool_id = "tiktok_top_comments"

    def __init__(self) -> None:
        super().__init__(
            "TikTok 热门评论",
            [
                FieldSpec("max_scan_comments", "每个视频最多扫描评论数", kind="int", default=500, minimum=100, maximum=10000),
                FieldSpec("txt_path", "视频链接，每行一个", kind="text_or_file", required=True, placeholder="https://www.tiktok.com/@user/video/123"),
            ],
        )

    def tool_config_params(self):
        return [
            ConfigParam("max_scroll_rounds", "最大滚动次数", kind="int", default=80, minimum=5, maximum=500),
            ConfigParam("comment_wait_timeout", "评论等待超时(毫秒)", kind="int", default=12000, minimum=5000, maximum=60000, step=1000),
            ConfigParam("video_batch_cooldown_every", "视频批量冷却间隔(个)", kind="int", default=3, minimum=1, maximum=50),
            ConfigParam("video_batch_cooldown_min", "视频批量冷却最小(秒)", kind="float", default=4.0, minimum=0.0, maximum=60.0, step=0.5, decimals=1),
            ConfigParam("video_batch_cooldown_max", "视频批量冷却最大(秒)", kind="float", default=9.0, minimum=0.0, maximum=120.0, step=0.5, decimals=1),
            ConfigParam("max_parallel_tabs", "并发Tab数", kind="int", default=3, minimum=1, maximum=5, tooltip="同时并发爬取评论的视频页面数"),
        ]

    def run_task(self, values, log_callback, finish_callback, stop_event, pause_event):
        from src.platforms.tiktok.comments import run_tiktok_top_comments_spider

        config = {k: v for k, v in values.items() if k.startswith("tiktok_") or k in ("page_load_timeout", "scroll_interval", "max_scroll_rounds", "comment_top_limit", "comment_wait_timeout", "no_new_scroll_limit", "video_batch_cooldown_every", "video_batch_cooldown_min", "video_batch_cooldown_max", "max_parallel_tabs")}
        return run_tiktok_top_comments_spider(
            self._text_to_tempfile(values["txt_path"]),
            DEFAULT_TIKTOK_CDP_URL,
            int(values["max_scan_comments"]),
            log_callback,
            finish_callback,
            stop_event,
            pause_event=pause_event,
            config=config,
        )


class TikTokKeywordProWindow(SimpleToolWindow):
    tool_id = "tiktok_keyword_metrics_pro"

    def __init__(self) -> None:
        super().__init__(
            "TikTok 关键词搜索 Pro",
            [
                FieldSpec("limit_time", "是否限制时间？", kind="combo", options=("是", "否"), default="是"),
                FieldSpec("start_date", "开始日期 YYYY-MM-DD", default=DEFAULT_START_DATE),
                FieldSpec("end_date", "结束日期 YYYY-MM-DD", default=DEFAULT_END_DATE),
                FieldSpec("keywords", "关键词，每行一个", kind="text_or_file", required=True, placeholder="每行一个关键词"),
                FieldSpec("get_comments", "是否获取视频评论信息？", kind="combo", options=("是", "否"), default="否"),
                FieldSpec("max_comments", "最多获取评论数", kind="int", default=100, minimum=10, maximum=10000),
                FieldSpec("enable_timer", "是否开启定时重复运行？", kind="combo", options=("否", "是"), default="否"),
                FieldSpec("timer_interval_minutes", "运行间隔 (分钟)", kind="int", default=60, minimum=1, maximum=10080),
                FieldSpec("timer_max_runs", "最大运行次数", kind="int", default=3, minimum=2, maximum=10000),
            ],
            height=800,
        )
        self.bind_field_visibility("limit_time", "是", ["start_date", "end_date"])
        self.bind_field_visibility("get_comments", "是", ["max_comments"])
        self.bind_field_visibility("enable_timer", "是", ["timer_interval_minutes", "timer_max_runs"])

    def validate_values(self, values):
        from src.platforms.tiktok.keyword import parse_date_range

        if not _lines(values["keywords"]):
            raise ValueError("至少需要输入一个关键词。")
        if values.get("enable_timer") == "是" and values.get("limit_time") != "是":
            raise ValueError("定时模式必须开启时间过滤，否则每轮采集结果完全相同。")
        if values.get("limit_time") == "是":
            parse_date_range(values["start_date"], values["end_date"])

    def tool_config_params(self):
        return [
            ConfigParam("max_parallel_tabs", "关键词爬取并行tab数", kind="int", default=1, minimum=1, maximum=3,
                        tooltip="同时处理几个关键词。1=顺序处理。最大3。"),
            ConfigParam("max_comment_tabs", "评论爬取并行tab数", kind="int", default=1, minimum=1, maximum=3,
                        tooltip="每个关键词同时用几个tab采集评论。1=顺序。最大3。"),
            ConfigParam("max_metrics_tabs", "详情页并行tab数", kind="int", default=1, minimum=1, maximum=3,
                        tooltip="同时用几个tab提取视频详情指标。1=串行最稳（默认），2~3更快但易触发风控。"),
            ConfigParam("max_queue_size", "评论队列最大长度", kind="int", default=5000, minimum=10, maximum=10000,
                        tooltip="待爬评论链接的缓冲上限。满了则暂停采集新视频。"),
            ConfigParam("max_videos", "最多搜索结果数", kind="int", default=1000, minimum=1, maximum=5000),
            ConfigParam("max_candidates", "最多检查数", kind="int", default=3000, minimum=1, maximum=20000),
            ConfigParam("max_search_scrolls", "最大滚动次数", kind="int", default=360, minimum=30, maximum=2000, step=10),
        ]

    def run_task(self, values, log_callback, finish_callback, stop_event, pause_event):
        from src.platforms.tiktok.keyword_pro import run_tiktok_keyword_pro_spider

        config = {k: v for k, v in values.items() if k.startswith("tiktok_") or k in ("max_videos", "max_candidates", "scroll_interval", "max_search_scrolls", "no_new_scroll_limit", "comment_top_limit", "max_parallel_tabs", "max_comment_tabs", "max_metrics_tabs", "max_queue_size", "cooldown_min", "cooldown_max", "enable_timer", "timer_interval_minutes", "timer_max_runs")}
        return run_tiktok_keyword_pro_spider(
            _lines(values["keywords"]),
            int(values.get("max_videos", 1000)),
            int(values.get("max_candidates", 3000)),
            values["limit_time"],
            values["start_date"],
            values["end_date"],
            values["get_comments"],
            int(values["max_comments"]),
            DEFAULT_TIKTOK_CDP_URL,
            log_callback,
            finish_callback,
            stop_event,
            pause_event=pause_event,
            config=config,
        )
