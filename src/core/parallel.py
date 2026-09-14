"""Small helpers for browser-window parallelism.

The scrapers use Playwright's sync API, so each worker owns its own Playwright
connection and pages. Shared state is limited to checkpoint claims and writers.
"""

from __future__ import annotations

import threading
from typing import Any


def normalize_parallel_windows(
    config: dict[str, Any] | None,
    *,
    key: str = "parallel_windows",
    default: int = 1,
    maximum: int = 4,
) -> int:
    try:
        value = int((config or {}).get(key, default) or default)
    except (TypeError, ValueError):
        value = default
    return max(1, min(maximum, value))


class AtomicCounter:
    def __init__(self, initial: int = 0):
        self._value = int(initial or 0)
        self._lock = threading.Lock()

    def next(self) -> int:
        with self._lock:
            self._value += 1
            return self._value

    @property
    def value(self) -> int:
        with self._lock:
            return self._value


class ThreadSafeWriter:
    """Serialize writes/saves to a workbook writer shared by worker threads."""

    def __init__(self, writer):
        self._writer = writer
        self._lock = threading.RLock()

    @property
    def raw(self):
        return self._writer

    def writerow(self, *args, **kwargs):
        with self._lock:
            return self._writer.writerow(*args, **kwargs)

    def writerows(self, *args, **kwargs):
        with self._lock:
            return self._writer.writerows(*args, **kwargs)

    def save(self):
        with self._lock:
            return self._writer.save()

    def __getattr__(self, name: str):
        return getattr(self._writer, name)
