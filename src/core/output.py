"""
文件输出路径管理模块，提供规范化的工作空间、输出文件夹路径解析，支持针对不同平台分类存放。
"""

from __future__ import annotations

import re
from pathlib import Path

# 社交平台别名与子目录映射关系，统一合并为规范名称（例如 twitter 和 x_platform 统一归入 x）
PLATFORM_DIRS = {
    "youtube": "youtube",
    "tiktok": "tiktok",
    "x": "x",
    "x_platform": "x",
    "twitter": "x",
    "data": "data",
    "steam": "steam",
}

# 用于从文件名中识别动作日期（YYYYMMDD）的匹配模式。
# 要求 8 位数字前后均非数字，避免误匹配用户名或时间戳中的片段。
_DATE_IN_FILENAME_RE = re.compile(r"(?<!\d)(\d{8})(?!\d)")


def _safe_path_segment(value: str) -> str:
    """
    校验并清洗作为目录层级的字符串，防止路径穿越与非法字符。
    """
    if not value:
        raise ValueError("Path segment must not be empty.")
    if ".." in value or "/" in value or "\\" in value:
        raise ValueError(f"Invalid path segment: {value}")
    cleaned = re.sub(r'[\\/*?:"<>|]', "", value).strip()
    if not cleaned:
        raise ValueError(f"Invalid path segment after cleanup: {value}")
    return cleaned


def get_workspace_root() -> Path:
    """
    自动推导项目的根目录（即包含 requirements.txt 和 main.py 的那级目录）。
    支持 PyInstaller 打包后的 EXE 环境。
    """
    import sys

    # PyInstaller 打包环境：以 EXE 所在目录为工作空间根目录
    if getattr(sys, 'frozen', False):
        root = Path(sys.executable).resolve().parent
        return root

    # 源码环境：__file__ 是 src/core/output.py，向上两级到项目根
    root = Path(__file__).resolve().parents[2]
    if not (root / "requirements.txt").exists() and not (root / "main.py").exists():
        raise RuntimeError(f"Workspace root not found at {root}")
    return root


def get_output_root() -> Path:
    """
    获取或创建统一输出文件的根目录（工作空间下的 output 目录）。
    """
    output_root = get_workspace_root() / "output"
    output_root.mkdir(parents=True, exist_ok=True)
    return output_root


def get_platform_output_dir(platform: str, channel: str | None = None, run_date: str | None = None) -> Path:
    """
    根据平台标识获取或创建其对应的专属输出子目录。

    目录层级约定：
        output/<平台>/[<渠道>/[<动作日期>/]]

    Args:
        platform: 平台名（如 'tiktok', 'youtube' 等）
        channel: 渠道/动作类型（如 'keyword', 'profiles', 'top_comments'）。可选。
        run_date: 动作日期（YYYYMMDD）。可选，传入时会作为独立子目录。

    Returns:
        Path: 对应的 Path 对象
    """
    # 路径安全防护：防止恶意传入包含 '..' 或斜杠的文件名，绕过限定范围实施路径穿越攻击
    if ".." in platform or "/" in platform or "\\" in platform:
        raise ValueError(f"Invalid platform name: {platform}")
    folder_name = PLATFORM_DIRS.get(platform, platform)
    parts = [get_output_root(), folder_name]
    if channel:
        parts.append(_safe_path_segment(channel))
    if run_date:
        parts.append(_safe_path_segment(run_date))
    output_dir = Path(*parts)
    output_dir.mkdir(parents=True, exist_ok=True)
    return output_dir


def derive_run_date_from_filename(filename: str) -> str | None:
    """
    从文件名中提取动作日期（YYYYMMDD），用于自动归类到日期子目录。
    匹配独立的 8 位数字，兼容 `20260622_192829` 与 `20260622` 两种时间戳写法。
    """
    match = _DATE_IN_FILENAME_RE.search(filename or "")
    return match.group(1) if match else None


def build_output_path(platform: str, filename: str, channel: str | None = None, run_date: str | None = None, organize: bool = True) -> str:
    """
    构造完整的数据输出文件存储路径（绝对路径字符串）。

    当 organize=True 时，按 `output/<平台>/<渠道>/<动作日期>/<文件名>` 归类存放：
      - channel：显式传入的渠道/动作类型，作为子目录。
      - run_date：若未显式传入，则自动从文件名中提取独立的 8 位日期。
    当 organize=False 时，退化为 `output/<平台>/<文件名>`（用于临时中转文件等）。

    Args:
        platform: 平台名
        filename: 目标文件名
        channel: 渠道/动作类型（可选）
        run_date: 动作日期 YYYYMMDD（可选，缺省时自动从文件名推断）
        organize: 是否启用渠道/日期分级目录

    Returns:
        str: 拼接后的绝对文件路径
    """
    if not organize:
        return str(get_platform_output_dir(platform) / filename)
    if run_date is None:
        run_date = derive_run_date_from_filename(filename)
    target_dir = get_platform_output_dir(platform, channel=channel, run_date=run_date)
    return str(target_dir / filename)
def generate_run_id() -> str:
    """
    生成全局唯一的任务执行 ID（使用时间戳+UUID随机八位字符，独占且唯一）。
    """
    import datetime
    import uuid
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    return f"{ts}_{uuid.uuid4().hex[:8]}"


def build_run_output_dir(tool_id: str, run_id: str) -> Path:
    """
    根据工具名和任务执行 ID 生成并创建唯一的产物输出目录。
    结构为: output/<tool_id>/<run_id>
    要求：只允许 [A-Za-z0-9._-]，防路径穿越，独占式创建目录。
    """
    import re
    CHAR_RE = re.compile(r"^[A-Za-z0-9._-]+$")
    if not CHAR_RE.match(tool_id) or not CHAR_RE.match(run_id):
        raise ValueError(f"Invalid character in tool_id or run_id: {tool_id}, {run_id}")
    if ".." in tool_id or ".." in run_id:
        raise ValueError("Path traversal detected")
    
    run_dir = get_output_root() / tool_id / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    return run_dir


def build_platform_run_output_dir(platform: str, channel: str, run_id: str) -> Path:
    """
    根据平台、渠道和任务执行 ID 生成并创建唯一的产物输出目录。
    结构为: output/<platform>/<channel>/<run_id>
    要求：run_id 只允许 [A-Za-z0-9._-]，防路径穿越，独占式创建目录。
    """
    import re
    CHAR_RE = re.compile(r"^[A-Za-z0-9._-]+$")
    if not CHAR_RE.match(run_id):
        raise ValueError(f"Invalid character in run_id: {run_id}")
    if ".." in run_id:
        raise ValueError("Path traversal detected")

    run_dir = get_platform_output_dir(platform, channel=channel) / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    return run_dir


def resolve_output_target(output_file: str | Path | None, run_dir: Path, default_name: str) -> Path:
    """
    解析输出目标路径。
    - output_file 为 None: 返回 run_dir / default_name
    - output_file 为仅文件名 (无父目录): 返回 run_dir / output_file
    - output_file 为绝对路径: 返回 Path(output_file)
    - output_file 为相对路径且包含父目录: 抛出 ValueError
    """
    if not output_file:
        return run_dir / default_name
        
    p = Path(output_file)
    if p.is_absolute():
        return p
        
    if len(p.parts) > 1:
        raise ValueError("XLSX_OUTPUT_INVALID: 相对路径包含子目录被拒绝，请使用绝对路径或仅文件名")
        
    return run_dir / p.name
