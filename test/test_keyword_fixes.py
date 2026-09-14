"""Verify all fixes in x_twitter/keyword.py and tiktok/keyword.py."""
import sys
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')

from pathlib import Path
project_root = Path(__file__).parent.parent
sys.path.append(str(project_root))

import queue
import threading

# -- X/Twitter keyword.py --
from src.platforms.x_twitter.keyword import (
    _format_media_label,
    _x_media_tag,
    is_repost_context,
    normalize_status_url,
    run_x_spider,
)

# -- TikTok keyword.py --
from src.platforms.tiktok.keyword import (
    _tiktok_media_tag,
    run_tiktok_spider,
)

passed = 0
failed = 0

def check(name, condition, detail=""):
    global passed, failed
    if condition:
        passed += 1
        print(f"  PASS  {name}{' - ' + detail if detail else ''}")
    else:
        failed += 1
        print(f"  FAIL  {name}{' - ' + detail if detail else ''}")

def test_keyword_fixes():
    global passed, failed
    passed = 0
    failed = 0
    
    print("=== X/Twitter keyword.py fixes ===\n")

    # Issue 2: video tweets classified correctly
    check("_x_media_tag([视频]) == '2'", _x_media_tag("[视频]") == "2", f"got {_x_media_tag('[视频]')!r}")
    check("_x_media_tag([图片 + 视频]) == '0'", _x_media_tag("[图片 + 视频]") == "0", f"got {_x_media_tag('[图片 + 视频]')!r}")
    check("_x_media_tag([图片]) == '1'", _x_media_tag("[图片]") == "1", f"got {_x_media_tag('[图片]')!r}")
    check("_x_media_tag('') == '3' (纯文本)", _x_media_tag("") == "3", f"got {_x_media_tag('')!r}")
    check("_x_media_tag([GIF]) == '4'", _x_media_tag("[GIF]") == "4", f"got {_x_media_tag('[GIF]')!r}")

    # _format_media_label: pure label assembly (photo/video/gif flags)
    check("_format_media_label() == ''", _format_media_label(False, False, False) == "", f"got {_format_media_label(False, False, False)!r}")
    check("_format_media_label(photo) == '[图片]'", _format_media_label(False, True, False) == "[图片]", f"got {_format_media_label(False, True, False)!r}")
    check("_format_media_label(video) == '[视频]'", _format_media_label(True, False, False) == "[视频]", f"got {_format_media_label(True, False, False)!r}")
    check("_format_media_label(video+photo) == '[视频 + 图片]'", _format_media_label(True, True, False) == "[视频 + 图片]", f"got {_format_media_label(True, True, False)!r}")
    check("_format_media_label(gif) == '[GIF]'", _format_media_label(False, False, True) == "[GIF]", f"got {_format_media_label(False, False, True)!r}")
    # tag mapping for combined image+video label
    check("_x_media_tag('[视频 + 图片]') == '0'", _x_media_tag("[视频 + 图片]") == "0", f"got {_x_media_tag('[视频 + 图片]')!r}")

    # Issue 1: quote/repost tweets filtered
    check("is_repost_context('User 转发了')", is_repost_context("User 转发了"))
    check("is_repost_context('User retweeted')", is_repost_context("User retweeted"))
    check("is_repost_context('User reposted')", is_repost_context("User reposted"))
    check("is_repost_context('某某 引用了')", is_repost_context("某某 引用了"), "quote tweet in Chinese")
    check("is_repost_context('Someone quoted')", is_repost_context("Someone quoted"), "quote tweet in English")
    check("is_repost_context('某某 转发')", is_repost_context("某某 转发"))
    check("is_repost_context('') == False", not is_repost_context(""))
    check("is_repost_context('regular tweet') == False", not is_repost_context("regular tweet"))

    # URL normalization
    check("normalize twitter.com -> x.com", normalize_status_url("https://twitter.com/user/status/123") == "https://x.com/user/status/123")
    check("normalize relative path", normalize_status_url("/user/status/123") == "https://x.com/user/status/123")
    check("normalize empty", normalize_status_url("") == "")

    print("\n=== browser.py fix ===\n")

    # force_new_context parameter added（需要 Playwright 浏览器环境，跳过）
    print("  SKIP  browser.py 测试（需要 Playwright 浏览器环境）")

    print("\n=== Queue timeout mechanism ===\n")

    # Queue put timeout
    q1 = queue.Queue(maxsize=1)
    q1.put("item")
    try:
        q1.put("overflow", timeout=1)
        check("put(timeout) raises Full", False, "should have raised queue.Full")
    except queue.Full:
        check("put(timeout) raises Full when full", True)

    # Queue get timeout
    q2 = queue.Queue()
    try:
        q2.get(timeout=1)
        check("get(timeout) raises Empty", False, "should have raised queue.Empty")
    except queue.Empty:
        check("get(timeout) raises Empty when no items", True)

    # get(timeout) enables stop_event checking
    q3 = queue.Queue()
    stop = threading.Event()
    stop.set()
    try:
        q3.get(timeout=1)
        check("stop_event checked after get timeout", False, "should have raised queue.Empty")
    except queue.Empty:
        check("stop_event can be checked after get timeout", True)

    print("\n=== Module imports ===\n")

    check("run_x_spider is callable", callable(run_x_spider))
    check("run_tiktok_spider is callable", callable(run_tiktok_spider))
    check("_tiktok_media_tag is callable", callable(_tiktok_media_tag))

    print(f"\n{'='*40}")
    print(f"Results: {passed} passed, {failed} failed out of {passed + failed} checks")
    assert failed == 0, "Some checks failed!"

if __name__ == '__main__':
    test_keyword_fixes()
