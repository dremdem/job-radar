"""One background description job at a time, for the Descriptions button and Sync.

``DescriptionJob`` runs ``pipeline.description_job`` (LinkedIn's public pages, then FDE
Times) in a thread, keeps its progress for the status poll and stops it on request. See
docs/linkedin-public-search.md#the-descriptions-button-205.

Usage:
    import job_radar.jobs.description_job as jobs_description_job
"""

import collections.abc as cabc
import dataclasses
import datetime as dt
import threading

MAX_LINES = 50


@dataclasses.dataclass
class DescriptionJob:
    """The one background description job of the web UI and its progress."""

    # idle / running / done / failed
    state: str = "idle"
    total: int = 0
    requests: int = 0
    saved: int = 0
    errors: int = 0
    # per site, Chrome fill only (#272)
    sources: list[dict] = dataclasses.field(default_factory=list)
    # vacancies with a problem (#274)
    failed: list[dict] = dataclasses.field(default_factory=list)
    # the next request, while pausing
    next_at: dt.datetime | None = None
    blocked: bool = False
    reason: str | None = None
    stopped: bool = False
    limit_reached: bool = False
    error: str | None = None
    lines: list[str] = dataclasses.field(default_factory=list)
    started_at: dt.datetime | None = None
    finished_at: dt.datetime | None = None
    blocked_note: str = "LinkedIn blocked ({reason}), stopped until tomorrow"
    stop_event: threading.Event = dataclasses.field(default_factory=threading.Event, repr=False)
    _lock: threading.Lock = dataclasses.field(default_factory=threading.Lock, repr=False)

    def start(self, runner: cabc.Callable[["DescriptionJob"], None]) -> bool:
        """Start unless one is running. Returns whether a new one started."""
        with self._lock:
            if self.state == "running":
                return False
            fresh = DescriptionJob(state="running", started_at=dt.datetime.now(dt.UTC))
            for name in ("state", "total", "requests", "saved", "errors", "sources", "failed",
                         "next_at", "blocked", "reason", "stopped", "limit_reached", "error",
                         "lines", "started_at", "finished_at"):  # fmt: skip
                setattr(self, name, getattr(fresh, name))
            self.stop_event = threading.Event()
        threading.Thread(target=self._run, args=(runner,), daemon=True).start()
        return True

    def stop(self) -> None:
        self.stop_event.set()

    # Called by the runner (pipeline.description_job).
    def progress(
        self,
        *,
        total: int,
        requests: int,
        saved: int,
        errors: int,
        sources: list[dict] | None = None,
        failed: list[dict] | None = None,
    ) -> None:
        with self._lock:
            self.total, self.requests, self.saved, self.errors = total, requests, saved, errors
            if sources is not None:
                self.sources = sources
            if failed is not None:
                self.failed = failed
            self.next_at = None

    def waiting(self, seconds: float) -> None:
        with self._lock:
            self.next_at = dt.datetime.now(dt.UTC) + dt.timedelta(seconds=seconds)

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
        return max(0, round((self.next_at - dt.datetime.now(dt.UTC)).total_seconds()))

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

    def _run(self, runner: cabc.Callable[["DescriptionJob"], None]) -> None:
        try:
            runner(self)
            state, error = "done", None
        except Exception as e:
            # shown in the status; the server keeps running
            state, error = "failed", str(e) or e.__class__.__name__
        with self._lock:
            self.state, self.error, self.finished_at = state, error, dt.datetime.now(dt.UTC)
            self.next_at = None
