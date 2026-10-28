"""Fresh Python jobs from the Jobicy public API. See docs/jobicy.md."""

import html
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from .dedup import add_snapshot, get_or_create_company
from .himalayas import EXCLUDE_TITLE, USER_AGENT, _text
from .models import Opportunity, SourceRecord, SourceType, SyncCursor
from .scoring import score_job
from .telegram.sources import DEFAULT_KEYWORDS, DEFAULT_TITLE

API_URL = "https://jobicy.com/api/v2/remote-jobs?count=50&tag=python"
CURSOR = "jobicy"
MAX_PAGES = 4
DELAY_SECONDS = 2.0
FIRST_RUN = timedelta(days=1)
MARGIN = timedelta(days=1)


class JobicyError(Exception):
    pass


class Client(Protocol):
    def page(self, cursor: str | None) -> dict[str, Any]: ...


class JobicyClient:
    def page(self, cursor: str | None) -> dict[str, Any]:
        url = API_URL + (f"&cursor={urllib.parse.quote(cursor)}" if cursor else "")
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.loads(response.read())
        except urllib.error.HTTPError as e:
            raise JobicyError(f"HTTP {e.code}") from e
        except (urllib.error.URLError, TimeoutError, ValueError) as e:
            raise JobicyError(str(e)) from e


@dataclass(frozen=True)
class JobicyJob:
    id: str
    url: str
    title: str
    company: str
    location: str
    description: str
    published_at: datetime


def _location(geo: str) -> str:
    regions = [r.strip() for r in geo.split(",") if r.strip()]
    if not regions or any(r.lower() in ("anywhere", "worldwide") for r in regions):
        return "Worldwide (Remote)"
    return ", ".join(["Remote", *regions])


def parse_job(raw: dict[str, Any]) -> JobicyJob:
    return JobicyJob(
        id=str(raw["id"]),
        url=str(raw.get("url") or f"https://jobicy.com/jobs/{raw['id']}"),
        title=" ".join(html.unescape(str(raw.get("jobTitle") or "")).split()),
        company=" ".join(html.unescape(str(raw.get("companyName") or "")).split()),
        location=_location(html.unescape(str(raw.get("jobGeo") or ""))),
        description=_text(str(raw.get("jobDescription") or raw.get("jobExcerpt") or "")),
        published_at=datetime.fromisoformat(str(raw["pubDate"])).astimezone(UTC),
    )


def wants(job: JobicyJob) -> bool:
    """A developer role (title filter) that mentions Python somewhere."""
    if not re.search(DEFAULT_TITLE, job.title, re.IGNORECASE):
        return False
    if re.search(EXCLUDE_TITLE, job.title, re.IGNORECASE):
        return False
    return bool(re.search(DEFAULT_KEYWORDS, f"{job.title}\n{job.description}", re.IGNORECASE))


@dataclass
class JobicyStats:
    pages: int = 0
    jobs: int = 0
    new: int = 0
    known: int = 0
    skipped: int = 0
    error: str | None = None


def _add(session: Session, job: JobicyJob) -> None:
    from .auto_ignore import apply_rules

    score = score_job(job.title, job.location)
    opportunity = Opportunity(
        title=job.title[:255],
        company=get_or_create_company(session, job.company or f"Unknown (Jobicy {job.id})"),
        location=job.location,
        score=score.value,
        score_reasons=[r.as_dict() for r in score.reasons],
    )
    record = SourceRecord(
        source_type=SourceType.JOBICY, external_id=job.id, url=job.url, opportunity=opportunity
    )
    session.add(record)
    session.flush()
    if job.description:
        add_snapshot(session, record, job.description)
    session.refresh(opportunity)
    apply_rules(session, opportunity)


def import_jobicy(
    session: Session,
    client: Client,
    *,
    now: datetime | None = None,
    max_pages: int = MAX_PAGES,
    delay: float = DELAY_SECONDS,
) -> JobicyStats:
    now = now or datetime.now(UTC)
    state = session.get(SyncCursor, CURSOR)
    if state and state.last_run_at:
        since = state.last_run_at.replace(tzinfo=state.last_run_at.tzinfo or UTC) - MARGIN
    else:
        since = now - FIRST_RUN
    stats = JobicyStats()
    cursor: str | None = None
    for page_number in range(1, max_pages + 1):
        if page_number > 1 and delay:
            time.sleep(delay)
        try:
            data = client.page(cursor)
        except JobicyError as e:
            stats.error = str(e)
            break
        stats.pages += 1
        jobs = [parse_job(raw) for raw in data.get("jobs") or []]
        for job in jobs:
            if job.published_at < since:
                continue
            stats.jobs += 1
            if not wants(job):
                stats.skipped += 1
            elif session.scalar(
                select(SourceRecord.id).where(
                    SourceRecord.source_type == SourceType.JOBICY,
                    SourceRecord.external_id == job.id,
                )
            ):
                stats.known += 1
            else:
                _add(session, job)
                stats.new += 1
        session.commit()
        cursor = data.get("nextCursor")
        more = str(data.get("hasMore")).lower() == "true"
        # Newest first: stop at the first page that reaches back past the cutoff.
        if not jobs or not cursor or not more or any(j.published_at < since for j in jobs):
            break
    if not stats.error:
        state = state or SyncCursor(name=CURSOR)
        state.last_run_at = now
        session.add(state)
        session.commit()
    return stats
