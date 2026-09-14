"""Task input cache and lightweight checkpoint helpers."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import time
import uuid
import atexit
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from src.core.app_state import get_app_state_root
from src.core.output import get_workspace_root


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _safe_name(value: str) -> str:
    return "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in str(value))[:80] or "task"


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in sorted(value.items(), key=lambda item: str(item[0]))}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(v) for v in value]
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def normalize_lines(value: str | list[str] | tuple[str, ...]) -> list[str]:
    if isinstance(value, str):
        raw_items = value.splitlines()
    else:
        raw_items = list(value or [])
    return [str(item).strip() for item in raw_items if str(item).strip()]


def task_fingerprint(tool_id: str, scope: dict[str, Any]) -> str:
    payload = {"tool_id": tool_id, "scope": _jsonable(scope)}
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


_MIGRATED_CHECKPOINT_PAIRS: set[tuple[str, str]] = set()
_LOCK_STALE_SECONDS = 12 * 60 * 60
_ACTIVE_RUN_TTL_SECONDS = 12 * 60 * 60


def _copy_missing_tree(src: Path, dst: Path) -> None:
    if not src.exists():
        return
    for item in src.rglob("*"):
        rel = item.relative_to(src)
        target = dst / rel
        if item.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        elif not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item, target)


def _same_path(left: Path, right: Path) -> bool:
    try:
        return left.resolve() == right.resolve()
    except OSError:
        return left.absolute() == right.absolute()


def checkpoint_root() -> Path:
    override = os.environ.get("SCRAPER_CHECKPOINT_DIR")
    root = Path(override).expanduser() if override else get_app_state_root() / "checkpoints"
    legacy_root = get_workspace_root() / "output" / "checkpoints"
    if not _same_path(root, legacy_root):
        pair = (str(legacy_root.absolute()).lower(), str(root.absolute()).lower())
        if pair not in _MIGRATED_CHECKPOINT_PAIRS:
            _copy_missing_tree(legacy_root, root)
            _MIGRATED_CHECKPOINT_PAIRS.add(pair)
    root.mkdir(parents=True, exist_ok=True)
    return root


def _atomic_write(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".{os.getpid()}.{uuid.uuid4().hex}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    try:
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def _timestamp() -> float:
    return time.time()


@contextmanager
def _path_lock(path: Path, timeout: float = 10.0):
    lock_dir = path.with_suffix(path.suffix + ".lock")
    deadline = time.monotonic() + max(0.1, float(timeout or 0.1))
    lock_acquired = False
    while not lock_acquired:
        try:
            lock_dir.mkdir(parents=True)
            lock_acquired = True
            try:
                (lock_dir / "owner.json").write_text(
                    json.dumps({"pid": os.getpid(), "created_at": _now()}, ensure_ascii=False),
                    encoding="utf-8",
                )
            except OSError:
                pass
        except FileExistsError:
            try:
                age = _timestamp() - lock_dir.stat().st_mtime
                if age > _LOCK_STALE_SECONDS:
                    shutil.rmtree(lock_dir, ignore_errors=True)
                    continue
            except OSError:
                pass
            if time.monotonic() >= deadline:
                raise TimeoutError(f"Checkpoint lock timeout: {path}")
            time.sleep(0.1)
    try:
        yield
    finally:
        shutil.rmtree(lock_dir, ignore_errors=True)


def save_tool_inputs(tool_id: str, values: dict[str, Any]) -> None:
    if not tool_id:
        return
    path = checkpoint_root() / "last_inputs" / f"{_safe_name(tool_id)}.json"
    _atomic_write(
        path,
        {
            "tool_id": tool_id,
            "updated_at": _now(),
            "values": _jsonable(values),
        },
    )


def load_tool_inputs(tool_id: str) -> dict[str, Any]:
    if not tool_id:
        return {}
    path = checkpoint_root() / "last_inputs" / f"{_safe_name(tool_id)}.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    values = data.get("values", {})
    return values if isinstance(values, dict) else {}


class TaskCheckpoint:
    def __init__(self, tool_id: str, scope: dict[str, Any]):
        self.tool_id = tool_id
        self.scope = _jsonable(scope)
        self.fingerprint = task_fingerprint(tool_id, self.scope)
        self.path = checkpoint_root() / "tasks" / _safe_name(tool_id) / f"{self.fingerprint}.json"
        self.run_id = f"{os.getpid()}-{uuid.uuid4().hex[:10]}"
        self._closed = False
        self.data = self._load()

    def _load(self) -> dict[str, Any]:
        if self.path.exists():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    data.setdefault("revision", 0)
                    data.setdefault("completed", {})
                    return data
            except Exception:
                pass
        return {
            "tool_id": self.tool_id,
            "fingerprint": self.fingerprint,
            "scope": self.scope,
            "revision": 0,
            "created_at": _now(),
            "updated_at": _now(),
            "completed": {},
            "active": {},
            "active_runs": {},
            "output_paths": [],
        }

    def _refresh(self) -> None:
        self.data = self._load()
        self._prune_runtime_state(self.data)

    def _prune_runtime_state(self, data: dict[str, Any]) -> bool:
        changed = False
        now_ts = _timestamp()
        active_runs = data.setdefault("active_runs", {})
        if not isinstance(active_runs, dict):
            active_runs = {}
            data["active_runs"] = active_runs
            changed = True
        for run_id, info in list(active_runs.items()):
            if not isinstance(info, dict):
                active_runs.pop(run_id, None)
                changed = True
                continue
            try:
                updated_ts = float(info.get("updated_ts", 0) or 0)
            except (TypeError, ValueError):
                updated_ts = 0.0
            try:
                lease_expires_ts = float(info.get("lease_expires_ts", 0) or 0)
            except (TypeError, ValueError):
                lease_expires_ts = 0.0
            expired = lease_expires_ts <= now_ts if lease_expires_ts else bool(
                updated_ts and now_ts - updated_ts > _ACTIVE_RUN_TTL_SECONDS
            )
            if expired:
                active_runs.pop(run_id, None)
                changed = True

        active = data.setdefault("active", {})
        if not isinstance(active, dict):
            active = {}
            data["active"] = active
            changed = True
        for key, info in list(active.items()):
            if not isinstance(info, dict):
                active.pop(key, None)
                changed = True
                continue
            run_id = str(info.get("run_id") or "")
            try:
                updated_ts = float(info.get("updated_ts", 0) or 0)
            except (TypeError, ValueError):
                updated_ts = 0.0
            try:
                lease_expires_ts = float(info.get("lease_expires_ts", 0) or 0)
            except (TypeError, ValueError):
                lease_expires_ts = 0.0
            expired = lease_expires_ts <= now_ts if lease_expires_ts else bool(
                updated_ts and now_ts - updated_ts > _ACTIVE_RUN_TTL_SECONDS
            )
            if run_id not in active_runs or expired:
                active.pop(key, None)
                changed = True
        return changed

    def _touch_run(self, data: dict[str, Any], ttl_seconds: float = _ACTIVE_RUN_TTL_SECONDS) -> None:
        now_ts = _timestamp()
        active_runs = data.setdefault("active_runs", {})
        if not isinstance(active_runs, dict):
            active_runs = {}
            data["active_runs"] = active_runs
        existing = active_runs.get(self.run_id)
        started_at = existing.get("started_at", _now()) if isinstance(existing, dict) else _now()
        active_runs[self.run_id] = {
            "pid": os.getpid(),
            "started_at": started_at,
            "updated_at": _now(),
            "updated_ts": now_ts,
            "lease_expires_ts": now_ts + max(1.0, float(ttl_seconds)),
        }

    def _write_locked(self, data: dict[str, Any]) -> None:
        try:
            revision = int(data.get("revision", 0) or 0)
        except (TypeError, ValueError):
            revision = 0
        data["revision"] = revision + 1
        data["updated_at"] = _now()
        _atomic_write(self.path, data)
        self.data = data

    def register_run(self) -> None:
        with _path_lock(self.path):
            data = self._load()
            self._prune_runtime_state(data)
            self._touch_run(data)
            self._write_locked(data)
        atexit.register(self.close_run)

    def heartbeat(self) -> None:
        """Refresh this run lease without changing task completion state."""

        if self._closed:
            return
        with _path_lock(self.path):
            data = self._load()
            self._prune_runtime_state(data)
            self._touch_run(data)
            self._write_locked(data)

    def close_run(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            with _path_lock(self.path, timeout=2.0):
                data = self._load()
                active_runs = data.setdefault("active_runs", {})
                if isinstance(active_runs, dict):
                    active_runs.pop(self.run_id, None)
                active = data.setdefault("active", {})
                if isinstance(active, dict):
                    for key, info in list(active.items()):
                        if isinstance(info, dict) and info.get("run_id") == self.run_id:
                            active.pop(key, None)
                self._write_locked(data)
        except Exception:
            pass

    def delete_if_inactive(self) -> bool:
        """Delete a finished checkpoint only when no other run still owns it."""

        self.close_run()
        with _path_lock(self.path):
            data = self._load()
            self._prune_runtime_state(data)
            active_runs = data.get("active_runs", {})
            if isinstance(active_runs, dict) and active_runs:
                self._write_locked(data)
                return False
            try:
                self.path.unlink(missing_ok=True)
            except OSError:
                return False
            self.data = data
            return True

    def active_other_run_count(self) -> int:
        with _path_lock(self.path):
            data = self._load()
            changed = self._prune_runtime_state(data)
            if changed:
                self._write_locked(data)
            else:
                self.data = data
        active_runs = self.data.get("active_runs", {})
        if not isinstance(active_runs, dict):
            return 0
        return len([run_id for run_id in active_runs if run_id != self.run_id])

    def has_other_active_runs(self) -> bool:
        return self.active_other_run_count() > 0

    def _entry_is_successful(self, entry: Any, positive_count_fields: tuple[str, ...] = ()) -> bool:
        if not isinstance(entry, dict):
            return False
        meta = entry.get("meta", {})
        if positive_count_fields and isinstance(meta, dict):
            for field in positive_count_fields:
                try:
                    if int(meta.get(field, 0) or 0) > 0:
                        return True
                except (TypeError, ValueError):
                    continue
            return False
        if entry.get("status") == "completed":
            return True
        return bool(entry.get("completed_at"))

    @property
    def completed(self) -> dict[str, Any]:
        completed = self.data.setdefault("completed", {})
        return completed if isinstance(completed, dict) else {}

    def completed_count(self) -> int:
        self._refresh()
        return len(self.completed)

    def is_completed(self, key: str) -> bool:
        self._refresh()
        return str(key).strip().lower() in self.completed

    def is_successfully_completed(self, key: str, positive_count_fields: tuple[str, ...] = ()) -> bool:
        self._refresh()
        entry = self.completed.get(str(key).strip().lower())
        return self._entry_is_successful(entry, positive_count_fields=positive_count_fields)

    def claim_item(
        self,
        key: str,
        positive_count_fields: tuple[str, ...] = (),
        ttl_seconds: float | None = None,
    ) -> tuple[bool, str]:
        normalized = str(key).strip().lower()
        if not normalized:
            return False, "empty"
        with _path_lock(self.path):
            data = self._load()
            self._prune_runtime_state(data)
            lease_seconds = _ACTIVE_RUN_TTL_SECONDS if ttl_seconds is None else max(1.0, float(ttl_seconds))
            self._touch_run(data)
            completed = data.setdefault("completed", {})
            if self._entry_is_successful(completed.get(normalized), positive_count_fields=positive_count_fields):
                self._write_locked(data)
                return False, "completed"
            active = data.setdefault("active", {})
            current = active.get(normalized)
            if isinstance(current, dict) and current.get("run_id") != self.run_id:
                self._write_locked(data)
                return False, "active"
            now_ts = _timestamp()
            active[normalized] = {
                "status": "running",
                "run_id": self.run_id,
                "pid": os.getpid(),
                "claimed_at": _now(),
                "updated_at": _now(),
                "updated_ts": now_ts,
                "lease_expires_ts": now_ts + lease_seconds,
            }
            self._write_locked(data)
            return True, "claimed"

    def claim_runtime_item(self, key: str, namespace: str = "runtime", owner_id: str | None = None) -> tuple[bool, str]:
        """Claim a temporary runtime lock that does not affect completed state.

        This is used by in-process parallel browser windows to avoid opening the
        same author/profile page at the same time while still allowing separate
        checkpoint completion keys such as "topic|profile".
        """
        raw_key = str(key).strip().lower()
        if not raw_key:
            return False, "empty"
        normalized = f"runtime:{str(namespace or 'runtime').strip().lower()}:{raw_key}"
        lock_owner = str(owner_id or self.run_id)
        with _path_lock(self.path):
            data = self._load()
            self._prune_runtime_state(data)
            active = data.setdefault("active", {})
            current = active.get(normalized)
            if isinstance(current, dict) and current.get("owner_id") != lock_owner:
                self._write_locked(data)
                return False, "active"
            active[normalized] = {
                "status": "running",
                "run_id": self.run_id,
                "owner_id": lock_owner,
                "pid": os.getpid(),
                "claimed_at": _now(),
                "updated_at": _now(),
                "updated_ts": _timestamp(),
            }
            self._write_locked(data)
            return True, "claimed"

    def release_runtime_item(self, key: str, namespace: str = "runtime", owner_id: str | None = None) -> None:
        raw_key = str(key).strip().lower()
        if not raw_key:
            return
        normalized = f"runtime:{str(namespace or 'runtime').strip().lower()}:{raw_key}"
        lock_owner = str(owner_id or self.run_id)
        with _path_lock(self.path):
            data = self._load()
            active = data.setdefault("active", {})
            current = active.get(normalized)
            if isinstance(current, dict) and current.get("owner_id") == lock_owner:
                active.pop(normalized, None)
            self._write_locked(data)

    def release_item(self, key: str) -> None:
        normalized = str(key).strip().lower()
        if not normalized:
            return
        with _path_lock(self.path):
            data = self._load()
            self._prune_runtime_state(data)
            self._touch_run(data)
            active = data.setdefault("active", {})
            current = active.get(normalized)
            if isinstance(current, dict) and current.get("run_id") == self.run_id:
                active.pop(normalized, None)
            self._write_locked(data)

    def mark_completed(self, key: str, meta: dict[str, Any] | None = None) -> None:
        normalized = str(key).strip().lower()
        if not normalized:
            return
        with _path_lock(self.path):
            data = self._load()
            self._prune_runtime_state(data)
            self._touch_run(data)
            completed = data.setdefault("completed", {})
            completed[normalized] = {
                "status": "completed",
                "completed_at": _now(),
                "meta": _jsonable(meta or {}),
            }
            active = data.setdefault("active", {})
            active.pop(normalized, None)
            self._write_locked(data)

    def get_state(self, key: str, default: Any = None) -> Any:
        if not key:
            return default
        self._refresh()
        return self.data.get(str(key), default)

    def set_state(self, key: str, value: Any) -> None:
        if not key:
            return
        with _path_lock(self.path):
            data = self._load()
            self._prune_runtime_state(data)
            data[str(key)] = _jsonable(value)
            self._write_locked(data)

    def add_output_path(self, output_path: str | None) -> None:
        if not output_path:
            return
        with _path_lock(self.path):
            data = self._load()
            paths = data.setdefault("output_paths", [])
            if not isinstance(paths, list):
                paths = []
                data["output_paths"] = paths
            if output_path not in paths:
                paths.append(output_path)
            self._write_locked(data)

    def latest_output_path(self) -> str | None:
        self._refresh()
        paths = self.data.get("output_paths", [])
        if not isinstance(paths, list):
            return None
        for output_path in reversed(paths):
            if output_path and Path(str(output_path)).exists():
                return str(output_path)
        return None

    def merge_compatible_siblings(self, list_keys: tuple[str, ...], keep_keys: tuple[str, ...] = ()) -> int:
        if not list_keys:
            return 0
        merged_count = 0
        changed = False
        best_completed_count = self.completed_count()
        best_output_paths: list[str] = []
        paths = self.data.setdefault("output_paths", [])
        if not isinstance(paths, list):
            paths = []
            self.data["output_paths"] = paths

        for sibling_path in self.path.parent.glob("*.json"):
            if sibling_path == self.path:
                continue
            try:
                sibling = json.loads(sibling_path.read_text(encoding="utf-8"))
            except Exception:
                continue
            if not isinstance(sibling, dict):
                continue
            if not self._compatible_scope(sibling.get("scope", {}), list_keys, keep_keys):
                continue

            sibling_completed = sibling.get("completed", {})
            if not isinstance(sibling_completed, dict):
                continue
            for key, entry in sibling_completed.items():
                if key not in self.completed:
                    self.completed[key] = entry
                    merged_count += 1
                    changed = True

            sibling_paths = [str(path) for path in sibling.get("output_paths", []) if path]
            for output_path in sibling_paths:
                if output_path not in paths:
                    paths.append(output_path)
                    changed = True
            if len(sibling_completed) > best_completed_count and sibling_paths:
                best_completed_count = len(sibling_completed)
                best_output_paths = sibling_paths

        if best_output_paths:
            ordered_paths = [path for path in paths if path not in best_output_paths] + best_output_paths
            if ordered_paths != self.data.get("output_paths"):
                self.data["output_paths"] = ordered_paths
                changed = True
        if changed:
            self.save()
        return merged_count

    def _compatible_scope(self, other_scope: dict[str, Any], list_keys: tuple[str, ...], keep_keys: tuple[str, ...]) -> bool:
        if not isinstance(other_scope, dict):
            return False
        matched_list = False
        for key in list_keys:
            current_value = self.scope.get(key)
            other_value = other_scope.get(key)
            if isinstance(current_value, list) and isinstance(other_value, list):
                if current_value != other_value:
                    return False
                matched_list = True
        if not matched_list:
            return False
        for key in keep_keys:
            if key in self.scope and key in other_scope and self.scope.get(key) != other_scope.get(key):
                return False
        return True

    def save(self) -> None:
        with _path_lock(self.path):
            latest = self._load()
            self._prune_runtime_state(latest)
            merged = dict(latest)
            merged["tool_id"] = self.tool_id
            merged["fingerprint"] = self.fingerprint
            merged["scope"] = self.scope
            merged.setdefault("created_at", self.data.get("created_at", _now()))
            completed = merged.setdefault("completed", {})
            if isinstance(self.data.get("completed"), dict):
                completed.update(self.data["completed"])
            output_paths = merged.setdefault("output_paths", [])
            if not isinstance(output_paths, list):
                output_paths = []
                merged["output_paths"] = output_paths
            for output_path in self.data.get("output_paths", []) or []:
                if output_path and output_path not in output_paths:
                    output_paths.append(output_path)
            self._write_locked(merged)


def open_task_checkpoint(
    tool_id: str,
    scope: dict[str, Any],
    log_callback=None,
    merge_on_keys: tuple[str, ...] = (),
    merge_keep_keys: tuple[str, ...] = (),
) -> TaskCheckpoint:
    checkpoint = TaskCheckpoint(tool_id, scope)
    checkpoint.register_run()
    merged_count = checkpoint.merge_compatible_siblings(merge_on_keys, merge_keep_keys) if merge_on_keys else 0
    other_runs = checkpoint.active_other_run_count()
    if checkpoint.completed_count():
        try:
            scope_note = _scope_count_note(checkpoint.scope)
            log_callback(
                f"断点续跑：本次输入{scope_note}，已加载 {checkpoint.completed_count()} 条历史断点记录；"
                "只会跳过确认成功的项。"
            )
            if merged_count:
                log_callback(f"断点续跑：已从旧参数任务合并 {merged_count} 条历史记录。")
        except Exception:
            pass
    if other_runs:
        try:
            log_callback(f"断点续跑：检测到同一输入任务还有 {other_runs} 个窗口在运行，本窗口将按输入项自动分流领取。")
        except Exception:
            pass
    return checkpoint


def _scope_count_note(scope: dict[str, Any]) -> str:
    labels = (
        ("profile_urls", "博主链接"),
        ("links", "链接"),
        ("keywords", "关键词"),
    )
    for key, label in labels:
        value = scope.get(key) if isinstance(scope, dict) else None
        if isinstance(value, list):
            return f" {len(value)} 条{label}"
    return ""


def open_checkpointed_row_writer(
    checkpoint: TaskCheckpoint,
    default_output_path: str,
    fieldnames,
    log_callback=None,
    writer_class=None,
    **kwargs,
):
    if writer_class is None:
        from src.core.xlsx import XlsxRowWriter as writer_class

    concurrent = checkpoint.has_other_active_runs()
    resume_path = None if concurrent else checkpoint.latest_output_path()
    if resume_path:
        try:
            writer = writer_class(resume_path, fieldnames, append=True, **kwargs)
            _log_resume_output(log_callback, resume_path)
            return resume_path, writer
        except Exception as exc:
            _log_new_output(log_callback, exc)
    output_path = _output_path_for_current_run(default_output_path, checkpoint, force_run_suffix=concurrent)
    if concurrent:
        _log_concurrent_output(log_callback, output_path)
    writer = writer_class(output_path, fieldnames, **kwargs)
    return output_path, writer


def open_checkpointed_multi_sheet_writer(
    checkpoint: TaskCheckpoint,
    default_output_path: str,
    sheets_fields,
    log_callback=None,
    **kwargs,
):
    from src.core.xlsx import MultiSheetXlsxWriter

    concurrent = checkpoint.has_other_active_runs()
    resume_path = None if concurrent else checkpoint.latest_output_path()
    if resume_path:
        try:
            writer = MultiSheetXlsxWriter(resume_path, sheets_fields, append=True, **kwargs)
            _log_resume_output(log_callback, resume_path)
            return resume_path, writer
        except Exception as exc:
            _log_new_output(log_callback, exc)
    output_path = _output_path_for_current_run(default_output_path, checkpoint, force_run_suffix=concurrent)
    if concurrent:
        _log_concurrent_output(log_callback, output_path)
    writer = MultiSheetXlsxWriter(output_path, sheets_fields, **kwargs)
    return output_path, writer


def _output_path_for_current_run(default_output_path: str, checkpoint: TaskCheckpoint, force_run_suffix: bool = False) -> str:
    path = Path(default_output_path)
    if force_run_suffix:
        path = path.with_name(f"{path.stem}_run_{_safe_name(checkpoint.run_id)[-8:]}{path.suffix}")
    if not path.exists():
        return str(path)
    for index in range(2, 1000):
        candidate = path.with_name(f"{path.stem}_{index}{path.suffix}")
        if not candidate.exists():
            return str(candidate)
    return str(path.with_name(f"{path.stem}_{uuid.uuid4().hex[:8]}{path.suffix}"))


def _log_resume_output(log_callback, output_path: str) -> None:
    try:
        log_callback(f"断点续跑：继续写入上次输出文件：{output_path}")
    except Exception:
        pass


def _log_new_output(log_callback, exc: Exception) -> None:
    try:
        log_callback(f"断点续跑：上次输出文件无法追加，将新建输出文件。原因：{exc}")
    except Exception:
        pass


def _log_concurrent_output(log_callback, output_path: str) -> None:
    try:
        log_callback(f"断点续跑：双开分流模式，本窗口写入独立输出文件：{output_path}")
    except Exception:
        pass
