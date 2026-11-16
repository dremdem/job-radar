"""One background sync at a time for the web UI's Sync button.

``SyncJob`` runs the sync (``pipeline.run_sync``) in a thread, collects its lines and step states
for the status poll, and refuses a second run while one is going. See docs/queue.md#sync.

Usage:
    import job_radar.web.sync_job as web_sync_job
"""

import collections.abc as cabc
import dataclasses
import datetime as dt
import threading
import typing as t

# runner(echo, on_steps): echo gets log lines, on_steps the progress list (pipeline.Step).
Runner = cabc.Callable[[cabc.Callable[[str], None], cabc.Callable[[list[t.Any]], None]], None]
MAX_LINES = 200


@dataclasses.dataclass
class SyncJob:
    """The one background sync of the web UI, its lines and step states."""

    # idle / running / done / failed
    state: str = "idle"
    lines: list[str] = dataclasses.field(default_factory=list)
    steps: list[t.Any] = dataclasses.field(default_factory=list)
    error: str | None = None
    started_at: dt.datetime | None = None
    finished_at: dt.datetime | None = None
    _lock: threading.Lock = dataclasses.field(default_factory=threading.Lock, repr=False)

    def start(self, runner: Runner) -> bool:
        """Start a sync unless one is running. Returns whether a new one started."""
        with self._lock:
            if self.state == "running":
                return False
            self.state, self.lines, self.steps, self.error = "running", [], [], None
            self.started_at, self.finished_at = dt.datetime.now(dt.UTC), None
        threading.Thread(target=self._run, args=(runner,), daemon=True).start()
        return True

    def _echo(self, line: str) -> None:
        with self._lock:
            self.lines = [*self.lines, line][-MAX_LINES:]

    def _on_steps(self, steps: list[t.Any]) -> None:
        with self._lock:
            self.steps = steps

    @property
    def failed_steps(self) -> list[t.Any]:
        return [s for s in self.steps if s.state == "failed"]

    def _run(self, runner: Runner) -> None:
        try:
            runner(self._echo, self._on_steps)
            state, error = "done", None
        except Exception as e:
            # shown to the user in the status, the server keeps running
            state, error = "failed", str(e) or e.__class__.__name__
        with self._lock:
            self.state, self.error, self.finished_at = state, error, dt.datetime.now(dt.UTC)
