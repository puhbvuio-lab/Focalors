from __future__ import annotations

from src.core.xlsx import DEFAULT_XLSX_AUTOSAVE_ROWS
from src.ui.base import FieldSpec, SimpleToolWindow
from src.ui.config_dialog import ConfigParam


def _lines(value: str) -> list[str]:
    return [line.strip() for line in str(value or "").splitlines() if line.strip()]


def _player_profile_config_params() -> list[ConfigParam]:
    return [
        ConfigParam("max_player_profiles", "最多增强玩家数", kind="int", default=2000, minimum=1, maximum=100000),
        ConfigParam("player_parallel_workers", "玩家画像 API 并发数", kind="int", default=2, minimum=1, maximum=8),
        ConfigParam("collect_player_owned_games", "采集玩家公开游戏库", kind="combo", options=("是", "否"), default="是"),
        ConfigParam("player_owned_games_limit", "每玩家最多游戏库条数(0=全部)", kind="int", default=0, minimum=0, maximum=100000),
        ConfigParam("collect_player_recent_games", "采集玩家最近游玩", kind="combo", options=("是", "否"), default="是"),
        ConfigParam("collect_player_target_achievements", "采集目标游戏玩家成就", kind="combo", options=("是", "否"), default="是"),
        ConfigParam("collect_player_badges", "采集玩家徽章", kind="combo", options=("是", "否"), default="是"),
        ConfigParam("collect_player_friends", "采集玩家公开好友", kind="combo", options=("是", "否"), default="是"),
    ]


class SteamApiResearchWindow(SimpleToolWindow):
    tool_id = "steam_api_research"

    def __init__(self) -> None:
        super().__init__(
            "Steam API 游戏数据采集",
            [
                FieldSpec("api_key", "Steam Web API Key（可选）", placeholder="用于 IStoreService AppList；留空也可采集商店公开数据"),
                FieldSpec("app_ids", "AppID / 商店链接，每行一个", kind="text_or_file", placeholder="https://store.steampowered.com/app/1623730/Palworld/\n1623730"),
                FieldSpec("keywords", "关键词，每行一个", kind="text_or_file", placeholder="monster taming games\npokemon like"),
                FieldSpec(
                    "language",
                    "商店语言",
                    kind="combo",
                    options=("english", "schinese", "japanese", "tchinese", "koreana", "french", "german", "spanish", "russian"),
                    default="english",
                ),
                FieldSpec("country", "商店地区代码", default="US", placeholder="US / JP / CN"),
                FieldSpec("collect_reviews", "是否采集玩家评论/评价摘要？", kind="combo", options=("否", "是"), default="否"),
                FieldSpec("max_reviews", "每个游戏最多玩家评论数（0=仅摘要）", kind="int", default=0, minimum=0, maximum=5000000),
                FieldSpec("collect_news", "是否采集新闻？", kind="combo", options=("否", "是"), default="否"),
                FieldSpec("max_news", "每个游戏最多新闻数", kind="int", default=5, minimum=1, maximum=100),
            ],
            height=780,
        )
        self.bind_field_visibility("collect_reviews", "是", ["max_reviews"])
        self.bind_field_visibility("collect_news", "是", ["max_news"])

    def validate_values(self, values):
        from src.platforms.steam.api import normalize_keywords, parse_app_ids

        if not parse_app_ids(values.get("app_ids", "")) and not normalize_keywords(values.get("keywords", "")):
            raise ValueError("至少需要输入一个 Steam AppID/商店链接，或一个关键词。")
        country = str(values.get("country", "")).strip()
        if country and not (len(country) == 2 and country.isalpha()):
            raise ValueError("商店地区代码请填写 2 位国家/地区代码，例如 US、JP、CN。")

    def tool_config_params(self):
        return [
            ConfigParam("parallel_workers", "API 并发数", kind="int", default=1, minimum=1, maximum=8),
            ConfigParam("max_apps_per_keyword", "每个关键词最多游戏数", kind="int", default=100, minimum=1, maximum=5000),
            ConfigParam(
                "keyword_search_mode",
                "关键词发现方式",
                kind="combo",
                options=("商店搜索接口（推荐）", "商店搜索后补 AppList", "AppList 本地匹配"),
                default="商店搜索接口（推荐）",
                tooltip="商店搜索更快；AppList 本地匹配会缓存完整应用列表，适合补漏但首次较慢。",
            ),
            ConfigParam("include_non_games", "是否保留非游戏 App", kind="combo", options=("否", "是"), default="否"),
            ConfigParam("collect_current_players", "采集当前在线人数", kind="combo", options=("是", "否"), default="是"),
            ConfigParam("collect_achievements", "采集成就数量", kind="combo", options=("否", "是"), default="否"),
            ConfigParam("collect_review_player_profiles", "评论玩家画像增强", kind="combo", options=("否", "是"), default="否",
                        tooltip="开启后会基于玩家评论里的 SteamID 继续采集公开资料、公开游戏库、最近游玩、目标游戏成就和徽章；需要 Steam Web API Key。"),
            ConfigParam("max_review_players", "每个游戏最多增强评论玩家数", kind="int", default=2000, minimum=1, maximum=100000),
            *_player_profile_config_params(),
            ConfigParam("review_language", "评论语言", kind="text", default="all", tooltip="Steam appreviews language 参数；all=全部，english/schinese/japanese 等。"),
            ConfigParam("reviews_filter", "评论排序/过滤", kind="combo", options=("all", "recent", "updated"), default="all"),
            ConfigParam("request_timeout", "API 请求超时(秒)", kind="int", default=30, minimum=5, maximum=180),
            ConfigParam("request_delay", "单请求间隔(秒)", kind="float", default=0.2, minimum=0.0, maximum=10.0, step=0.1, decimals=1),
            ConfigParam("cache_ttl_hours", "关键词/AppList 缓存有效期(小时)", kind="int", default=168, minimum=0, maximum=8760),
            ConfigParam(
                "save_batch_size",
                "每批保存条数",
                kind="int",
                default=DEFAULT_XLSX_AUTOSAVE_ROWS,
                minimum=DEFAULT_XLSX_AUTOSAVE_ROWS,
                maximum=100000,
                step=1000,
            ),
        ]

    def run_task(self, values, log_callback, finish_callback, stop_event, pause_event):
        from src.platforms.steam.api import run_steam_api_spider

        config_keys = {
            "parallel_workers",
            "max_apps_per_keyword",
            "keyword_search_mode",
            "include_non_games",
            "collect_current_players",
            "collect_achievements",
            "collect_review_player_profiles",
            "max_review_players",
            "max_player_profiles",
            "player_parallel_workers",
            "collect_player_owned_games",
            "player_owned_games_limit",
            "collect_player_recent_games",
            "collect_player_target_achievements",
            "collect_player_badges",
            "collect_player_friends",
            "review_language",
            "reviews_filter",
            "request_timeout",
            "request_delay",
            "cache_ttl_hours",
            "save_batch_size",
        }
        config = {k: v for k, v in values.items() if k in config_keys}
        return run_steam_api_spider(
            str(values.get("api_key", "")).strip(),
            values.get("app_ids", ""),
            values.get("keywords", ""),
            values.get("language", "english"),
            values.get("country", "US"),
            values.get("collect_reviews", "否"),
            int(values.get("max_reviews", 0) or 0),
            values.get("collect_news", "否"),
            int(values.get("max_news", 5) or 5),
            log_callback,
            finish_callback,
            stop_event,
            pause_event=pause_event,
            config=config,
        )


class SteamPlayerProfilesWindow(SimpleToolWindow):
    tool_id = "steam_player_profiles"

    def __init__(self) -> None:
        super().__init__(
            "Steam 评论玩家画像补采",
            [
                FieldSpec("api_key", "Steam Web API Key", required=True, placeholder="必填，用于读取玩家公开资料/游戏库/成就"),
                FieldSpec("xlsx_path", "已有玩家评论 Excel（可选）", kind="file", placeholder="选择 Steam API 游戏数据采集产出的 xlsx"),
                FieldSpec("steam_ids", "SteamID / 主页链接，每行一个（可选）", kind="text_or_file", placeholder="7656119...\nhttps://steamcommunity.com/profiles/7656119..."),
                FieldSpec("target_appid", "目标游戏 AppID / 商店链接（手填 SteamID 时可选）", placeholder="1623730 或 Steam 商店链接"),
                FieldSpec("target_game_name", "目标游戏名（手填 SteamID 时可选）", placeholder="Palworld"),
                FieldSpec(
                    "language",
                    "成就语言",
                    kind="combo",
                    options=("english", "schinese", "japanese", "tchinese", "koreana", "french", "german", "spanish", "russian"),
                    default="english",
                ),
            ],
            height=760,
        )

    def validate_values(self, values):
        from pathlib import Path
        from src.platforms.steam.api import parse_steam_ids

        if not str(values.get("api_key", "")).strip():
            raise ValueError("Steam Web API Key 不能为空。")
        xlsx_path = str(values.get("xlsx_path", "")).strip()
        steam_ids = parse_steam_ids(values.get("steam_ids", ""))
        if not xlsx_path and not steam_ids:
            raise ValueError("请至少选择一个玩家评论 Excel，或输入 SteamID/玩家主页链接。")
        if xlsx_path and not Path(xlsx_path).exists():
            raise ValueError(f"玩家评论 Excel 不存在：{xlsx_path}")

    def tool_config_params(self):
        return [
            *_player_profile_config_params(),
            ConfigParam("request_timeout", "API 请求超时(秒)", kind="int", default=30, minimum=5, maximum=180),
            ConfigParam("request_delay", "单请求间隔(秒)", kind="float", default=0.2, minimum=0.0, maximum=10.0, step=0.1, decimals=1),
            ConfigParam(
                "save_batch_size",
                "每批保存条数",
                kind="int",
                default=DEFAULT_XLSX_AUTOSAVE_ROWS,
                minimum=DEFAULT_XLSX_AUTOSAVE_ROWS,
                maximum=100000,
                step=1000,
            ),
        ]

    def run_task(self, values, log_callback, finish_callback, stop_event, pause_event):
        from src.platforms.steam.api import run_steam_player_profiles_spider

        config_keys = {
            "max_player_profiles",
            "player_parallel_workers",
            "collect_player_owned_games",
            "player_owned_games_limit",
            "collect_player_recent_games",
            "collect_player_target_achievements",
            "collect_player_badges",
            "collect_player_friends",
            "request_timeout",
            "request_delay",
            "save_batch_size",
        }
        config = {k: v for k, v in values.items() if k in config_keys}
        return run_steam_player_profiles_spider(
            str(values.get("api_key", "")).strip(),
            values.get("xlsx_path", ""),
            values.get("steam_ids", ""),
            values.get("target_appid", ""),
            values.get("target_game_name", ""),
            values.get("language", "english"),
            log_callback,
            finish_callback,
            stop_event,
            pause_event=pause_event,
            config=config,
        )
