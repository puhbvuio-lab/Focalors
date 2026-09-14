# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

A PyQt5 desktop application for social media data extraction from YouTube, TikTok, X/Twitter, Instagram, and Facebook. Includes AIGC content validation (langgraph + DeepSeek) and XLSX file merging utilities.

## Setup and Execution

```bash
.\.venv\Scripts\pip.exe install -r requirements.txt
.\.venv\Scripts\python.exe -m playwright install chromium
.\.venv\Scripts\python.exe main.py
```

Environment variables for AIGC features (create `.env` in project root):
```
DEEPSEEK_API_KEY=your_api_key
DEEPSEEK_BASE_URL=https://api.deepseek.com
DEEPSEEK_MODEL_NAME=deepseek-chat
```

## Common Commands

All commands must use the project virtual environment (`.venv`):

- **Run application**: `.\.venv\Scripts\python.exe main.py`
- **Run linter**: `.\.venv\Scripts\python.exe -m ruff check .`
- **Run single test**: `.\.venv\Scripts\python.exe test/test_visibility.py`
- **Check tool integrity**: `.\.venv\Scripts\python.exe src/studio/tool_runner.py --check --tool-id <tool_id>`

## Architecture

### Process Isolation Model

The app uses a **two-level process architecture**:
1. **Main process** (`src/studio/qt_app.py`): Hosts the studio UI, tool discovery, and registry.
2. **Tool subprocesses** (`src/studio/tool_runner.py`): Each tool launches as an isolated QProcess. A tool crash doesn't affect the main window.

When a tool is launched, the main process spawns `tool_runner.py` with `--tool-id <id>`, which reflectively loads the entrypoint class specified in the manifest.

### Tool Discovery System

Tools are defined by **manifest files** (`*.manifest.json`) scanned from `src/platforms/` and `src/processing/`. Each manifest declares:
- `tool_id`: Globally unique identifier
- `entrypoint`: Dotted path to a QWidget class (e.g., `src.platforms.youtube.windows.YouTubeKeywordWindow`)
- `implementation_path`: Relative path to the scraper logic

Discovery happens at startup and on "reload tools" button press. See `src/studio/discovery.py`.

### Configuration System

`src/core/config_store.py` manages per-tool JSON config files stored in `config/` directory:
- Each tool has a `DEFAULT_CONFIGS` entry with type-safe defaults
- Config values are loaded with automatic type coercion from JSON
- Supports **named profiles** (e.g., `tool_id_profileName.json`)
- **Global config** (`__global__`) provides 9 shared parameters (`page_load_timeout`, `scroll_interval`, `no_new_scroll_limit`, `max_scrolls`, `scroll_px`, `cooldown_min`, `cooldown_max`, `save_batch_size`, `comment_top_limit`) with alias mapping across tools (e.g., `page_load_timeout` → `youtube_browser_page_timeout`)
- **Merge priority**: tool JSON > global JSON > tool defaults. Alias params: if user hasn't modified the alias key in the tool dialog, the global value takes effect; if user explicitly set a different value, tool value wins.

### Browser Automation

`src/core/browser.py` handles Chrome CDP connections:
- Uses `user_data/` for persistent browser sessions (login state preserved across runs)
- `connect_existing_chromium()` is the standard entry point — always use `with sync_playwright() as p:` pattern
- Auto-launches Chrome with CDP port if not already running

### Excel Output

- `src/core/xlsx.py` provides two writer classes:
  - `MultiSheetXlsxWriter`: Initialize with dict of `{sheet_name: [headers]}` for multi-sheet files
  - `XlsxRowWriter`: Simple single-sheet writer with flat header list
- Output goes to `output/<platform>/` subdirectories

## Development Conventions

- **Linter**: `ruff` with `line-length = 150`, ignoring `E402` and `F841`
- **UI Framework**: PyQt5 (no Qt Designer files)
- **Browser**: Playwright with persistent contexts; always use `sync_playwright` pattern; prefer `wait_until="load"` over `"domcontentloaded"` for SPA pages
- **Data Input**: TXT files, one record per line, skip `#` lines and empty lines
- **Data Output**: `.xlsx` files in `output/` subdirectories; use `autosave_every` parameter on `XlsxRowWriter`/`MultiSheetXlsxWriter` to control save frequency
- **YouTube API**: Use `_api_execute_with_retry()` helper (found in `keyword.py` and `channel_works.py`) to wrap `.execute()` calls with exponential backoff retry for transient 500/503/429 errors
- **Config**: Use `config.get("key", DEFAULT)` to read parameters, never use module-level constants directly. Use `interruptible_sleep` not `time.sleep`. Use `random_cooldown` for rate limiting.

## Adding a New Tool

1. Create implementation file: `src/platforms/<platform>/new_tool.py`
2. Add window class to `src/platforms/<platform>/windows.py`
3. Create manifest: `src/platforms/<platform>/new_tool.manifest.json`
4. Add defaults to `DEFAULT_CONFIGS` in `src/core/config_store.py` if the tool needs configurable parameters
5. Click "Reload Tools" in the main window (no restart needed)
