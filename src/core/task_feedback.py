"""Thread-safe structured progress and activity reporting for long-running tasks."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Mapping
from typing import Any


FeedbackCallback = Callable[[dict[str, Any]], None]


class TaskFeedback:
    """Hold progress and the current safe-stop wait reason for a running task."""

    def __init__(self, on_change: FeedbackCallback | None = None) -> None:
        self._lock = threading.Lock()
        self._on_change = on_change
        self._completed = 0
        self._total = 0
        self._phase = ""
        self._activity = ""
        self._stop_reason = ""
        self._updated_at = time.monotonic()

    def _snapshot_locked(self) -> dict[str, Any]:
        total = self._total
        completed = self._completed
        percent = min(100.0, completed * 100.0 / total) if total > 0 else 0.0
        return {
            "completed": completed,
            "total": total,
            "percent": percent,
            "phase": self._phase,
            "activity": self._activity,
            "stop_reason": self._stop_reason,
            "updated_at": self._updated_at,
        }

    def _notify(self, snapshot: dict[str, Any]) -> None:
        callback = self._on_change
        if callback is None:
            return
        try:
            callback(snapshot)
        except Exception:
            # Feedback must never make the collection task fail.
            pass

    def _update(
        self,
        *,
        completed: int | None = None,
        total: int | None = None,
        phase: str | None = None,
        activity: str | None = None,
        stop_reason: str | None = None,
        advance: int = 0,
    ) -> dict[str, Any]:
        with self._lock:
            if total is not None:
                self._total = max(0, int(total))
            if completed is not None:
                self._completed = max(0, int(completed))
            if advance:
                self._completed = max(0, self._completed + int(advance))
            if self._total > 0:
                self._completed = min(self._completed, self._total)
            if phase is not None:
                self._phase = str(phase)
            if activity is not None:
                self._activity = str(activity)
            if stop_reason is not None:
                self._stop_reason = str(stop_reason)
            self._updated_at = time.monotonic()
            snapshot = self._snapshot_locked()
        self._notify(snapshot)
        return snapshot

    def reset(
        self,
        *,
        total: int = 0,
        phase: str = "",
        activity: str = "",
        stop_reason: str = "",
    ) -> dict[str, Any]:
        return self._update(
            completed=0,
            total=total,
            phase=phase,
            activity=activity,
            stop_reason=stop_reason,
        )

    def set_total(
        self,
        total: int,
        *,
        completed: int = 0,
        phase: str | None = None,
    ) -> dict[str, Any]:
        return self._update(total=total, completed=completed, phase=phase)

    def set_progress(
        self,
        completed: int,
        *,
        total: int | None = None,
        phase: str | None = None,
        activity: str | None = None,
        stop_reason: str | None = None,
    ) -> dict[str, Any]:
        return self._update(
            completed=completed,
            total=total,
            phase=phase,
            activity=activity,
            stop_reason=stop_reason,
        )

    def advance(
        self,
        delta: int = 1,
        *,
        phase: str | None = None,
        activity: str | None = None,
        stop_reason: str | None = None,
    ) -> dict[str, Any]:
        return self._update(
            advance=delta,
            phase=phase,
            activity=activity,
            stop_reason=stop_reason,
        )

    def set_activity(
        self,
        activity: str,
        *,
        phase: str | None = None,
        stop_reason: str | None = None,
    ) -> dict[str, Any]:
        return self._update(
            phase=phase,
            activity=activity,
            stop_reason=stop_reason,
        )

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return self._snapshot_locked()


def feedback_from_config(config: Mapping[str, Any] | None) -> TaskFeedback | None:
    """Return the runtime feedback channel without coupling it to saved config."""

    if not config:
        return None
    feedback = config.get("_task_feedback")
    return feedback if isinstance(feedback, TaskFeedback) else None
