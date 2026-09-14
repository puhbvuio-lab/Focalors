import sys
from pathlib import Path

# 添加项目根目录到 sys.path 以便导入 src
project_root = Path(__file__).parents[2]
sys.path.append(str(project_root))

from src.platforms.tiktok.profile_videos import run_tiktok_profile_videos_spider
from src.core import DEFAULT_TIKTOK_CDP_URL

def main():
    # 测试输入文件，你可以修改为你的真实测试账号
    txt_path = str(Path(__file__).parent / "test_input.txt")
    
    def log_callback(message):
        print(f"[LOG] {message}")
        
    def finish_callback(output_path):
        print(f"\n[FINISH] 任务已完成，输出文件路径: {output_path}")
        
    print("=== 开始测试 TikTok 博主视频及评论采集 ===")
    
    # 模拟 UI 的各个选项
    # 限制时间：否
    # 视频信息：是
    # 评论信息：是
    run_tiktok_profile_videos_spider(
        txt_path=txt_path,
        start_date="2025-05-06",
        end_date="2026-05-06",
        limit_time_str="否",
        max_scrolls=1,          # 为了快速测试，设置最大只滚动1次
        get_video_info_str="是",
        get_comments_str="是",
        max_comments=10,        # 为了快速测试，每个视频最多只抓10条评论
        fetch_play_counts_str="否",
        cdp_port_or_url=DEFAULT_TIKTOK_CDP_URL,
        log_callback=log_callback,
        finish_callback=finish_callback,
        stop_event=None
    )

if __name__ == "__main__":
    main()
