"""应用版本号定义。

版本号存储在 config/version.json 中，
发布新版本时修改该文件的 version 字段即可。
"""

from __future__ import annotations

import json
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_VERSION_JSON = _PROJECT_ROOT / "config" / "version.json"
DEFAULT_VERSION = "2.0.0"


def get_version() -> str:
    """从 config/version.json 读取当前版本号。"""
    try:
        data = json.loads(_VERSION_JSON.read_text(encoding="utf-8"))
        return data.get("version", DEFAULT_VERSION)
    except Exception:
        return DEFAULT_VERSION


def set_version(version: str) -> None:
    """将新版本号写入 config/version.json。"""
    _VERSION_JSON.parent.mkdir(parents=True, exist_ok=True)
    _VERSION_JSON.write_text(
        json.dumps({"version": version}, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


__version__ = get_version()
