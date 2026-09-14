"""
浏览器控制模块，负责在 Windows 环境下查找、自动启动 Chrome / Edge 进程，并通过 Playwright
的 CDP (Chrome DevTools Protocol) 接口进行连接，支持持久化用户数据以保持登录态。

Edge 与 Chrome 同为 Chromium 内核，CDP 协议完全兼容，可由用户在全局配置里选择使用哪一个。
"""

from __future__ import annotations

import atexit
import csv
import json
import logging
import os
import socket
import subprocess
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen

from src.core.app_logging import log_line
from src.core.app_state import get_app_state_root

logger = logging.getLogger(__name__)

# 默认 CDP 调试接口地址
DEFAULT_X_CDP_URL = "http://localhost:9222"
DEFAULT_TIKTOK_CDP_URL = "http://localhost:9222"
DEFAULT_EDGE_CDP_URL = "http://localhost:9223"

# 浏览器类型标识
BROWSER_AUTO = "auto"
BROWSER_CHROME = "chrome"
BROWSER_EDGE = "edge"
SUPPORTED_BROWSERS = (BROWSER_AUTO, BROWSER_CHROME, BROWSER_EDGE)

# 自启浏览器的启动宽限期（秒）：Chrome 会先占用调试端口再开放 CDP 接口，
# 这段时间内即使 CDP 尚未就绪也不能判定为“卡死”并重启，否则会反复开关窗口。
STARTUP_GRACE_SECONDS = 25.0

# Chrome 默认安装路径
DEFAULT_CHROME_PATHS = (
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
)

# Edge 默认安装路径
DEFAULT_EDGE_PATHS = (
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
)

# 各浏览器的可执行文件名（用于受管 PID 校验与 LOCALAPPDATA 路径拼接）
_BROWSER_EXE_NAME = {
    BROWSER_CHROME: "chrome.exe",
    BROWSER_EDGE: "msedge.exe",
}

# 各浏览器在 LOCALAPPDATA 下的相对路径
_BROWSER_LOCAL_REL = {
    BROWSER_CHROME: ("Google", "Chrome", "Application", "chrome.exe"),
    BROWSER_EDGE: ("Microsoft", "Edge", "Application", "msedge.exe"),
}

_BROWSER_PROFILE_DIR_NAME = {
    BROWSER_CHROME: "chrome",
    BROWSER_EDGE: "edge",
}
_BROWSER_PROFILE_ENV = {
    BROWSER_CHROME: "SCRAPER_CHROME_USER_DATA_DIR",
    BROWSER_EDGE: "SCRAPER_EDGE_USER_DATA_DIR",
}


@dataclass(slots=True)
class ManagedBrowserProcess:
    pid: int
    browser_name: str
    host: str
    port: int
    user_data_dir: str | None
    launched_by_app: bool
    owner_pid: int
    started_at: datetime


# 仅保存当前 Python 进程主动拉起并登记的浏览器。
_chrome_processes: dict[int, subprocess.Popen] = {}
_managed_browser_processes: dict[int, ManagedBrowserProcess] = {}
_managed_browser_process_lock = threading.RLock()


def _cleanup_chrome() -> None:
    """
    进程退出时的清理勾子，仅终止当前应用登记过的浏览器进程。
    """
    current_pid = os.getpid()
    with _managed_browser_process_lock:
        owned_pids = [
            pid
            for pid, record in _managed_browser_processes.items()
            if record.owner_pid == current_pid and record.launched_by_app
        ]
    for pid in owned_pids:
        _terminate_managed_browser_pid(pid, force=True)
    with _managed_browser_process_lock:
        _chrome_processes.clear()
        _managed_browser_processes.clear()


atexit.register(_cleanup_chrome)


def build_cdp_url(port_or_url: str | int) -> str:
    """
    将端口号或简写 URL 规范化为完整的 HTTP CDP 连接地址。
    """
    value = str(port_or_url).strip()
    if not value:
        raise ValueError("CDP port or URL is required.")

    if value.startswith("http://") or value.startswith("https://"):
        return value

    return f"http://localhost:{value}"


def debug_port_from_cdp_url(port_or_url: str | int) -> str:
    """
    从给定的 CDP 端口或 URL 中解析提取出单纯的端口号。
    """
    cdp_url = build_cdp_url(port_or_url)
    parsed = urlparse(cdp_url)
    if parsed.port is not None:
        return str(parsed.port)
    return parsed.netloc or cdp_url


def cdp_url_for_browser(browser: str | None = None, default_url: str = DEFAULT_X_CDP_URL) -> str:
    """
    根据浏览器偏好返回推荐 CDP 地址。
    """
    resolved_browser = _resolve_browser_preference(browser) if browser else _get_configured_browser()
    if resolved_browser == BROWSER_EDGE:
        return DEFAULT_EDGE_CDP_URL
    return default_url


def get_workspace_root():
    """
    获取工作空间根目录。采用延迟导入以避免与 output 模块产生循环引用。
    """
    from src.core.output import get_workspace_root

    return get_workspace_root()


def _is_nonempty_dir(path: Path) -> bool:
    try:
        return path.is_dir() and any(path.iterdir())
    except OSError:
        return False


def _legacy_user_data_dir(browser: str) -> Path:
    dir_name = "user_data_edge" if browser == BROWSER_EDGE else "user_data"
    return get_workspace_root() / dir_name


def get_chrome_user_data_dir(browser: str = BROWSER_CHROME) -> str:
    """
    获取浏览器缓存及用户登录信息的存储路径。
    """
    resolved_browser = BROWSER_EDGE if browser == BROWSER_EDGE else BROWSER_CHROME
    override = os.environ.get(_BROWSER_PROFILE_ENV[resolved_browser])
    if override:
        user_data_dir = Path(override).expanduser()
        user_data_dir.mkdir(parents=True, exist_ok=True)
        return str(user_data_dir)

    stable_root = get_app_state_root() / "browser_profiles"
    stable_dir = stable_root / _BROWSER_PROFILE_DIR_NAME[resolved_browser]
    legacy_dir = _legacy_user_data_dir(resolved_browser)

    if _is_nonempty_dir(stable_dir):
        return str(stable_dir)
    if _is_nonempty_dir(legacy_dir):
        return str(legacy_dir)

    stable_dir.mkdir(parents=True, exist_ok=True)
    return str(stable_dir)


def _resolve_browser_preference(browser: str | None) -> str:
    """
    将用户传入的浏览器偏好规范化为具体的浏览器类型。
    """
    value = (browser or "").strip().lower() or BROWSER_AUTO
    if value == BROWSER_CHROME:
        return BROWSER_CHROME
    if value == BROWSER_EDGE:
        return BROWSER_EDGE
    if _find_executable_for(BROWSER_CHROME) is not None:
        return BROWSER_CHROME
    if _find_executable_for(BROWSER_EDGE) is not None:
        return BROWSER_EDGE
    return BROWSER_CHROME


def _find_executable_for(browser: str) -> str | None:
    """
    在已知安装路径中查找指定浏览器的可执行文件。
    """
    paths = DEFAULT_EDGE_PATHS if browser == BROWSER_EDGE else DEFAULT_CHROME_PATHS
    for path in paths:
        if os.path.exists(path):
            return path
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        rel = _BROWSER_LOCAL_REL.get(browser)
        if rel:
            candidate = os.path.join(local_app_data, *rel)
            if os.path.exists(candidate):
                return candidate
    return None


def find_browser_executable(browser: str = BROWSER_CHROME) -> str:
    """
    自动查找系统中的浏览器可执行文件路径。
    """
    path = _find_executable_for(browser)
    if path is not None:
        return path
    return _BROWSER_EXE_NAME.get(browser, "chrome.exe")


def find_chrome_executable() -> str:
    """
    兼容旧调用的别名：默认查找 Chrome 可执行文件。
    """
    return find_browser_executable(BROWSER_CHROME)


def chrome_launch_hint(port_or_url: str | int, browser: str = BROWSER_CHROME) -> str:
    """
    生成在命令行中手动启动浏览器的提示命令。
    """
    return (
        f'"{find_browser_executable(browser)}" '
        f"--remote-debugging-port={debug_port_from_cdp_url(port_or_url)} "
        "--remote-allow-origins=* "
        f'--user-data-dir="{get_chrome_user_data_dir(browser)}"'
    )


def _normalized_host(host: str | None) -> str:
    host_value = str(host or "").strip().lower()
    return "localhost" if host_value in {"127.0.0.1", "localhost", "::1"} else host_value


def _normalize_cdp_target(port_or_url: str | int) -> tuple[str, int, str]:
    cdp_url = build_cdp_url(port_or_url).rstrip("/")
    parsed = urlparse(cdp_url)
    if parsed.hostname is None or parsed.port is None:
        raise ValueError(f"Invalid CDP target: {port_or_url}")
    return _normalized_host(parsed.hostname), parsed.port, cdp_url


def is_tcp_port_open(host: str, port: int, timeout: float = 1.0) -> bool:
    """
    仅检测 TCP 端口是否可连接，不判断是否为 CDP。
    """
    try:
        with socket.create_connection((host, int(port)), timeout=max(0.1, float(timeout))):
            return True
    except OSError:
        return False


def _read_json_response(url: str, timeout: float) -> tuple[int | None, dict[str, Any] | None]:
    try:
        with urlopen(Request(url, headers={"User-Agent": "Mozilla/5.0"}), timeout=timeout) as response:
            raw = response.read().decode("utf-8", "ignore").strip()
            if not raw:
                return response.status, None
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                return response.status, None
            return response.status, data if isinstance(data, dict) else None
    except HTTPError as exc:
        raw = ""
        try:
            raw = exc.read().decode("utf-8", "ignore").strip()
        except Exception:
            pass
        if not raw:
            return exc.code, None
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return exc.code, None
        return exc.code, data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None, None


def is_valid_cdp_endpoint(base_url: str, timeout: float = 2.0) -> bool:
    """
    判断给定地址是否为可用的 CDP 端点。
    """
    cdp_url = build_cdp_url(base_url).rstrip("/")
    parsed = urlparse(cdp_url)
    if parsed.hostname is None or parsed.port is None:
        return False
    if not is_tcp_port_open(parsed.hostname, parsed.port, timeout=min(timeout, 1.0)):
        return False
    status, payload = _read_json_response(f"{cdp_url}/json/version", timeout=timeout)
    if status != 200 or not payload:
        return False
    websocket_url = payload.get("webSocketDebuggerUrl")
    if not isinstance(websocket_url, str) or not websocket_url.strip():
        return False
    browser_name = payload.get("Browser")
    if browser_name is not None and not isinstance(browser_name, str):
        return False
    return True


def is_cdp_available(port_or_url: str | int, timeout: float = 1.0) -> bool:
    """
    兼容旧调用：检测给定的 CDP 调试地址是否已经可用。
    """
    try:
        return is_valid_cdp_endpoint(build_cdp_url(port_or_url), timeout=timeout)
    except ValueError:
        return False


def _register_managed_browser_process(record: ManagedBrowserProcess, handle: subprocess.Popen) -> None:
    with _managed_browser_process_lock:
        _chrome_processes[record.pid] = handle
        _managed_browser_processes[record.pid] = record


def _forget_managed_browser_process(pid: int) -> None:
    with _managed_browser_process_lock:
        _chrome_processes.pop(pid, None)
        _managed_browser_processes.pop(pid, None)


def _is_process_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    with _managed_browser_process_lock:
        handle = _chrome_processes.get(pid)
    if handle is not None:
        return handle.poll() is None
    try:
        result = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
            capture_output=True,
            text=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception:
        return False
    output = (result.stdout or "").strip()
    if not output or "No tasks are running" in output:
        return False
    try:
        row = next(csv.reader([output]))
        return row[0].strip('"').lower() not in {"", "n/a"}
    except Exception:
        return True


def _process_image_name(pid: int) -> str | None:
    try:
        result = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
            capture_output=True,
            text=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception:
        return None
    output = (result.stdout or "").strip()
    if not output or "No tasks are running" in output:
        return None
    try:
        row = next(csv.reader([output]))
        return row[0].strip('"')
    except Exception:
        return None


def _get_managed_browser_process(port: int, host: str, browser: str | None = None) -> ManagedBrowserProcess | None:
    normalized_host = _normalized_host(host)
    with _managed_browser_process_lock:
        candidates = list(_managed_browser_processes.values())
    for record in candidates:
        if record.port != int(port):
            continue
        if _normalized_host(record.host) != normalized_host:
            continue
        if browser and record.browser_name != browser:
            continue
        if record.owner_pid != os.getpid() or not record.launched_by_app:
            continue
        if not _is_process_alive(record.pid):
            _forget_managed_browser_process(record.pid)
            continue
        return record
    return None


def _terminate_managed_browser_pid(pid: int, force: bool = False, log_callback=None) -> bool:
    with _managed_browser_process_lock:
        record = _managed_browser_processes.get(pid)
    if record is None or record.owner_pid != os.getpid() or not record.launched_by_app:
        return False
    if not _is_process_alive(pid):
        _forget_managed_browser_process(pid)
        return False
    current_image = _process_image_name(pid)
    expected_image = _BROWSER_EXE_NAME.get(record.browser_name, "").lower()
    if current_image and expected_image and current_image.lower() != expected_image:
        log_line(log_callback, f"跳过 PID {pid}，当前进程镜像 {current_image} 与登记的 {expected_image} 不一致。")
        return False

    command = ["taskkill", "/PID", str(pid), "/T"]
    if force:
        command.append("/F")
    try:
        result = subprocess.run(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception as exc:
        log_line(log_callback, f"无法终止受管浏览器 PID {pid}: {exc}")
        return False

    if result.returncode != 0 and _is_process_alive(pid):
        log_line(log_callback, f"受管浏览器 PID {pid} 终止失败，退出码 {result.returncode}")
        return False

    _forget_managed_browser_process(pid)
    return True


def _list_cdp_page_targets(port_or_url: str | int, timeout: float = 1.0) -> list[dict]:
    """
    查询 CDP `/json` 端点返回的 page 类型目标列表。
    """
    cdp_url = build_cdp_url(port_or_url).rstrip("/")
    try:
        with urlopen(f"{cdp_url}/json", timeout=timeout) as response:
            if response.status != 200:
                return []
            data = json.loads(response.read().decode("utf-8", "ignore"))
            if isinstance(data, list):
                return [target for target in data if isinstance(target, dict) and target.get("type") == "page"]
    except (OSError, ValueError, json.JSONDecodeError):
        return []
    return []


def _wait_for_initial_page(port_or_url: str | int, timeout: float = 8.0, log_callback=None) -> bool:
    """
    轮询等待浏览器至少存在一个 page 目标。
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _list_cdp_page_targets(port_or_url, timeout=1.0):
            return True
        time.sleep(0.3)
    log_line(log_callback, "未能在预期时间内检测到浏览器初始页面，继续尝试连接...")
    return False


def _open_cdp_page_target(port_or_url: str | int, url: str = "about:blank", timeout: float = 2.0) -> bool:
    """
    让已存在的 CDP 服务创建一个新的 page target。
    """
    cdp_url = build_cdp_url(port_or_url).rstrip("/")
    new_target_url = f"{cdp_url}/json/new?{quote(url, safe=':/?#[]@!$&()*+,;=%')}"
    for method in ("PUT", "GET"):
        try:
            request = Request(new_target_url, method=method)
            with urlopen(request, timeout=timeout) as response:
                if 200 <= response.status < 300:
                    return True
        except (OSError, ValueError):
            continue
    return False


def _ensure_cdp_page_target(port_or_url: str | int, log_callback=None, timeout: float = 5.0) -> bool:
    """
    确保 CDP 服务至少具备一个可用的 page target。
    """
    if _wait_for_initial_page(port_or_url, timeout=timeout, log_callback=None):
        return True
    log_line(log_callback, "CDP 已存活但没有可用页面，尝试创建新的空白页...")
    if _open_cdp_page_target(port_or_url):
        return _wait_for_initial_page(port_or_url, timeout=3.0, log_callback=log_callback)
    log_line(log_callback, "无法通过 CDP 创建空白页，后续将按策略决定是否重启浏览器。")
    return False


def _is_port_occupied(port_or_url: str | int, timeout: float = 1.0) -> bool:
    """
    检测端口是否已有 TCP 监听，不判断是否为有效 CDP。
    """
    try:
        host, port, _ = _normalize_cdp_target(port_or_url)
    except ValueError:
        return False
    return is_tcp_port_open(host, port, timeout=timeout)


def launch_chrome_for_cdp(port_or_url: str | int, browser: str = BROWSER_CHROME) -> subprocess.Popen:
    """
    启动带有 CDP 调试端口的浏览器，并登记为当前应用受管进程。
    """
    browser_path = find_browser_executable(browser)
    host, port, _ = _normalize_cdp_target(port_or_url)
    if host != "localhost":
        raise ValueError(f"Refusing to launch a local browser for remote CDP host: {host}")
    user_data_dir = get_chrome_user_data_dir(browser)

    chrome_env = os.environ.copy()
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        chrome_env.pop(key, None)

    process = subprocess.Popen(
        [
            browser_path,
            f"--remote-debugging-port={port}",
            "--remote-allow-origins=*",
            f"--user-data-dir={user_data_dir}",
            "--no-first-run",
            "--no-default-browser-check",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
        env=chrome_env,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    _register_managed_browser_process(
        ManagedBrowserProcess(
            pid=process.pid,
            browser_name=browser,
            host=host,
            port=port,
            user_data_dir=user_data_dir,
            launched_by_app=True,
            owner_pid=os.getpid(),
            started_at=datetime.now(timezone.utc),
        ),
        process,
    )
    return process


def _kill_chrome_on_port(port_or_url: str | int, log_callback=None, browser: str = BROWSER_CHROME) -> bool:
    """
    仅终止当前应用已登记的受管浏览器，不会按进程名全局清理。
    """
    host, port, _ = _normalize_cdp_target(port_or_url)
    record = _get_managed_browser_process(port, host, browser=browser)
    if record is None:
        log_line(log_callback, f"端口 {port} 被占用，但没有当前应用登记的受管浏览器实例。")
        return False
    log_line(log_callback, f"端口 {port} 对应的受管浏览器 PID {record.pid} 即将终止...")
    return _terminate_managed_browser_pid(record.pid, force=True, log_callback=log_callback)


def _get_configured_browser() -> str:
    """
    从全局配置读取用户选择的浏览器。
    """
    try:
        from src.core.config_store import GLOBAL_CONFIG_DEFAULTS, GLOBAL_TOOL_ID, load_config

        cfg = load_config(GLOBAL_TOOL_ID, GLOBAL_CONFIG_DEFAULTS, None)
        return _resolve_browser_preference(cfg.get("browser"))
    except Exception:
        return _resolve_browser_preference(BROWSER_AUTO)


def ensure_chrome_for_cdp(
    port_or_url: str | int,
    log_callback=None,
    wait_seconds: float = 20.0,
    browser: str | None = None,
) -> bool:
    """
    确保浏览器（Chrome 或 Edge）CDP 调试端点已就绪。

    返回值表示本次调用是否主动拉起了本地浏览器。
    """
    host, port, cdp_url = _normalize_cdp_target(port_or_url)
    is_local = host == "localhost"

    if browser:
        resolved_browser = _resolve_browser_preference(browser)
    elif is_local and port == urlparse(DEFAULT_EDGE_CDP_URL).port:
        resolved_browser = BROWSER_EDGE
    else:
        resolved_browser = _get_configured_browser()
    browser_display = "Edge" if resolved_browser == BROWSER_EDGE else "Chrome"

    if not is_local:
        if is_valid_cdp_endpoint(cdp_url, timeout=1.0):
            if _ensure_cdp_page_target(cdp_url, log_callback=log_callback, timeout=5.0):
                return False
            raise RuntimeError(f"Remote CDP endpoint has no usable page target: {cdp_url}")
        raise RuntimeError(f"Remote CDP endpoint is unavailable: {cdp_url}")

    if is_valid_cdp_endpoint(cdp_url, timeout=1.0):
        if _ensure_cdp_page_target(cdp_url, log_callback=log_callback, timeout=5.0):
            return False
        record = _get_managed_browser_process(port, host, browser=resolved_browser)
        if record is None:
            raise RuntimeError(
                f"{browser_display} CDP is reachable, but the browser is not managed by this app; "
                "refusing to terminate or relaunch it."
            )
        if not _kill_chrome_on_port(cdp_url, log_callback=log_callback, browser=resolved_browser):
            raise RuntimeError(
                f"{browser_display} CDP is reachable but the managed browser PID {record.pid} could not be terminated."
            )
    elif is_tcp_port_open(host, port, timeout=1.0):
        record = _get_managed_browser_process(port, host, browser=resolved_browser)
        if record is None:
            raise RuntimeError(
                f"Port {port} is occupied by a browser not launched by this app; refusing to terminate it."
            )
        if not _kill_chrome_on_port(cdp_url, log_callback=log_callback, browser=resolved_browser):
            raise RuntimeError(f"Port {port} is occupied and the managed browser PID {record.pid} could not be terminated.")

    log_line(log_callback, f"未检测到浏览器，正在自动启动 {browser_display}...")
    log_line(log_callback, f"{browser_display} 用户数据目录：{get_chrome_user_data_dir(resolved_browser)}")
    # 记录本次调用刚刚拉起的进程：Chrome 启动过程中会先占用端口再开放 CDP，
    # 若在此时按「端口被占用但 CDP 不可用」处理，会把它杀掉重启，
    # 形成反复开关浏览器窗口的现象。这里给自启实例留出启动宽限期。
    self_launched_at: dict[int, float] = {}

    def _launched_by_this_call(record) -> bool:
        started = self_launched_at.get(getattr(record, "pid", None))
        return started is not None and (time.time() - started) < STARTUP_GRACE_SECONDS

    def _launch_and_track() -> None:
        process = launch_chrome_for_cdp(cdp_url, browser=resolved_browser)
        pid = getattr(process, "pid", None)
        if pid:
            self_launched_at[pid] = time.time()

    _launch_and_track()
    launched = True

    deadline = time.time() + wait_seconds
    while time.time() < deadline:
        if is_valid_cdp_endpoint(cdp_url, timeout=1.0):
            if _ensure_cdp_page_target(cdp_url, timeout=10.0, log_callback=log_callback):
                log_line(log_callback, "浏览器已就绪，等待初始窗口稳定...")
                time.sleep(2.5)
                return launched
            record = _get_managed_browser_process(port, host, browser=resolved_browser)
            if record is None:
                raise RuntimeError(
                    f"{browser_display} CDP is reachable, but no page target can be created; "
                    "the browser is not managed by this app."
                )
            if _launched_by_this_call(record):
                time.sleep(0.4)
                continue
            if not _kill_chrome_on_port(cdp_url, log_callback=log_callback, browser=resolved_browser):
                raise RuntimeError(
                    f"{browser_display} CDP is reachable but the managed browser PID {record.pid} could not be terminated."
                )
            _launch_and_track()
            time.sleep(0.4)
            continue

        if is_tcp_port_open(host, port, timeout=0.5):
            record = _get_managed_browser_process(port, host, browser=resolved_browser)
            if record is None:
                raise RuntimeError(
                    f"Port {port} is occupied but not managed by this app; safe restart is not allowed."
                )
            if _launched_by_this_call(record):
                time.sleep(0.4)
                continue
            if not _kill_chrome_on_port(cdp_url, log_callback=log_callback, browser=resolved_browser):
                raise RuntimeError(f"Port {port} is occupied and the managed browser PID {record.pid} could not be terminated.")
            _launch_and_track()
        time.sleep(0.4)

    raise RuntimeError(
        f"{browser_display} 未能在 {wait_seconds}s 内启动在端口 {debug_port_from_cdp_url(cdp_url)}。"
        f"请检查 {browser_display} 是否已安装且未被阻止。"
    )


def _warmup_context(context, log_callback=None, attempts: int = 3) -> bool:
    """
    用一次轻量导航预热浏览器上下文。
    """
    for index in range(attempts):
        page = None
        try:
            page = context.new_page()
            page.goto("about:blank", wait_until="load", timeout=8000)
            return True
        except Exception:
            log_line(log_callback, f"浏览器预热中，等待初始窗口稳定（{index + 1}/{attempts}）...")
            time.sleep(1.5)
        finally:
            if page is not None:
                try:
                    page.close()
                except Exception:
                    pass
    log_line(log_callback, "浏览器预热未成功，仍尝试继续执行业务流程。")
    return False


def connect_existing_chromium(
    playwright: Any,
    port_or_url: str | int,
    *,
    context_index: int = 0,
    log_callback=None,
    warmup: bool = True,
    browser: str | None = None,
):
    """
    拉起（或确认）浏览器调试端口后，通过 Playwright 连接已有的 Chromium 实例。
    """
    launched = ensure_chrome_for_cdp(port_or_url, log_callback=log_callback, browser=browser)
    cdp_url = build_cdp_url(port_or_url)
    chromium = playwright.chromium.connect_over_cdp(cdp_url)

    deadline = time.time() + 10.0
    while time.time() < deadline and len(chromium.contexts) <= context_index:
        time.sleep(0.3)
    contexts = chromium.contexts
    context = contexts[context_index] if len(contexts) > context_index else chromium.new_context()

    if warmup and launched:
        _warmup_context(context, log_callback=log_callback)
    return chromium, context


def _is_page_closed(page) -> bool:
    """
    检测 Playwright page 是否已关闭或底层连接已断。
    """
    if page is None:
        return True
    try:
        if page.is_closed():
            return True
        _ = page.url
        return False
    except Exception:
        return True


def _recreate_page(context, old_page):
    """
    关闭旧 page（若仍可关）并在给定 context 上新建一个 page。
    """
    if old_page is not None:
        try:
            if not old_page.is_closed():
                old_page.close()
        except Exception:
            pass
    return context.new_page()
