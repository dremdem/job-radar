"""The queue of deep analyses and the workers that run them. See docs/claude.md."""

import re
import threading
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session, sessionmaker

from ..ai.analysis import quote_in_text
from ..description import latest_description
from ..models import AppSetting, ClaudeAnalysis, ClaudeMessage, Opportunity
from ..stack import current_profile
from .prompt import PROMPT_VERSION, SCHEMA, analysis_prompt, chat_system_prompt, system_prompt
from .runner import ClaudeError, Runner

SETTINGS_KEY = "claude"
MODELS = ("sonnet", "opus", "haiku")
LANGUAGES = ("Russian", "English")
PENDING = ("queued", "running")


MAX_PARALLEL = 8
USAGE_PAUSE = timedelta(minutes=15)
# The subscription's limit: waiting a few seconds will not help (#197).
_USAGE_LIMIT = re.compile(r"usage limit|rate.?limit|\b429\b|too many requests", re.IGNORECASE)


@dataclass(frozen=True)
class ClaudeSettings:
    model: str = "sonnet"
    daily_cap: int = 30
    language: str = "Russian"
    parallel: int = 4  # analyses at once, 1..MAX_PARALLEL (#197)


def load_claude_settings(session: Session) -> ClaudeSettings:
    row = session.get(AppSetting, SETTINGS_KEY)
    if row is None or not isinstance(row.value, dict):
        return ClaudeSettings()
    known = {k: v for k, v in row.value.items() if k in ClaudeSettings.__dataclass_fields__}
    return ClaudeSettings(**known)


def save_claude_settings(session: Session, value: ClaudeSettings) -> None:
    row = session.get(AppSetting, SETTINGS_KEY) or AppSetting(key=SETTINGS_KEY)
    row.value = asdict(value)
    session.add(row)
    session.commit()


def latest(session: Session, opportunity_id: int) -> ClaudeAnalysis | None:
    return session.scalar(
        select(ClaudeAnalysis)
        .where(ClaudeAnalysis.opportunity_id == opportunity_id)
        .order_by(ClaudeAnalysis.id.desc())
        .limit(1)
    )


def enqueue(session: Session, opportunity_ids: list[int]) -> int:
    """Queue the vacancies that are not queued or running already and have a description
    (there is nothing to analyze without one, #146). Returns how many were queued."""
    model = load_claude_settings(session).model
    added = 0
    for opportunity_id in dict.fromkeys(opportunity_ids):
        opportunity = session.get(Opportunity, opportunity_id)
        if opportunity is None or not latest_description(opportunity):
            continue
        current = latest(session, opportunity_id)
        if current is not None and current.state in PENDING:
            continue
        session.add(
            ClaudeAnalysis(
                opportunity_id=opportunity_id, model=model, prompt_version=PROMPT_VERSION
            )
        )
        added += 1
    session.commit()
    return added


def without_description(session: Session, opportunity_ids: list[int]) -> int:
    """How many of these vacancies have no description (and so are not queued)."""
    found = (session.get(Opportunity, i) for i in dict.fromkeys(opportunity_ids))
    return sum(1 for o in found if o is not None and not latest_description(o))


def latest_done(session: Session, opportunity_id: int) -> ClaudeAnalysis | None:
    return session.scalar(
        select(ClaudeAnalysis)
        .where(
            ClaudeAnalysis.opportunity_id == opportunity_id,
            ClaudeAnalysis.state == "done",
            ClaudeAnalysis.session_id.is_not(None),
        )
        .order_by(ClaudeAnalysis.id.desc())
        .limit(1)
    )


def latest_matches(session: Session) -> dict[int, int]:
    """Claude's match % per vacancy, from its latest finished analysis (#207)."""
    rows = session.execute(
        select(ClaudeAnalysis.opportunity_id, ClaudeAnalysis.result)
        .where(ClaudeAnalysis.state == "done", ClaudeAnalysis.result.is_not(None))
        .order_by(ClaudeAnalysis.id)
    )
    matches: dict[int, int] = {}
    for opportunity_id, result in rows:
        match = (result or {}).get("match")
        if isinstance(match, int | float):
            matches[opportunity_id] = int(match)
    return matches


def messages(session: Session, opportunity_id: int) -> list[ClaudeMessage]:
    return list(
        session.scalars(
            select(ClaudeMessage)
            .where(ClaudeMessage.opportunity_id == opportunity_id)
            .order_by(ClaudeMessage.id)
        )
    )


def ask(session: Session, opportunity_id: int, text: str) -> ClaudeMessage | None:
    """Queue a chat question; None when there is no finished analysis to continue."""
    analysis = latest_done(session, opportunity_id)
    if analysis is None or not text.strip():
        return None
    message = ClaudeMessage(
        opportunity_id=opportunity_id,
        analysis_id=analysis.id,
        role="user",
        text=text.strip(),
        state="queued",
    )
    session.add(message)
    session.commit()
    return message


def chat_pending(session: Session, opportunity_id: int) -> bool:
    return bool(
        session.scalar(
            select(ClaudeMessage.id).where(
                ClaudeMessage.opportunity_id == opportunity_id,
                ClaudeMessage.state.in_(PENDING),
            )
        )
    )


@dataclass(frozen=True)
class QueueStatus:
    queued: int
    running: int
    done_today: int
    daily_cap: int
    recent: list[int]  # vacancies finished in the last minutes, for live row updates

    @property
    def pending(self) -> int:
        return self.queued + self.running

    @property
    def capped(self) -> bool:
        return self.queued > 0 and self.done_today >= self.daily_cap


def _today(now: datetime) -> datetime:
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


def _done_today(session: Session, now: datetime) -> int:
    return session.scalar(
        select(func.count(ClaudeAnalysis.id)).where(
            ClaudeAnalysis.state.in_(("done", "failed")),
            ClaudeAnalysis.started_at >= _today(now),
        )
    )


def status(session: Session, now: datetime | None = None) -> QueueStatus:
    now = now or datetime.now(UTC)
    counts = dict(
        session.execute(
            select(ClaudeAnalysis.state, func.count(ClaudeAnalysis.id))
            .where(ClaudeAnalysis.state.in_(PENDING))
            .group_by(ClaudeAnalysis.state)
        ).all()
    )
    recent = session.scalars(
        select(ClaudeAnalysis.opportunity_id).where(
            ClaudeAnalysis.state.in_(("done", "failed")),
            ClaudeAnalysis.finished_at >= now - timedelta(minutes=10),
        )
    ).all()
    return QueueStatus(
        queued=counts.get("queued", 0),
        running=counts.get("running", 0),
        done_today=_done_today(session, now),
        daily_cap=load_claude_settings(session).daily_cap,
        recent=list(recent),
    )


def _check_quotes(result: dict[str, Any], text: str) -> dict[str, Any]:
    """Mark every quote as found in the description or not (`quote_ok`)."""

    def mark(item: dict[str, Any]) -> None:
        quote = str(item.get("quote") or "")
        item["quote_ok"] = bool(quote) and quote_in_text(quote, text)

    if isinstance(result.get("location"), dict):
        mark(result["location"])
    for key in ("strengths", "gaps", "red_flags"):
        for item in result.get(key) or []:
            if isinstance(item, dict):
                mark(item)
    return result


class Worker:
    """Runs queued analyses in a pool of background threads (#197) and answers chat
    questions in a thread of its own, so they never wait behind analyses."""

    def __init__(
        self,
        session_factory: sessionmaker,
        runner: Runner,
        *,
        profile_path: Path,
        cv_path: Path | None,
        idle_wait: float = 5.0,
    ):
        self.session_factory = session_factory
        self.runner = runner
        self.profile_path = profile_path
        self.cv_path = cv_path
        self.idle_wait = idle_wait
        self.retry_waits: tuple[float, ...] = (10.0, 30.0)  # pauses before attempts 2 and 3
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._claim = threading.Lock()  # one thread at a time picks the next vacancy
        self.paused_until: datetime | None = None  # after the subscription's usage limit

    def start(self) -> None:
        with self.session_factory() as session:
            # A restart interrupted these: run them again.
            for row in session.scalars(
                select(ClaudeAnalysis).where(ClaudeAnalysis.state == "running")
            ):
                # A new session id: claude refuses one it has already seen.
                row.state, row.started_at, row.session_id = "queued", None, None
            for message in session.scalars(
                select(ClaudeMessage).where(ClaudeMessage.state == "running")
            ):
                message.state = "queued"
            session.commit()
        loops = [("claude-chat", self._chat_loop)] + [
            (f"claude-worker-{n}", lambda n=n: self._analysis_loop(n)) for n in range(MAX_PARALLEL)
        ]
        for name, target in loops:
            thread = threading.Thread(target=target, daemon=True, name=name)
            thread.start()
            self._threads.append(thread)

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def wake(self) -> None:
        self._wake.set()

    def _idle(self) -> None:
        # Every thread sleeps until woken; the event stays set briefly so all of them see it.
        if self._wake.wait(self.idle_wait):
            time.sleep(0.05)
            self._wake.clear()

    def _chat_loop(self) -> None:
        while not self._stop.is_set():
            try:
                worked = self._answer_once()
            except Exception:  # never kill the thread; the row says what failed
                worked = False
            if not worked:
                self._idle()

    def _analysis_loop(self, slot: int) -> None:
        while not self._stop.is_set():
            try:
                with self.session_factory() as session:
                    parallel = load_claude_settings(session).parallel
                active = slot < max(1, min(MAX_PARALLEL, parallel))
                worked = active and self._analyze_once()
            except Exception:  # never kill the thread; the row says what failed
                worked = False
            if not worked:
                self._idle()

    def _cv(self) -> str | None:
        if self.cv_path and self.cv_path.expanduser().exists():
            return self.cv_path.expanduser().read_text()
        return None

    def run_once(self, now: datetime | None = None) -> bool:
        """Answer the oldest chat question, else run the oldest queued analysis.

        Returns False when there was nothing to run. Tests drive the worker with it."""
        return self._answer_once() or self._analyze_once(now)

    def _claim_next(self, session: Session, now: datetime) -> ClaudeAnalysis | None:
        """Mark the oldest queued vacancy as running, unless capped or paused."""
        with self._claim:
            if self.paused_until is not None and now < self.paused_until:
                return None
            settings = load_claude_settings(session)
            running = session.scalar(
                select(func.count(ClaudeAnalysis.id)).where(ClaudeAnalysis.state == "running")
            )
            if _done_today(session, now) + running >= settings.daily_cap:
                return None
            job_id = session.scalar(
                select(ClaudeAnalysis.id)
                .where(ClaudeAnalysis.state == "queued")
                .order_by(ClaudeAnalysis.id)
                .limit(1)
            )
            if job_id is None:
                return None
            taken = session.execute(
                update(ClaudeAnalysis)
                .where(ClaudeAnalysis.id == job_id, ClaudeAnalysis.state == "queued")
                .values(state="running", started_at=now)
            )
            session.commit()
            if taken.rowcount != 1:
                return None
            job = session.get(ClaudeAnalysis, job_id)
            session.refresh(job)
            job.session_id = job.session_id or str(uuid.uuid4())
            session.commit()
            return job

    def _analyze_once(self, now: datetime | None = None) -> bool:
        with self.session_factory() as session:
            now = now or datetime.now(UTC)
            job = self._claim_next(session, now)
            if job is None:
                return False
            settings = load_claude_settings(session)

            o = job.opportunity
            description = latest_description(o)
            if not description:
                job.state, job.error = "failed", "no description saved for this vacancy"
                job.finished_at = datetime.now(UTC)
                session.commit()
                return True
            prompt = analysis_prompt(
                cv=self._cv(),
                profile=current_profile(self.profile_path),
                title=o.title,
                company=o.company.name,
                location=o.location,
                salary=o.salary,
                description=description,
            )
            reply, error = None, None
            for attempt in range(1 + len(self.retry_waits)):
                if attempt:
                    time.sleep(self.retry_waits[attempt - 1])
                    job.session_id = str(uuid.uuid4())  # claude refuses a reused session id
                job.attempts = attempt + 1
                session.commit()
                try:
                    reply = self.runner.run(
                        prompt,
                        system=system_prompt(settings.language),
                        model=job.model,
                        schema=SCHEMA,
                        session_id=job.session_id,
                    )
                    if not isinstance(reply.structured, dict):
                        raise ClaudeError("the answer has no structured result", transient=True)
                    error = None
                    break
                except Exception as e:  # transient ones are retried; the rest shown on the row
                    error = e
                    if _USAGE_LIMIT.search(str(e)):
                        # The subscription's limit: back to the queue, pause new ones (#197).
                        self.paused_until = datetime.now(UTC) + USAGE_PAUSE
                        until = self.paused_until.astimezone().strftime("%H:%M")
                        job.state, job.started_at, job.session_id = "queued", None, None
                        job.error = f"{e} · paused until {until}, then retried"
                        session.commit()
                        return True
                    if not getattr(e, "transient", False):
                        break
            if error is not None:
                job.state, job.error = "failed", str(error) or error.__class__.__name__
            else:
                job.state, job.error = "done", None
                job.result = _check_quotes(dict(reply.structured), description)
                job.session_id = reply.session_id or job.session_id
                job.cost_usd = reply.cost_usd
            job.finished_at = datetime.now(UTC)
            session.commit()
            return True

    def _answer_once(self) -> bool:
        """Chat questions go first: someone is waiting on the page. Not capped."""
        with self.session_factory() as session:
            question = session.scalar(
                select(ClaudeMessage)
                .where(ClaudeMessage.state == "queued", ClaudeMessage.role == "user")
                .order_by(ClaudeMessage.id)
                .limit(1)
            )
            if question is None:
                return False
            question.state = "running"
            session.commit()
            analysis = question.analysis
            settings = load_claude_settings(session)
            try:
                reply = self.runner.run(
                    question.text,
                    system=chat_system_prompt(settings.language),
                    model=analysis.model,
                    resume=analysis.session_id,
                )
            except Exception as e:  # shown under the question; ask again to retry
                question.state, question.error = "failed", str(e) or e.__class__.__name__
            else:
                question.state, question.error = "done", None
                session.add(
                    ClaudeMessage(
                        opportunity_id=question.opportunity_id,
                        analysis_id=analysis.id,
                        role="assistant",
                        text=reply.text,
                    )
                )
            session.commit()
            return True
