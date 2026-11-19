"""Single-flight background jobs for the web app.

Sync, scan and track are long (seconds to minutes) and must never block a
request thread. One job runs at a time; the UI polls ``/partials/job``.
"""

from __future__ import annotations

import logging
import threading
import traceback
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any

log = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now().strftime("%H:%M:%S")


@dataclass
class JobState:
    name: str = ""
    title: str = ""
    status: str = "idle"          # idle | running | done | error
    message: str = ""
    started_at: str | None = None
    finished_at: str | None = None
    result: dict[str, Any] = field(default_factory=dict)
    progress: dict[str, Any] = field(default_factory=dict)

    @property
    def running(self) -> bool:
        return self.status == "running"

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class JobManager:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self.state = JobState()

    def start(self, name: str, title: str, fn: Callable[[], dict | None]) -> bool:
        """Run ``fn`` in a daemon thread. Returns False if one is running."""
        with self._lock:
            if self.state.running and self._thread and self._thread.is_alive():
                return False
            self.state = JobState(name=name, title=title, status="running",
                                  message="working", started_at=_now())
            self._thread = threading.Thread(
                target=self._run, args=(fn,), name=f"sta-{name}", daemon=True)
            self._thread.start()
            return True

    def report(self, phase: str, current: int, total: int) -> None:
        """Update the progress bar from inside a job (thread-safe, cheap)."""
        total = max(int(total), 0)
        current = max(0, min(int(current), total)) if total else int(current)
        self.state.progress = {
            "phase": phase,
            "current": current,
            "total": total,
            "pct": round(100 * current / total) if total else 0,
        }

    def _run(self, fn: Callable[[], dict | None]) -> None:
        try:
            result = fn() or {}
            self.state.result = result
            self.state.status = "done"
            self.state.message = "finished"
            self.state.progress = {"phase": "done", "current": 100,
                                   "total": 100, "pct": 100}
        except Exception as exc:  # noqa: BLE001 — surfaced in the UI
            log.exception("background job failed")
            self.state.status = "error"
            self.state.message = f"{exc.__class__.__name__}: {exc}"
            self.state.result = {"traceback": traceback.format_exc(limit=4)}
        finally:
            self.state.finished_at = _now()

    def snapshot(self) -> dict[str, Any]:
        return self.state.as_dict()

    @property
    def running(self) -> bool:
        return self.state.running and self._thread is not None \
            and self._thread.is_alive()
