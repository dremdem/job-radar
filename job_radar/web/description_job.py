"""One background LinkedIn description job at a time, for the Descriptions button and Sync.
See docs/linkedin-public-search.md#the-descriptions-button-205."""

import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

MAX_LINES = 50


@dataclass
class DescriptionJob:
    state: str = "idle"  # idle / running / done / failed
    total: int = 0
    requests: int = 0
    saved: int = 0
    errors: int = 0
    next_at: datetime | None = None  # the next request, while pausing
    blocked: bool = False
    reason: str | None = None
    stopped: bool = False
    limit_reached: bool = False
    error: str | None = None
    lines: list[str] = field(default_factory=list)
    started_at: datetime | None = None
    finished_at: datetime | None = None
    blocked_note: str = "LinkedIn blocked ({reason}), stopped until tomorrow"
    stop_event: threading.Event = field(default_factory=threading.Event, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def start(self, runner: Callable[["DescriptionJob"], None]) -> bool:
        """Start unless one is running. Returns whether a new one started."""
        with self._lock:
            if self.state == "running":
                return False
            fresh = DescriptionJob(state="running", started_at=datetime.now(UTC))
            for name in ("state", "total", "requests", "saved", "errors", "next_at", "blocked",
                         "reason", "stopped", "limit_reached", "error", "lines", "started_at",
                         "finished_at"):  # fmt: skip
                setattr(self, name, getattr(fresh, name))
            self.stop_event = threading.Event()
        threading.Thread(target=self._run, args=(runner,), daemon=True).start()
        return True

    def stop(self) -> None:
        self.stop_event.set()

    # Called by the runner (pipeline.description_job).
    def progress(self, *, total: int, requests: int, saved: int, errors: int) -> None:
        with self._lock:
            self.total, self.requests, self.saved, self.errors = total, requests, saved, errors
            self.next_at = None

    def waiting(self, seconds: float) -> None:
        with self._lock:
            self.next_at = datetime.now(UTC) + timedelta(seconds=seconds)

    def finish(
        self,
        *,
        blocked: bool = False,
        reason: str | None = None,
        stopped: bool = False,
        limit_reached: bool = False,
    ) -> None:
        with self._lock:
            self.blocked, self.reason = blocked, reason
            self.stopped, self.limit_reached = stopped, limit_reached
            self.next_at = None

    def echo(self, line: str) -> None:
        with self._lock:
            self.lines = [*self.lines, line][-MAX_LINES:]

    @property
    def next_in(self) -> int | None:
        if self.next_at is None:
            return None
        return max(0, round((self.next_at - datetime.now(UTC)).total_seconds()))

    @property
    def outcome(self) -> str:
        """Why it ended, in a few words."""
        if self.state == "failed":
            return f"failed: {self.error}"
        if self.blocked:
            return self.blocked_note.format(reason=self.reason)
        if self.stopped:
            return "stopped by you"
        if self.total == 0 and not self.limit_reached:
            return "nothing to fetch"
        if self.limit_reached:
            return "daily limit reached"
        return "done"

    def _run(self, runner: Callable[["DescriptionJob"], None]) -> None:
        try:
            runner(self)
            state, error = "done", None
        except Exception as e:  # shown in the status; the server keeps running
            state, error = "failed", str(e) or e.__class__.__name__
        with self._lock:
            self.state, self.error, self.finished_at = state, error, datetime.now(UTC)
            self.next_at = None
