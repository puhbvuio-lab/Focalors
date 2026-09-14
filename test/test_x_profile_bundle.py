from pathlib import Path
import sys

project_root = Path(__file__).parent.parent
sys.path.append(str(project_root))

from src.platforms.x_twitter.profile_bundle import build_profile_row, build_tweet_row
from src.platforms.x_twitter.windows import XProfileBundleWindow
from src.studio.discovery import discover_tools
from src.studio.registry import TOOLS


def test_x_profile_bundle_rows():
    profile_record = {
        "作者主页链接": "https://x.com/demo",
        "作者的名称": "Demo",
        "账号ID": "demo",
        "粉丝数": "1000",
        "简介": "bio",
    }
    profile_row = build_profile_row(profile_record)
    tweet_row = build_tweet_row(
        1,
        profile_record,
        {
            "post_id": "123",
            "url": "https://x.com/demo/status/123",
            "published_at": "2026-06-01 00:00:00",
            "content": "first\npost",
            "views": "100",
            "likes": "10",
            "replies": "2",
        },
    )

    assert profile_row["作者主页链接"] == "https://x.com/demo"
    assert profile_row["作者名称"] == "Demo"
    assert profile_row["作者ID"] == "demo"
    assert profile_row["粉丝量"] == "1000"
    assert tweet_row["推文链接"] == "https://x.com/demo/status/123"
    assert tweet_row["作品链接"] == "https://x.com/demo/status/123"
    assert tweet_row["博主主页链接"] == "https://x.com/demo"
    assert tweet_row["标题"] == "first post"
    assert tweet_row["作品内容"] == "first post[推文]"
    assert tweet_row["频道名称"] == "Demo"
    assert tweet_row["作品类型"] == "推文"
    assert tweet_row["浏览量"] == "100"
    assert tweet_row["点赞数"] == "10"


def test_x_profile_bundle_defaults_and_discovery():
    window = XProfileBundleWindow.__new__(XProfileBundleWindow)
    defaults = {param.key: param.default for param in window.tool_config_params()}

    assert defaults["max_tweets_per_author"] == 50
    assert defaults["max_scrolls"] == 80
    assert defaults["profile_entry_mode"] == "直接打开"

    static_ids = {tool.tool_id for tool in TOOLS}
    assert "x_profile_bundle" in static_ids

    discovered, errors = discover_tools()
    discovered_ids = {tool.tool_id for tool in discovered}
    assert "x_profile_bundle" in discovered_ids
    assert not [error for error in errors if "x_profile_bundle" in error]


if __name__ == "__main__":
    test_x_profile_bundle_rows()
    test_x_profile_bundle_defaults_and_discovery()
    print("x profile bundle tests passed")
