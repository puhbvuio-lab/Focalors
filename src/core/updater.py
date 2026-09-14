"""版本更新检查模块。

通过 GitHub API v3 获取仓库最新 release，与本地版本号比较，
判断是否存在可用的新版本。

如需提高 API 额度（5000 次/小时），可在 .env 中配置：
    GITHUB_TOKEN=ghp_xxxxxxxxxxxxxxxxxxxx
Token 无需任何 scope（公开仓库即可）。
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path

import requests
from dotenv import load_dotenv

# 确保从项目根目录加载 .env
load_dotenv(Path(__file__).resolve().parents[2] / ".env")

logger = logging.getLogger(__name__)

# GitHub API 请求超时（秒）
REQUEST_TIMEOUT = 15
# GitHub API 要求的 User-Agent 头
USER_AGENT = "social-platform-scraper"
DEFAULT_UPDATE_REPO_OWNER = "puhbvuio-lab"
DEFAULT_UPDATE_REPO_NAME = "funina"
# 可选：GitHub Personal Access Token，用于提高 API 额度（60→5000 次/小时）
_GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")


def parse_semver(version_string: str) -> tuple[int, int, int] | None:
    """
    解析语义化版本字符串为 (major, minor, patch) 三元组。

    支持带有 pre-release 后缀的版本号（如 "2.0.0-beta"），
    仅提取前三段数字部分进行比较。

    Returns:
        解析成功返回 (major, minor, patch)，失败返回 None。
    """
    if not version_string:
        return None
    # 匹配 major.minor.patch（可选 pre-release 后缀）
    match = re.match(r"^(\d+)\.(\d+)\.(\d+)", version_string.strip())
    if not match:
        return None
    return (int(match.group(1)), int(match.group(2)), int(match.group(3)))


def is_newer(local_version: str, remote_version: str) -> bool:
    """
    比较两个语义化版本号，判断远程版本是否大于本地版本。

    优先使用三元组逐位比较；解析失败时退回字符串自然排序比较。

    Args:
        local_version: 本地版本号（如 "1.0.0"）
        remote_version: 远程版本号（如 "2.1.0"，不含 v 前缀）

    Returns:
        True 表示远程版本 > 本地版本。
    """
    local_tuple = parse_semver(local_version)
    remote_tuple = parse_semver(remote_version)

    if local_tuple is not None and remote_tuple is not None:
        return remote_tuple > local_tuple

    # 兜底：字符串直接比较（对非标准版本号容错）
    logger.warning("版本号解析失败，退回字符串比较：local=%s remote=%s", local_version, remote_version)
    return remote_version > local_version


def _github_headers(use_token: bool = True) -> dict[str, str]:
    headers = {"User-Agent": USER_AGENT}
    if use_token and _GITHUB_TOKEN:
        headers["Authorization"] = f"Bearer {_GITHUB_TOKEN}"
    return headers


def check_for_updates(
    current_version: str,
    repo_owner: str = DEFAULT_UPDATE_REPO_OWNER,
    repo_name: str = DEFAULT_UPDATE_REPO_NAME,
) -> tuple[bool, str | None, str | None]:
    """
    检查 GitHub 仓库是否有新版本 release。

    通过 GitHub API v3 获取最新的 release 标签，
    与当前本地版本号比较，判断是否需要更新。

    如果 .env 中配置了 GITHUB_TOKEN，自动使用 Token 认证，
    将 API 额度从 60 次/小时提升至 5000 次/小时。

    Args:
        current_version: 当前本地版本号，如 "1.0.0"
        repo_owner: GitHub 仓库所有者
        repo_name: GitHub 仓库名称

    Returns:
        (has_update, latest_version, release_url) 三元组：
        - has_update: True 表示存在新版本
        - latest_version: 远程最新版本号（去掉 v 前缀），无 release 时为 None
        - release_url: 新版本 release 的 HTML 地址，无 release 时为 None

    Raises:
        requests.RequestException: 网络请求失败时抛出
        ValueError: API 返回非预期数据结构时抛出
    """
    url = f"https://api.github.com/repos/{repo_owner}/{repo_name}/releases/latest"

    logger.info("正在检查更新：%s/%s", repo_owner, repo_name)
    response = requests.get(url, headers=_github_headers(use_token=True), timeout=REQUEST_TIMEOUT)

    if response.status_code == 401 and _GITHUB_TOKEN:
        logger.warning("GitHub token unauthorized while checking updates; retrying without token.")
        response = requests.get(url, headers=_github_headers(use_token=False), timeout=REQUEST_TIMEOUT)

    # 404 通常表示仓库没有 release 或不可访问；401/403 不应打断主界面。
    if response.status_code in {401, 403, 404}:
        logger.info("更新源不可访问或没有 release（HTTP %s），跳过更新检查。", response.status_code)
        return (False, None, None)

    response.raise_for_status()
    data = response.json()

    tag_name = data.get("tag_name", "")
    if not tag_name:
        raise ValueError("GitHub API 返回的 release 数据缺少 tag_name 字段")

    # 去掉 "v" 前缀
    remote_version = tag_name.lstrip("v")
    release_url = data.get("html_url", "")

    logger.info("远程最新版本：%s（原始 tag：%s）", remote_version, tag_name)

    has_update = is_newer(current_version, remote_version)
    return (has_update, remote_version, release_url)
