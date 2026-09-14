from __future__ import annotations

from pathlib import Path

from PyQt5.QtWidgets import QMessageBox

from src.ui.base import FieldSpec, SimpleToolWindow
from src.ui.config_dialog import ConfigParam


DEFAULT_START_DATE = "2025-05-06"
DEFAULT_END_DATE = "2026-05-06"


def _lines(value: str) -> list[str]:
    return [line.strip() for line in value.splitlines() if line.strip()]


def _choose_spool_action(parent, spool_path: str) -> str | None:
    if not Path(spool_path).exists():
        return "continue"
    dialog = QMessageBox(parent)
    dialog.setWindowTitle("发现未完成任务")
    dialog.setIcon(QMessageBox.Question)
    dialog.setText("检测到相同输入的未完成采集数据。")
    dialog.setInformativeText("可以继续采集、立即导出当前数据，或删除旧进度后重新开始。")
    continue_button = dialog.addButton("继续采集", QMessageBox.AcceptRole)
    export_button = dialog.addButton("导出当前数据", QMessageBox.ActionRole)
    restart_button = dialog.addButton("重新开始", QMessageBox.DestructiveRole)
    dialog.addButton(QMessageBox.Cancel)
    dialog.setDefaultButton(continue_button)
    dialog.exec_()
    clicked = dialog.clickedButton()
    if clicked is continue_button:
        return "continue"
    if clicked is export_button:
        return "export"
    if clicked is restart_button:
        confirm = QMessageBox.question(
            parent,
            "确认重新开始",
            "这会删除当前未完成进度，是否继续？",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        return "restart" if confirm == QMessageBox.Yes else None
    return None


class YouTubeKeywordWindow(SimpleToolWindow):
    tool_id = "youtube_keyword_mining"
    task_feedback_enabled = True

    def __init__(self) -> None:
        super().__init__(
            "YouTube 关键词搜索",
            [
                FieldSpec("api_key", "Google API Key(s) (支持多行或导入txt)", kind="text_or_file", required=True, placeholder="支持每行填写一个API Key，耗尽自动轮询"),
                FieldSpec("limit_time", "是否限制时间？", kind="combo", options=("是", "否"), default="是"),
                FieldSpec("start_date", "开始日期 YYYY-MM-DD", default=DEFAULT_START_DATE),
                FieldSpec("end_date", "结束日期 YYYY-MM-DD", default=DEFAULT_END_DATE),
                FieldSpec("keywords", "关键词，每行一个", kind="text_or_file", required=True, placeholder="每行一个关键词"),
                FieldSpec("get_comments", "是否获取视频评论信息？", kind="combo", options=("是", "否"), default="否"),
                FieldSpec("max_comments", "最多获取评论数", kind="int", default=100, minimum=10, maximum=10000),
                FieldSpec("check_video_type", "是否检测长短视频类型？", kind="combo", options=("是", "否"), default="是"),
                FieldSpec("auto_snapshot_3d", "自动生成3日快照？", kind="combo", options=("是", "否"), default="否"),
                FieldSpec("auto_snapshot_7d", "自动生成7日快照？", kind="combo", options=("是", "否"), default="否"),
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
        from src.platforms.youtube.keyword import parse_date_range

        if not _lines(values["keywords"]):
            raise ValueError("至少需要输入一个关键词。")
        if values.get("enable_timer") == "是" and values.get("limit_time") != "是":
            raise ValueError("定时模式必须开启时间过滤，否则每轮采集结果完全相同。")
        if values.get("limit_time") == "是":
            parse_date_range(values["start_date"], values["end_date"])

    def tool_config_params(self):
        return [
            ConfigParam("max_parallel_tabs", "并发数", kind="int", default=3, minimum=1, maximum=5, tooltip="同时并发搜索关键词的数量"),
            ConfigParam("max_results", "最多搜索结果数", kind="int", default=5000, minimum=1, maximum=5000),
            ConfigParam(
                "youtube_search_method",
                "搜索方式",
                kind="combo",
                options=("浏览器优先（省配额）", "仅API（消耗配额）"),
                default="浏览器优先（省配额）",
                tooltip="【重要】‘浏览器优先’模式利用浏览器在后台模拟搜索获取视频链接，可节省 99% 的 YouTube API 每日配额消耗！"
            ),
            ConfigParam("youtube_search_batch_size", "搜索每页条数", kind="int", default=50, minimum=1, maximum=50),
            ConfigParam("youtube_date_chunk_days", "日期切分粒度(天)", kind="int", default=7, minimum=1, maximum=30,
                        tooltip="YouTube API 单次搜索最多返回约 500 条。开启时间过滤时，会将日期范围按此天数切分为多个小区间分别检索，绕过 500 条上限。越小越精确但消耗更多配额。"),
            ConfigParam("youtube_date_chunk_hours", "日期切分粒度(小时)，留空则使用上方天数", kind="int", default=0, minimum=0, maximum=720,
                        tooltip="若填写（如 1），则按小时切分时间区间，优先级高于上方的天数粒度。适合短时间范围内大量视频的精确采集。0 或留空表示使用天数粒度。"),
            ConfigParam("youtube_video_batch_size", "视频详情每批条数", kind="int", default=50, minimum=1, maximum=50),
            ConfigParam("youtube_video_type_workers", "视频类型检测并发数", kind="int", default=2, minimum=1, maximum=10,
                        tooltip="控制长视频/Shorts HEAD 探测的并发数。关键词并发较高时建议保持 2-3，避免网络请求瞬间堆高导致卡顿。"),
            ConfigParam("youtube_language_filter", "目标语种代码", kind="text", default="fr, ru, de, es",
                        tooltip="可填写一个或多个语言代码，逗号、分号、空格或换行分隔，例如 zh-CN, zh-TW, en。为空表示不过滤。"),
            ConfigParam("youtube_comment_mode", "评论采集模式", kind="combo", options=("快速模式", "深扫模式"), default="快速模式"),
            ConfigParam("youtube_comment_workers", "评论并发数", kind="int", default=5, minimum=1, maximum=10),
            ConfigParam("youtube_browser_scroll_px", "浏览器每次滚动像素", kind="int", default=2500, minimum=500, maximum=10000, step=100),
            ConfigParam("youtube_browser_scroll_delay", "浏览器滚动间隔(秒)", kind="float", default=1.0, minimum=0.2, maximum=5.0, step=0.1, decimals=1),
            ConfigParam("youtube_browser_max_scrolls", "浏览器最大滚动次数", kind="int", default=100, minimum=10, maximum=500),
            ConfigParam("youtube_browser_page_timeout", "浏览器页面加载超时(毫秒)", kind="int", default=45000, minimum=10000, maximum=120000, step=1000),
            ConfigParam("youtube_browser_no_new_limit", "浏览器无新内容停止阈值", kind="int", default=8, minimum=2, maximum=50),
            ConfigParam("youtube_initial_load_delay", "初始页面加载等待(秒)", kind="float", default=2.0, minimum=0.5, maximum=10.0, step=0.1, decimals=1,
                        tooltip="浏览器模式下打开YouTube搜索页后的初始等待缓冲秒数，较慢的网络可适当调大。"),
            ConfigParam("comment_top_limit", "单个视频导出热度前N条评论", kind="int", default=100, minimum=1, maximum=1000),
        ]

    def run_task(self, values, log_callback, finish_callback, stop_event, pause_event):
        from src.platforms.youtube.keyword import run_youtube_keyword

        config = {k: v for k, v in values.items() if k.startswith("youtube_") or k in ("max_results", "comment_top_limit", "enable_timer", "timer_interval_minutes", "timer_max_runs", "max_parallel_tabs", "_task_feedback")}
        return run_youtube_keyword(
            _lines(values["api_key"]),
            _lines(values["keywords"]),
            int(values.get("max_results", 5000)),
            values["limit_time"],
            values["start_date"],
            values["end_date"],
            values["get_comments"],
            int(values.get("max_comments", 100)),
            values.get("check_video_type", "是"),
            values.get("auto_snapshot_3d", "否"),
            values.get("auto_snapshot_7d", "否"),
            log_callback,
            finish_callback,
            stop_event,
            config=config,
            pause_event=pause_event,
        )


class YouTubeProfilesWindow(SimpleToolWindow):
    task_feedback_enabled = True

    def __init__(self) -> None:
        super().__init__(
            "YouTube 博主信息",
            [
                FieldSpec("api_key", "Google API Key(s) (支持多行或导入txt)", kind="text_or_file", required=True, placeholder="支持每行填写一个API Key，耗尽自动轮询"),
                FieldSpec("txt_path", "博主主页链接，每行一个", kind="text_or_file", required=True, placeholder="https://www.youtube.com/@username"),
            ],
        )

    def tool_config_params(self):
        return [
            ConfigParam("max_parallel_tabs", "并发数", kind="int", default=3, minimum=1, maximum=5, tooltip="同时处理的博主主页数量"),
        ]

    def run_task(self, values, log_callback, finish_callback, stop_event, pause_event):
        from src.platforms.youtube.profiles import run_channel_spider

        config = {
            "max_parallel_tabs": values.get("max_parallel_tabs", 3),
            "_task_feedback": values.get("_task_feedback"),
        }
        return run_channel_spider(_lines(values["api_key"]), self._text_to_tempfile(values["txt_path"]), log_callback, finish_callback, stop_event, pause_event=pause_event, config=config)


class YouTubeContextWindow(SimpleToolWindow):
    tool_id = "youtube_paired_context_metrics"
    task_feedback_enabled = True

    def __init__(self) -> None:
        super().__init__(
            "YouTube 视频上下文数据",
            [
                FieldSpec("api_key", "Google API Key(s) (支持多行或导入txt)", kind="text_or_file", required=True, placeholder="支持每行填写一个API Key，耗尽自动轮询"),
                FieldSpec("txt_path", "视频链接 + 博主主页，每行一对", kind="text_or_file", required=True, placeholder="视频链接 博主主页链接"),
            ],
        )

    def tool_config_params(self):
        return [
            ConfigParam("max_parallel_tabs", "并发数", kind="int", default=3, minimum=1, maximum=5, tooltip="同时处理的视频对数量"),
            ConfigParam("context_size", "目标视频前后各取几条", kind="int", default=5, minimum=1, maximum=20),
            ConfigParam("max_upload_pages", "最多翻页数", kind="int", default=200, minimum=10, maximum=1000),
            ConfigParam("check_video_type", "是否检测长短视频类型？", kind="combo", options=("是", "否"), default="是"),
            ConfigParam("youtube_video_type_workers", "视频类型检测并发数", kind="int", default=2, minimum=1, maximum=10,
                        tooltip="控制长视频/Shorts HEAD 探测的并发数。"),
        ]

    def run_task(self, values, log_callback, finish_callback, stop_event, pause_event):
        from src.platforms.youtube.context import run_youtube_paired_context_spider

        config = {k: v for k, v in values.items() if k in ("context_size", "max_upload_pages", "check_video_type", "max_parallel_tabs", "youtube_video_type_workers", "_task_feedback")}
        return run_youtube_paired_context_spider(_lines(values["api_key"]), self._text_to_tempfile(values["txt_path"]), log_callback, finish_callback, stop_event, config=config, pause_event=pause_event)


class YouTubeChannelWorksWindow(SimpleToolWindow):
    tool_id = "youtube_channel_works"
    task_feedback_enabled = True

    def __init__(self) -> None:
        super().__init__(
            "YouTube 博主作品采集",
            [
                FieldSpec("api_key", "Google API Key(s) (支持多行或导入txt)", kind="text_or_file", required=True, placeholder="支持每行填写一个API Key，耗尽自动轮询"),
                FieldSpec(
                    "channel_urls",
                    "博主主页链接，每行一个",
                    kind="text_or_file",
                    placeholder="https://www.youtube.com/@username",
                    required=True,
                ),
                FieldSpec("collect_target", "采集目标", kind="combo", options=("全部", "仅视频与Shorts", "仅帖子 (Posts)"), default="全部"),
                FieldSpec("fetch_shorts_related", "抓取 Shorts 关联长视频", kind="combo", options=("否", "是"), default="否"),
                FieldSpec("live_stream_policy", "直播处理策略", kind="combo", options=("不处理", "保留并标记", "直接排除"), default="不处理"),
                FieldSpec("limit_time", "是否限制时间？", kind="combo", options=("是", "否"), default="否"),
                FieldSpec("start_date", "开始日期 YYYY-MM-DD", default=DEFAULT_START_DATE),
                FieldSpec("end_date", "结束日期 YYYY-MM-DD", default=DEFAULT_END_DATE),
                FieldSpec("get_comments", "是否获取视频评论信息？", kind="combo", options=("是", "否"), default="否"),
                FieldSpec("max_comments", "最多获取评论数", kind="int", default=100, minimum=10, maximum=10000),
                FieldSpec("verify_video_type", "是否精确验证视频长短类型？", kind="combo", options=("是", "否"), default="是"),
            ],
            height=820,
        )
        self.bind_field_visibility("limit_time", "是", ["start_date", "end_date"])
        self.bind_field_visibility("get_comments", "是", ["max_comments"])

    def validate_values(self, values):
        if not _lines(values["channel_urls"]):
            raise ValueError("至少需要输入一个 YouTube 博主主页链接。")
        if values.get("limit_time") == "是":
            from src.platforms.youtube.keyword import parse_date_range
            parse_date_range(values["start_date"], values["end_date"])

    def prepare_run_values(self, values):
        from src.platforms.youtube.channel_works import (
            build_channel_works_scope,
            channel_works_spool_path,
            parse_channel_urls,
        )

        settings = dict(self.config_values)
        settings.update(values)
        scope = build_channel_works_scope(
            parse_channel_urls(values["channel_urls"]),
            values.get("collect_target", "全部"),
            int(settings.get("max_video_items", 5000)),
            int(settings.get("max_post_scrolls", 200)),
            values.get("fetch_shorts_related", "否"),
            values.get("live_stream_policy", "不处理"),
            values.get("limit_time", "否"),
            values.get("start_date", ""),
            values.get("end_date", ""),
            values.get("get_comments", "否"),
            int(values.get("max_comments", 100)),
            settings,
        )
        action = _choose_spool_action(self, channel_works_spool_path(scope))
        if action is None:
            return None
        prepared = dict(values)
        prepared["_resume_action"] = action
        return prepared

    def tool_config_params(self):
        return [
            ConfigParam("max_parallel_tabs", "并发数", kind="int", default=3, minimum=1, maximum=5, tooltip="同时处理的博主数量"),
            ConfigParam("max_video_items", "最多作品数", kind="int", default=5000, minimum=1, maximum=5000),
            ConfigParam("max_post_scrolls", "帖子最大滚动次数", kind="int", default=200, minimum=1, maximum=5000),
            ConfigParam("initial_load_delay", "初始加载等待(秒)", kind="float", default=1.8, minimum=0.5, maximum=10.0, step=0.1, decimals=1),
            ConfigParam("youtube_comment_scan_limit", "评论扫描上限", kind="int", default=500, minimum=10, maximum=10000,
                        tooltip="深扫模式下每视频最多扫描的评论数。快速模式下受\"最多获取评论数\"约束。"),
            ConfigParam("youtube_comment_mode", "评论采集模式", kind="combo", options=("快速模式", "深扫模式"), default="快速模式"),
            ConfigParam("youtube_comment_workers", "评论并发数", kind="int", default=5, minimum=1, maximum=10),
            ConfigParam("youtube_video_type_workers", "视频类型检测并发数", kind="int", default=2, minimum=1, maximum=10,
                        tooltip="控制长视频/Shorts HEAD 探测的并发数。"),
            ConfigParam("youtube_shorts_related_delay", "Shorts关联长视频提取延迟(秒)", kind="float", default=1.0, minimum=0.0, maximum=10.0, step=0.5, decimals=1,
                        tooltip="抓取 Shorts 关联的长视频时的限速延迟秒数。"),
        ]

    def run_task(self, values, log_callback, finish_callback, stop_event, pause_event):
        from src.platforms.youtube.channel_works import run_youtube_channel_works_spider

        config = {k: v for k, v in values.items() if k.startswith("youtube_") or k in ("max_video_items", "page_load_timeout", "scroll_interval", "no_new_scroll_limit", "scroll_px", "max_post_scrolls", "initial_load_delay", "verify_video_type", "max_parallel_tabs", "_resume_action", "_task_feedback")}
        return run_youtube_channel_works_spider(
            _lines(values["api_key"]),
            values["channel_urls"],
            values.get("collect_target", "全部"),
            int(values.get("max_video_items", 5000)),
            int(values.get("max_post_scrolls", 200)),
            values.get("fetch_shorts_related", "否"),
            values.get("live_stream_policy", "不处理"),
            values["limit_time"],
            values["start_date"],
            values["end_date"],
            values["get_comments"],
            int(values["max_comments"]),
            log_callback,
            finish_callback,
            stop_event,
            config=config,
            pause_event=pause_event,
        )


class YouTubeCommentsWindow(SimpleToolWindow):
    tool_id = "youtube_top_comments"
    task_feedback_enabled = True

    def __init__(self) -> None:
        super().__init__(
            "YouTube 视频数据与评论采集",
            [
                FieldSpec("api_key", "Google API Key(s) (支持多行或导入txt)", kind="text_or_file", required=True, placeholder="支持每行填写一个API Key，耗尽自动轮询"),
                FieldSpec("txt_path", "视频链接，每行一个", kind="text_or_file", required=True, placeholder="https://www.youtube.com/watch?v=xxxx"),
                FieldSpec("fetch_shorts_related", "抓取 Shorts 关联长视频", kind="combo", options=("否", "是"), default="否"),
                FieldSpec("live_stream_policy", "直播处理策略", kind="combo", options=("不处理", "保留并标记", "直接排除"), default="不处理"),
                FieldSpec("get_comments", "是否获取视频评论信息？", kind="combo", options=("是", "否"), default="否"),
                FieldSpec("max_scan_comments", "最多获取评论数", kind="int", default=500, minimum=50, maximum=10000),
                FieldSpec("check_type", "是否精确检测视频长短类型？", kind="combo", options=("是", "否"), default="否"),
            ],
        )
        self.bind_field_visibility("get_comments", "是", ["max_scan_comments"])

    def tool_config_params(self):
        return [
            ConfigParam("max_parallel_tabs", "并发数", kind="int", default=3, minimum=1, maximum=5, tooltip="同时处理视频详情请求的并发数"),
            ConfigParam("youtube_api_page_size", "评论每页条数", kind="int", default=100, minimum=1, maximum=100),
            ConfigParam("youtube_comment_mode", "评论采集模式", kind="combo", options=("快速模式", "深扫模式"), default="快速模式"),
            ConfigParam("youtube_comment_workers", "评论并发数", kind="int", default=5, minimum=1, maximum=10),
            ConfigParam("comment_top_limit", "单个视频导出热度前N条评论", kind="int", default=100, minimum=1, maximum=1000),
            ConfigParam("youtube_video_type_workers", "视频类型检测并发数", kind="int", default=2, minimum=1, maximum=10,
                        tooltip="控制长视频/Shorts HEAD 探测的并发数。"),
            ConfigParam("youtube_shorts_related_delay", "Shorts关联长视频提取延迟(秒)", kind="float", default=1.0, minimum=0.0, maximum=10.0, step=0.5, decimals=1,
                        tooltip="抓取 Shorts 关联的长视频时的限速延迟秒数。"),
        ]

    def prepare_run_values(self, values):
        from src.platforms.youtube.comments import (
            build_video_metrics_scope,
            extract_video_id,
            video_metrics_spool_path,
        )

        video_ids = []
        seen = set()
        for line in _lines(values["txt_path"]):
            video_id = extract_video_id(line)
            if video_id and video_id not in seen:
                seen.add(video_id)
                video_ids.append(video_id)
        settings = dict(self.config_values)
        settings.update(values)
        scope = build_video_metrics_scope(
            video_ids,
            values.get("fetch_shorts_related", "否"),
            values.get("live_stream_policy", "不处理"),
            values.get("get_comments", "否"),
            values.get("check_type", "否"),
            int(values.get("max_scan_comments", 500)),
            settings,
        )
        action = _choose_spool_action(self, video_metrics_spool_path(scope))
        if action is None:
            return None
        prepared = dict(values)
        prepared["_resume_action"] = action
        return prepared

    def run_task(self, values, log_callback, finish_callback, stop_event, pause_event):
        from src.platforms.youtube.comments import run_youtube_video_metrics_spider

        config = {k: v for k, v in values.items() if k.startswith("youtube_") or k in ("comment_top_limit", "_resume_action", "_task_feedback")}
        return run_youtube_video_metrics_spider(
            _lines(values["api_key"]),
            self._text_to_tempfile(values["txt_path"]),
            values.get("fetch_shorts_related", "否"),
            values.get("live_stream_policy", "不处理"),
            values.get("get_comments", "否"),
            values.get("check_type", "否"),
            int(values.get("max_scan_comments", 500)),
            log_callback,
            finish_callback,
            stop_event,
            config=config,
            pause_event=pause_event,
        )
