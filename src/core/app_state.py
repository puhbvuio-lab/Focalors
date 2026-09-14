"""Stable per-machine state paths used across installs."""

from __future__ import annotations

import os
from pathlib import Path

APP_STATE_DIR_NAME = "SocialPlatformScraper"


def get_app_state_root(create: bool = True) -> Path:
    """Return a stable local state directory that does not move with code updates."""
    override = os.environ.get("SCRAPER_STATE_DIR")
    if override:
        root = Path(override).expanduser()
    elif os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
        root = Path(base) / APP_STATE_DIR_NAME if base else Path.home() / APP_STATE_DIR_NAME
    else:
        base = os.environ.get("XDG_STATE_HOME")
        root = Path(base) / "social-platform-scraper" if base else Path.home() / ".local" / "state" / "social-platform-scraper"
    if create:
        root.mkdir(parents=True, exist_ok=True)
    return root
