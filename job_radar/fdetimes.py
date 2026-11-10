"""Forward Deployed Engineer roles from FDE Times' public job list. See docs/fdetimes.md."""

import json
import random
import re
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Protocol

from bs4 import BeautifulSoup
from sqlalchemy import select
from sqlalchemy.orm import Session

from .dedup import add_snapshot, get_or_create_company
from .himalayas import EXCLUDE_TITLE, USER_AGENT, _text
from .models import Opportunity, OpportunityState, SourceRecord, SourceType, SyncCursor
from .scoring import score_job
from .telegram.sources import DEFAULT_TITLE

BASE_URL = "https://fdetimes.net"
LIST_URL = f"{BASE_URL}/en/jobs/"
JOB_URL = f"{BASE_URL}/en/jobs/{{slug}}/"
CURSOR = "fdetimes"
REGIONS = ("north-america", "europe", "apac", "remote", "other")
# The time is when the company posted the role (some carry only a date), and a role can
# reach the board days later: re-read a week before the last run. One request either way.
MARGIN = timedelta(days=7)
GONE = (404, 410)  # the role was taken down: the vacancy is not available
BLOCKED = (403, 429)  # stop asking for this run
_SLUG = re.compile(r"^/en/jobs/([a-z0-9][a-z0-9-]*)/?$")

# (http status, body); a non-200 status is returned, not raised.
HttpGet = Callable[[str], tuple[int, str]]


class FDETimesError(Exception):
    pass


def urllib_get(url: str) -> tuple[int, str]:
    # A fresh request every time: no cookies, no session.
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, ""
    except (urllib.error.URLError, TimeoutError) as e:
        raise FDETimesError(str(getattr(e, "reason", e))) from e


class Client(Protocol):
    def jobs_page(self) -> str: ...


class FDETimesClient:
    def __init__(self, http_get: HttpGet = urllib_get):
        self._get = http_get

    def jobs_page(self) -> str:
        status, body = self._get(LIST_URL)
        if status != 200:
            raise FDETimesError(f"HTTP {status}")
        return body


@dataclass(frozen=True)
class FDEJob:
    slug: str
    title: str
    company: str
    location: str
    region: str
    kind: str  # "fde" or "related"
    posted_at: datetime

    @property
    def url(self) -> str:
        return JOB_URL.format(slug=self.slug)


def parse_jobs(html: str) -> list[FDEJob]:
    """Every role on the list page; rows without a link or a date are left out."""
    jobs = []
    for row in BeautifulSoup(html, "html.parser").select("div.job[data-kind]"):
        link = row.select_one("a.t[href]")
        match = _SLUG.match(link["href"]) if link else None
        try:
            posted = datetime.fromisoformat(row.get("data-posted", "")).astimezone(UTC)
        except ValueError:
            continue
        if not match:
            continue
        meta = row.select_one(".m")
        company, _, location = (meta.get_text(" ", strip=True) if meta else "").partition(" · ")
        jobs.append(
            FDEJob(
                slug=match.group(1),
                title=" ".join(link.get_text(" ", strip=True).split()),
                company=" ".join(company.split()),
                location=" ".join(location.split()),
                region=row.get("data-region", ""),
                kind=row.get("data-kind", ""),
                posted_at=posted,
            )
        )
    return jobs


def wants_title(title: str) -> bool:
    """A developer-type role: the Himalayas title filter."""
    return bool(re.search(DEFAULT_TITLE, title, re.IGNORECASE)) and not re.search(
        EXCLUDE_TITLE, title, re.IGNORECASE
    )


def parse_regions(text: str) -> tuple[str, ...]:
    """``"remote, apac"`` -> ("remote", "apac"); empty means every region."""
    regions = tuple(r.strip().lower() for r in text.split(",") if r.strip())
    unknown = [r for r in regions if r not in REGIONS]
    if unknown:
        raise ValueError(f"unknown FDE Times region {unknown[0]!r}; known: {', '.join(REGIONS)}")
    return regions or REGIONS


@dataclass
class FDETimesStats:
    listed: int = 0  # roles on the page
    fresh: int = 0  # published since the last run
    new: int = 0
    known: int = 0
    skipped: int = 0  # not the kind, region or role asked for
    error: str | None = None


def _add(session: Session, job: FDEJob) -> None:
    from .auto_ignore import apply_rules

    score = score_job(job.title, job.location or None)
    opportunity = Opportunity(
        title=job.title[:255],
        company=get_or_create_company(session, job.company or f"Unknown (FDE Times {job.slug})"),
        location=job.location or None,
        score=score.value,
        score_reasons=[r.as_dict() for r in score.reasons],
    )
    session.add(
        SourceRecord(
            source_type=SourceType.FDETIMES,
            external_id=job.slug,
            url=job.url,
            opportunity=opportunity,
        )
    )
    session.flush()
    session.refresh(opportunity)
    apply_rules(session, opportunity)


def import_fdetimes(
    session: Session,
    client: Client,
    *,
    now: datetime | None = None,
    first_run_days: float = 7,
    regions: tuple[str, ...] = REGIONS,
    related: bool = False,
) -> FDETimesStats:
    now = now or datetime.now(UTC)
    state = session.get(SyncCursor, CURSOR)
    if state and state.last_run_at:
        since = state.last_run_at.replace(tzinfo=state.last_run_at.tzinfo or UTC) - MARGIN
    else:
        since = now - timedelta(days=first_run_days)
    stats = FDETimesStats()
    try:
        jobs = parse_jobs(client.jobs_page())
    except FDETimesError as e:
        stats.error = str(e)
        return stats
    stats.listed = len(jobs)
    kinds = ("fde", "related") if related else ("fde",)
    for job in jobs:
        if job.posted_at < since:
            continue
        stats.fresh += 1
        if job.kind not in kinds or job.region not in regions or not wants_title(job.title):
            stats.skipped += 1
        elif session.scalar(
            select(SourceRecord.id).where(
                SourceRecord.source_type == SourceType.FDETIMES,
                SourceRecord.external_id == job.slug,
            )
        ):
            stats.known += 1
        else:
            _add(session, job)
            stats.new += 1
    state = state or SyncCursor(name=CURSOR)
    state.last_run_at = now
    session.add(state)
    session.commit()
    return stats


# --- descriptions -----------------------------------------------------------


def parse_job_page(html: str) -> str | None:
    """The description of a role page: its JobPosting data, else the description block."""
    soup = BeautifulSoup(html, "html.parser")
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
        except ValueError:
            continue
        for item in data if isinstance(data, list) else [data]:
            if not (isinstance(item, dict) and item.get("@type") == "JobPosting"):
                continue
            if text := _text(str(item.get("description") or "")):
                return text
    article = soup.select_one("article#job-desc")
    if article is None:
        return None
    for heading in article.select(".job-desc-h"):
        heading.decompose()
    return _text(str(article)) or None


class Stopped(Exception):
    """The user pressed Stop; raised before the next request."""


class PageClient:
    """Role pages with a random pause before every request but the first."""

    def __init__(
        self,
        http_get: HttpGet = urllib_get,
        *,
        pause: tuple[float, float] = (10.0, 40.0),
        sleep: Callable[[float], object] | None = None,
        rand: Callable[[float, float], float] = random.uniform,
        stop: threading.Event | None = None,
        on_wait: Callable[[float], None] | None = None,
    ):
        self._get, self._pause, self._sleep, self._rand = http_get, pause, sleep, rand
        self._stop, self._on_wait = stop, on_wait
        self._requested = False

    def _stopped(self) -> bool:
        return self._stop is not None and self._stop.is_set()

    def _wait(self) -> None:
        seconds = self._rand(*self._pause)
        if self._on_wait:
            self._on_wait(seconds)
        if self._sleep is not None:
            self._sleep(seconds)
        elif self._stop is not None:
            self._stop.wait(seconds)
        else:
            time.sleep(seconds)

    def job_page(self, slug: str) -> tuple[int, str]:
        if self._stopped():
            raise Stopped
        if self._requested:
            self._wait()
            if self._stopped():
                raise Stopped
        self._requested = True
        return self._get(JOB_URL.format(slug=slug))


@dataclass
class FDEDescriptionStats:
    total: int = 0
    requests: int = 0
    saved: int = 0
    errors: int = 0
    gone: int = 0
    blocked: bool = False
    reason: str | None = None
    stopped: bool = False
    problems: list[str] = field(default_factory=list)


def without_description(session: Session) -> list[tuple[Opportunity, SourceRecord]]:
    """To review vacancies from FDE Times with no description from any source, best first."""
    rows = session.scalars(
        select(Opportunity)
        .join(SourceRecord)
        .where(
            Opportunity.state == OpportunityState.NEW,
            Opportunity.unavailable_at.is_(None),
            SourceRecord.source_type == SourceType.FDETIMES,
        )
        .distinct()
    ).all()
    out = []
    for o in rows:
        if any(r.snapshots for r in o.source_records):
            continue
        record = next(r for r in o.source_records if r.source_type is SourceType.FDETIMES)
        out.append((o, record))
    return sorted(out, key=lambda pair: -(pair[0].score if pair[0].score is not None else -1))


def import_descriptions(
    session: Session,
    client: PageClient,
    *,
    max_per_run: int = 50,
    on_progress: Callable[[FDEDescriptionStats], None] | None = None,
) -> FDEDescriptionStats:
    """Description from the role page for To review vacancies without one (docs/fdetimes.md)."""
    from .auto_ignore import apply_rules, mark_unavailable

    def progress() -> None:
        if on_progress:
            on_progress(stats)

    stats = FDEDescriptionStats()
    todo = without_description(session)[: max(0, max_per_run)]
    stats.total = len(todo)
    progress()
    for opportunity, record in todo:
        try:
            status, body = client.job_page(record.external_id)
        except Stopped:
            stats.stopped = True
            break
        except FDETimesError as e:
            status, body = None, str(e)
        stats.requests += 1
        if status == 200 and (description := parse_job_page(body)):
            add_snapshot(session, record, description)
            stats.saved += 1
            session.refresh(opportunity)
            apply_rules(session, opportunity)
        elif status in GONE:
            mark_unavailable(session, opportunity)
            stats.gone += 1
        elif status in BLOCKED:
            stats.blocked, stats.reason = True, f"HTTP {status}"
        else:
            stats.errors += 1
            why = (
                body
                if status is None
                else (f"HTTP {status}" if status != 200 else "no description")
            )
            stats.problems.append(f"{record.external_id}: {why}")
        session.commit()
        progress()
        if stats.blocked:
            break
    return stats
