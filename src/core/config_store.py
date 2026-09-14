"""JSON 持久化配置管理。"""
from __future__ import annotations

import json
import os
from pathlib import Path

from src.core.output import get_workspace_root
from src.ui.config_dialog import ConfigParam

# 配置文件存放目录名称
_CONFIG_DIR_NAME = "config"

# ---------------------------------------------------------------------------
# 全局配置（跨工具共享参数）
# ---------------------------------------------------------------------------

GLOBAL_TOOL_ID = "__global__"

GLOBAL_CONFIG_PARAMS: list[ConfigParam] = [
    ConfigParam("proxy_address", "代理地址(非Playwright)", kind="text", default="", tooltip="如: http://127.0.0.1:7890 (留空走系统默认)"),
    ConfigParam("ai_base_url", "AI 服务地址", kind="text", default="", tooltip="留空时回退到 OPENAI_BASE_URL / DEEPSEEK_BASE_URL / BASE_URL"),
    ConfigParam("ai_api_key", "AI API Key", kind="text", default="", tooltip="留空时回退到 OPENAI_API_KEY / DEEPSEEK_API_KEY / API_KEY"),
    ConfigParam("ai_model", "AI 模型", kind="text", default="", tooltip="留空时回退到 OPENAI_MODEL / DEEPSEEK_MODEL_NAME / MODEL_NAME"),
    ConfigParam("page_load_timeout", "页面加载超时(毫秒)", kind="int", default=45000, minimum=10000, maximum=120000, step=1000),
    ConfigParam("scroll_interval", "滚动间隔(秒)", kind="float", default=2.0, minimum=0.1, maximum=10.0, step=0.1, decimals=1),
    ConfigParam("no_new_scroll_limit", "无新内容停止阈值", kind="int", default=8, minimum=2, maximum=50),
    ConfigParam("max_scrolls", "最大滚动次数", kind="int", default=200, minimum=1, maximum=999999),
    ConfigParam("scroll_px", "每次滚动像素(px)", kind="int", default=2000, minimum=100, maximum=10000, step=100),
    ConfigParam("cooldown_min", "冷却等待最小(秒)", kind="float", default=5.0, minimum=0.0, maximum=60.0, step=0.5, decimals=1),
    ConfigParam("cooldown_max", "冷却等待最大(秒)", kind="float", default=10.0, minimum=0.0, maximum=120.0, step=0.5, decimals=1),
    ConfigParam("save_batch_size", "每批保存条数", kind="int", default=10, minimum=1, maximum=100),
    ConfigParam("comment_top_limit", "最多输出评论数", kind="int", default=100, minimum=1, maximum=500),
]

GLOBAL_CONFIG_DEFAULTS: dict[str, object] = {p.key: p.default for p in GLOBAL_CONFIG_PARAMS}


def apply_global_proxy() -> None:
    """根据全局配置应用系统级的 HTTP_PROXY 与 HTTPS_PROXY 环境变量。"""
    config = load_config(GLOBAL_TOOL_ID, GLOBAL_CONFIG_DEFAULTS, None)
    proxy_url = str(config.get("proxy_address", "")).strip()
    if proxy_url:
        os.environ["HTTP_PROXY"] = proxy_url
        os.environ["HTTPS_PROXY"] = proxy_url
        os.environ["http_proxy"] = proxy_url
        os.environ["https_proxy"] = proxy_url
    else:
        for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
            os.environ.pop(k, None)


# 参数名别名映射：全局标准名 → [工具中使用的别名列表]
# 仅包含语义和类型完全兼容的映射
GLOBAL_ALIAS_MAP: dict[str, list[str]] = {
    "page_load_timeout": ["search_page_timeout", "youtube_browser_page_timeout"],
    "scroll_interval": ["youtube_browser_scroll_delay"],
    "max_scrolls": ["max_search_scrolls", "max_post_scrolls", "youtube_browser_max_scrolls", "max_scroll_rounds", "max_profile_scrolls"],
    "no_new_scroll_limit": ["youtube_browser_no_new_limit", "comment_no_new_scroll_limit"],
    "scroll_px": ["youtube_browser_scroll_px"],
}

# ---------------------------------------------------------------------------
# 各工具默认配置
# ---------------------------------------------------------------------------

# 系统所有工具的默认配置项字典。这些值作为配置缺省或校验时的基准
DEFAULT_CONFIGS: dict[str, dict] = {
    "youtube_keyword_mining": {
        "max_results": 5000,                  # 搜索结果最大爬取数
        "youtube_search_batch_size": 50,       # 搜索 API 批量请求大小
        "youtube_video_batch_size": 50,        # 视频详情 API 批量请求大小
        "youtube_video_type_workers": 2,       # 长短视频类型 HEAD 探测并发数
        "youtube_language_filter": "fr, ru, de, es",  # 目标语种代码，默认法/俄/德/西
        "youtube_comment_mode": "快速模式",
        "youtube_comment_workers": 5,
    },
    "youtube_paired_context_metrics": {
        "context_size": 5,                     # 上下文选取大小（目标视频前后各取 N 个）
        "max_upload_pages": 200,               # 上传列表分页获取最大页数
        "check_video_type": "是",               # 是否使用统一 HEAD/重定向逻辑检测长短视频类型
        "youtube_video_type_workers": 2,
    },
    "youtube_channel_works": {
        "max_video_items": 5000,               # 频道作品提取上限
        "max_post_scrolls": 200,               # 帖子/社区内容滚动的最大次数（别名保留）
        "initial_load_delay": 1.8,
        "youtube_comment_scan_limit": 500,     # 评论扫描上限（深扫模式下生效，快速模式下受 top_limit 约束）
        "youtube_comment_mode": "快速模式",
        "youtube_comment_workers": 5,
        "youtube_video_type_workers": 2,
    },
    "youtube_top_comments": {
        "max_parallel_tabs": 3,                # 视频详情请求并发数
        "max_scan_comments": 500,              # 扫描评论最大上限
        "youtube_api_page_size": 100,          # API 每页请求数量
        "youtube_comment_mode": "快速模式",
        "youtube_comment_workers": 5,
        "youtube_video_type_workers": 2,
    },
    "tiktok_keyword_metrics": {
        "max_videos": 1000,
        "max_candidates": 3000,                # 候选视频扫描上限（去重过滤前的上限）
        "max_search_scrolls": 360,             # 别名保留
        "max_parallel_tabs": 1,                # 并发执行的标签页数
        "max_comment_tabs": 1,
        "max_queue_size": 5000,                # 缓冲队列最大容量
        "cooldown_min": 3.0,
        "cooldown_max": 8.0,
    },
    "tiktok_keyword_metrics_pro": {
        "max_videos": 1000,
        "max_candidates": 3000,
        "max_search_scrolls": 360,
        "max_parallel_tabs": 1,
        "max_comment_tabs": 1,
        "max_queue_size": 5000,
        "cooldown_min": 3.0,
        "cooldown_max": 8.0,
    },
    "tiktok_profile_directory": {
        "captcha_wait": 12,                    # 遇到验证码时手动滑块的等待时间（秒）
        "cooldown_every": 5,
    },
    "tiktok_profile_videos": {
        "link_batch_size": 50,
        "detail_load_timeout": 30000,
        "detail_delay_min": 2.0,
        "detail_delay_max": 5.0,
    },
    "tiktok_keyword_author_works": {
        "parallel_windows": 1,
        "max_seed_works": 300,
        "max_authors": 100,
        "max_profile_works_per_author": 50,
        "max_search_scrolls": 360,
        "max_profile_scrolls": 500,
        "profile_scroll_interval": 2.5,
    },
    "tiktok_hashtag_author_works": {
        "parallel_windows": 1,
        "max_seed_works": 300,
        "max_authors": 100,
        "max_profile_works_per_author": 50,
        "max_topic_scrolls": 360,
        "max_profile_scrolls": 500,
        "profile_scroll_interval": 2.5,
    },
    "tiktok_profile_play_counts": {
    },
    "tiktok_paired_context_metrics": {
        "context_size": 5,
        "api_page_size": 35,
        "max_api_pages": 10,
        "max_profile_scrolls": 80,             # 别名保留
    },
    "tiktok_top_comments": {
        "max_scroll_rounds": 80,               # 别名保留
        "comment_wait_timeout": 12000,
        "video_batch_cooldown_every": 3,
        "video_batch_cooldown_min": 4.0,
        "video_batch_cooldown_max": 9.0,
    },
    "steam_api_research": {
        "parallel_workers": 1,
        "max_apps_per_keyword": 100,
        "keyword_search_mode": "商店搜索接口（推荐）",
        "include_non_games": "否",
        "collect_current_players": "是",
        "collect_achievements": "否",
        "collect_review_player_profiles": "否",
        "max_review_players": 2000,
        "max_player_profiles": 2000,
        "player_parallel_workers": 2,
        "collect_player_owned_games": "是",
        "player_owned_games_limit": 0,
        "collect_player_recent_games": "是",
        "collect_player_target_achievements": "是",
        "collect_player_badges": "是",
        "collect_player_friends": "是",
        "review_language": "all",
        "reviews_filter": "all",
        "request_timeout": 30,
        "request_delay": 0.2,
        "cache_ttl_hours": 168,
        "save_batch_size": 5000,
    },
    "steam_player_profiles": {
        "max_player_profiles": 2000,
        "player_parallel_workers": 2,
        "collect_player_owned_games": "是",
        "player_owned_games_limit": 0,
        "collect_player_recent_games": "是",
        "collect_player_target_achievements": "是",
        "collect_player_badges": "是",
        "collect_player_friends": "是",
        "request_timeout": 30,
        "request_delay": 0.2,
        "save_batch_size": 5000,
    },
    "x_keyword_video_search": {
        "slice_days": 7,                       # 时间跨度切分天数，用于按区间精准爬取
        "search_page_timeout": 40000,          # 别名保留
        "no_new_scroll_limit": 5,              # 保留（还有 comment_no_new_scroll_limit 需独立配置）
        "comment_no_new_scroll_limit": 5,
        "max_parallel_tabs": 1,
        "max_comment_tabs": 1,
        "max_queue_size": 5000,
        "search_refresh_count": 3,
        "search_refresh_interval": 5.0,
        "comment_refresh_count": 3,
        "comment_refresh_interval": 5.0,
    },
    "x_tweet_author_profiles": {
        "tweet_ready_timeout": 12000,
        "cooldown_every": 5,
        "profile_entry_mode": "直接打开",
    },
    "x_keyword_author_works": {
        "parallel_windows": 1,
        "profile_entry_mode": "直接打开",
    },
    "x_paired_context_metrics": {
        "context_size": 5,
        "max_profile_scrolls": 45,             # 别名保留
    },
    "x_tweet_metrics": {
        "page_ready_wait": 2.5,
        "cooldown_every": 3,
    },
    "x_profile_tweets": {
        "parallel_windows": 1,
        "max_tweets_per_author": 50,
        "max_scrolls": 80,
        "initial_load_delay": 2.0,
        "profile_entry_mode": "直接打开",
    },
    "x_profile_bundle": {
        "parallel_windows": 1,
        "max_tweets_per_author": 50,
        "max_scrolls": 80,
        "initial_load_delay": 2.0,
        "profile_entry_mode": "直接打开",
    },
    "x_top_comments": {
    },
    "instagram_profile_works": {
        "max_works": 5000,
        "detail_delay_min": 3.0,
        "detail_delay_max": 7.0,
        "initial_load_delay": 3.5,
        "before_detail_delay_min": 6.0,
        "before_detail_delay_max": 12.0,
    },
    "facebook_profile_works": {
        "max_posts": 100,
        "max_scrolls": 200,                    # 保留（使用标准名）
        "page_load_timeout": 60000,            # 保留（Facebook 需要更长默认值）
        "scroll_delay": 2000,                  # 保留（int 毫秒，与全局 scroll_interval 不兼容）
        "collect_comments": "否",
        "cooldown_min": 1.0,
        "cooldown_max": 3.0,
    },
    "facebook_keyword_search": {
        "max_posts": 100,
        "max_scrolls": 200,                    # 保留（使用标准名）
        "page_load_timeout": 60000,            # 保留（Facebook 需要更长默认值）
        "scroll_delay": 2000,                  # 保留（int 毫秒，与全局 scroll_interval 不兼容）
        "cooldown_min": 1.0,
        "cooldown_max": 3.0,
    },
    "judge_aigc": {
        "temperature": 0.1,
        "sleep_seconds": 0.5,
        "trust_local_negative_aigc": False,   # 本地正则判定为否定时，是否信任本地结果（跳过大模型）
    },
    "keyword_candidate_validator": {
        "temperature": 0.1,
        "sleep_seconds": 0.5,
    },
}


def get_config_dir() -> Path:
    """
    获取配置存储根目录路径。

    Returns:
        Path: 配置文件夹的 Path 对象
    """
    return get_workspace_root() / _CONFIG_DIR_NAME


def get_config_path(tool_id: str) -> Path:
    """
    获取指定工具默认配置文件的绝对路径。

    Args:
        tool_id: 工具标识名

    Returns:
        Path: 对应的 JSON 配置文件路径
    """
    return get_config_dir() / f"{tool_id}.json"


def get_config_path_for_profile(tool_id: str, profile: str | None) -> Path:
    """
    获取指定方案的配置文件路径。profile 为 None 时使用默认文件。

    Args:
        tool_id: 工具标识名
        profile: 自定义配置方案名称

    Returns:
        Path: 对应的 JSON 配置文件路径
    """
    if not profile:
        return get_config_path(tool_id)
    # 使用 translate 去除文件名中的非法字符，以防 profile 包含非法文件名路径产生注入或报错
    safe_name = profile.strip().translate(str.maketrans({c: "_" for c in r'\/:*?"<>|'}))
    return get_config_dir() / f"{tool_id}_{safe_name}.json"


def list_profiles(tool_id: str) -> list[tuple[str, str | None]]:
    """
    列出某个工具的所有配置方案。

    Args:
        tool_id: 工具标识名

    Returns:
        list[tuple[str, str | None]]: 返回列表，格式为 [(方案显示名称, profile_key), ...]，profile_key 为 None 表示默认配置。
    """
    config_dir = get_config_dir()
    if not config_dir.exists():
        return [("默认配置", None)]
    profiles: list[tuple[str, str | None]] = [("默认配置", None)]
    prefix = f"{tool_id}_"
    for f in sorted(config_dir.glob(f"{tool_id}_*.json")):
        stem = f.stem
        if stem.startswith(prefix):
            name = stem[len(prefix):]
            if name:
                profiles.append((name, name))
    return profiles


def _coerce_value(value, default):
    """
    将加载的 JSON 值强制转换为与默认配置值相同的 Python 原生类型。
    主要是为了防止手动修改 JSON 配置导致字段类型混乱（比如布尔值变成字符串）。
    """
    if isinstance(default, bool):
        if isinstance(value, str):
            return value.lower() in ("true", "1", "yes")
        return bool(value)
    # Python 中 bool 是 int 的子类，所以 isinstance(True, int) 为 True。
    # 这里必须加 isinstance(default, bool) 排除条件，否则布尔值会被误强转为 0 或 1。
    if isinstance(default, int) and not isinstance(default, bool):
        try:
            return int(value)
        except (TypeError, ValueError):
            return default
    if isinstance(default, float):
        try:
            return float(value)
        except (TypeError, ValueError):
            return default
    return value


def load_config(tool_id: str, defaults: dict, profile: str | None = None) -> dict:
    """
    从本地 JSON 文件加载配置，缺失字段用 defaults 自动补齐。

    Args:
        tool_id: 工具标识名
        defaults: 默认参数键值对字典
        profile: 配置方案名称，为 None 时加载默认配置

    Returns:
        dict: 最终补齐并强转完类型后的配置字典
    """
    result = dict(defaults)
    path = get_config_path_for_profile(tool_id, profile)
    if path.exists():
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                for key in defaults:
                    if key in data:
                        result[key] = _coerce_value(data[key], defaults[key])
        except (json.JSONDecodeError, OSError):
            pass
    return result


def save_config(tool_id: str, values: dict, defaults: dict | None = None, profile: str | None = None) -> None:
    """
    保存配置到本地 JSON 文件，只保留 defaults 中定义过的有效参数 Key。

    Args:
        tool_id: 工具标识名
        values: 准备保存的键值对字典
        defaults: 可选，作为字段校验的默认字典，默认自动读取 DEFAULT_CONFIGS
        profile: 方案名称，为 None 时保存到默认配置
    """
    if defaults is None:
        defaults = DEFAULT_CONFIGS.get(tool_id, {})
    if not defaults:
        return
    filtered = {k: v for k, v in values.items() if k in defaults}
    config_dir = get_config_dir()
    config_dir.mkdir(parents=True, exist_ok=True)
    path = get_config_path_for_profile(tool_id, profile)
    # 采用原子写入模式：先写入临时文件，再做文件替换。
    # 这样可以防止在写入过程中发生断电或程序强退导致原 JSON 配置文件内容被清空或损坏。
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(filtered, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, path)


def generate_all_defaults() -> None:
    """
    启动应用时，为 DEFAULT_CONFIGS 中所有的工具生成默认的 JSON 配置文件（已存在的文件不会被覆盖）。
    """
    for tool_id, defaults in DEFAULT_CONFIGS.items():
        path = get_config_path(tool_id)
        if not path.exists():
            save_config(tool_id, defaults)


def delete_profile(tool_id: str, profile: str) -> bool:
    """
    删除指定的自定义配置方案文件（默认配置无法删除）。

    Args:
        tool_id: 工具标识名
        profile: 要删除的方案名称，不能为空

    Returns:
        bool: 是否删除成功
    """
    if not profile:
        return False
    path = get_config_path_for_profile(tool_id, profile)
    if path.exists():
        path.unlink()
        return True
    return False

