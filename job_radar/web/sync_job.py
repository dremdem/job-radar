"""One background sync at a time for the web UI's Sync button. See docs/queue.md#sync."""

import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

# runner(echo, on_steps): echo gets log lines, on_steps the progress list (pipeline.Step).
Runner = Callable[[Callable[[str], None], Callable[[list[Any]], None]], None]
MAX_LINES = 200


@dataclass
class SyncJob:
    state: str = "idle"  # idle / running / done / failed
    lines: list[str] = field(default_factory=list)
    steps: list[Any] = field(default_factory=list)
    error: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def start(self, runner: Runner) -> bool:
        """Start a sync unless one is running. Returns whether a new one started."""
        with self._lock:
            if self.state == "running":
                return False
            self.state, self.lines, self.steps, self.error = "running", [], [], None
            self.started_at, self.finished_at = datetime.now(UTC), None
        threading.Thread(target=self._run, args=(runner,), daemon=True).start()
        return True

    def _echo(self, line: str) -> None:
        with self._lock:
            self.lines = [*self.lines, line][-MAX_LINES:]

    def _on_steps(self, steps: list[Any]) -> None:
        with self._lock:
            self.steps = steps

    @property
    def failed_steps(self) -> list[Any]:
        return [s for s in self.steps if s.state == "failed"]

    def _run(self, runner: Runner) -> None:
        try:
            runner(self._echo, self._on_steps)
            state, error = "done", None
        except Exception as e:  # shown to the user in the status, the server keeps running
            state, error = "failed", str(e) or e.__class__.__name__
        with self._lock:
            self.state, self.error, self.finished_at = state, error, datetime.now(UTC)
