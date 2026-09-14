"""
YouTube Shorts 专项工具模块，用于抓取官方 API 无法提供的特殊字段，例如 Shorts 关联的普通视频信息。
"""

import json
import re

import requests


def fetch_short_related_video(video_id: str) -> tuple[str, str]:
    """
    爬取 YouTube Shorts 页面，从 ytInitialData 中提取其关联的普通视频（Created from）。

    使用 requests 库发起请求，可自动读取 HTTP_PROXY / HTTPS_PROXY 环境变量走全局代理。

    Args:
        video_id: Shorts 的视频 ID。

    Returns:
        tuple[str, str]: (关联视频标题, 关联视频链接)。如果没有关联视频或抓取失败，返回 ("", "")。
    """
    url = f"https://www.youtube.com/shorts/{video_id}"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    }

    try:
        resp = requests.get(url, headers=headers, timeout=10)
        resp.raise_for_status()
        html = resp.text

        match = re.search(r"ytInitialData\s*=\s*(\{.*?\});\s*</script>", html)
        if not match:
            return "", ""

        data = json.loads(match.group(1))

        related_title = ""
        related_link = ""

        def find_related(d):
            nonlocal related_title, related_link
            if related_title and related_link:
                return
            if isinstance(d, dict):
                # 检查是否存在 reelMultiFormatLinkViewModel
                if "reelMultiFormatLinkViewModel" in d:
                    vm = d["reelMultiFormatLinkViewModel"]
                    try:
                        title = vm.get("title", {}).get("content", "")
                        cmd = vm.get("command", {}).get("innertubeCommand", {})
                        watch_ep = cmd.get("watchEndpoint", {})
                        rel_vid = watch_ep.get("videoId", "")

                        if title and rel_vid:
                            related_title = title
                            related_link = f"https://www.youtube.com/watch?v={rel_vid}"
                            return
                    except Exception:
                        pass
                for k, v in d.items():
                    find_related(v)
            elif isinstance(d, list):
                for item in d:
                    find_related(item)

        find_related(data)
        return related_title, related_link
    except Exception:
        return "", ""
