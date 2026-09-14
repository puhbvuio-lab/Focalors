import os
import sys

# Add project root to sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from src.platforms.x_twitter.profile_tweets import run_x_profile_tweets_spider
from src.core import DEFAULT_X_CDP_URL

def log_callback(msg):
    print(msg)

def finish_callback(output_path):
    print(f"Test finished! Output saved to: {output_path}")

def run_test():
    profile_urls_text = "https://x.com/elonmusk"
    keywords_text = "AI\nTesla"
    limit_time_str = "否"
    start_date = "2025-05-06"
    end_date = "2026-05-06"
    get_comments_str = "否"
    max_comments = 50

    # Limit scrolls to 2 for quick testing
    config = {
        "max_scrolls": 2,
        "page_load_timeout": 30000,
        "scroll_interval": 3.0,
        "no_new_scroll_limit": 2,
        "save_batch_size": 10,
        "cooldown_min": 1.0,
        "cooldown_max": 2.0,
    }

    print("Starting X Profile Keywords Search Test...")
    run_x_profile_tweets_spider(
        profile_urls_text=profile_urls_text,
        keywords_text=keywords_text,
        limit_time_str=limit_time_str,
        start_date=start_date,
        end_date=end_date,
        get_comments_str=get_comments_str,
        max_comments=max_comments,
        cdp_port_or_url=DEFAULT_X_CDP_URL,
        log_callback=log_callback,
        finish_callback=finish_callback,
        config=config,
    )

if __name__ == "__main__":
    run_test()
