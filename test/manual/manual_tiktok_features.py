import sys
from pathlib import Path

project_root = Path(__file__).parents[2]
sys.path.append(str(project_root))

from src.platforms.tiktok.profile_play_counts import run_tiktok_profile_play_counts_spider
from src.platforms.tiktok.profile_videos import run_tiktok_profile_videos_spider
from src.core import DEFAULT_TIKTOK_CDP_URL

def main():
    txt_path = str(Path(__file__).parent / "test_input.txt")
    
    def log_callback(message):
        print(f"[LOG] {message}")
        
    def finish_callback(output_path):
        print(f"\n[FINISH] 任务已完成，输出文件路径: {output_path}")

    # ==========================================
    # 1. 测试独立工具：TikTok 博主视频播放量
    # ==========================================
    print("\n\n=== 1. 测试独立工具：TikTok 博主视频播放量 ===")
    run_tiktok_profile_play_counts_spider(
        txt_path=txt_path,
        cdp_port_or_url=DEFAULT_TIKTOK_CDP_URL,
        max_scrolls=1,
        log_callback=log_callback,
        finish_callback=finish_callback,
        stop_event=None
    )

    # ==========================================
    # 2. 测试整合增强：TikTok 博主视频采集（开启爬取播放量，开启保底前五条）
    # ==========================================
    print("\n\n=== 2. 测试整合工具：TikTok 博主视频采集（带播放量与前五条保底） ===")
    run_tiktok_profile_videos_spider(
        txt_path=txt_path,
        start_date="2025-05-06",
        end_date="2026-05-06",
        limit_time_str="是",
        max_scrolls=1,
        get_video_info_str="是",
        get_comments_str="否",
        max_comments=10,
        fetch_play_counts_str="是",
        cdp_port_or_url=DEFAULT_TIKTOK_CDP_URL,
        log_callback=log_callback,
        finish_callback=finish_callback,
        stop_event=None
    )

if __name__ == "__main__":
    main()
