"""Fresh Python jobs from the Jobicy public API.

``JobicyClient`` reads the API page by page, ``parse_job`` turns an item into a
``JobicyJob``, ``wants`` keeps developer roles that mention Python, and
``import_jobicy`` stores the new ones and counts the rest as noise. See docs/jobicy.md.

Usage:
    import job_radar.sources.boards.jobicy as boards_jobicy
"""

import dataclasses
import datetime as dt
import html
import json
import re
import time
import typing as t
import urllib.error
import urllib.parse
import urllib.request

import sqlalchemy as sa
import sqlalchemy.orm as sa_orm

import job_radar.dedup as dedup
import job_radar.models as models
import job_radar.scoring.rules as scoring_rules
import job_radar.sources.http as sources_http
import job_radar.sources.titles as sources_titles

API_URL = "https://jobicy.com/api/v2/remote-jobs?count=50&tag=python"
CURSOR = "jobicy"
MAX_PAGES = 4
DELAY_SECONDS = 2.0
FIRST_RUN = dt.timedelta(days=1)
MARGIN = dt.timedelta(days=1)


class JobicyError(Exception):
    """The Jobicy API could not be read."""

    pass


class Client(t.Protocol):
    """What the import needs from the Jobicy API: one page of jobs. Tests pass a fake."""

    def page(self, cursor: str | None) -> dict[str, t.Any]: ...


class JobicyClient:
    """The real client over the public Jobicy API."""

    def page(self, cursor: str | None) -> dict[str, t.Any]:
        url = API_URL + (f"&cursor={urllib.parse.quote(cursor)}" if cursor else "")
        request = urllib.request.Request(url, headers={"User-Agent": sources_http.USER_AGENT})
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.loads(response.read())
        except urllib.error.HTTPError as e:
            raise JobicyError(f"HTTP {e.code}") from e
        except (urllib.error.URLError, TimeoutError, ValueError) as e:
            raise JobicyError(str(e)) from e


@dataclasses.dataclass(frozen=True)
class JobicyJob:
    """One job from the Jobicy API, in the shape the import stores."""

    id: str
    url: str
    title: str
    company: str
    location: str
    description: str
    published_at: dt.datetime


def _location(geo: str) -> str:
    regions = [r.strip() for r in geo.split(",") if r.strip()]
    if not regions or any(r.lower() in ("anywhere", "worldwide") for r in regions):
        return "Worldwide (Remote)"
    return ", ".join(["Remote", *regions])


def parse_job(raw: dict[str, t.Any]) -> JobicyJob:
    return JobicyJob(
        id=str(raw["id"]),
        url=str(raw.get("url") or f"https://jobicy.com/jobs/{raw['id']}"),
        title=" ".join(html.unescape(str(raw.get("jobTitle") or "")).split()),
        company=" ".join(html.unescape(str(raw.get("companyName") or "")).split()),
        location=_location(html.unescape(str(raw.get("jobGeo") or ""))),
        description=sources_http.html_text(
            str(raw.get("jobDescription") or raw.get("jobExcerpt") or "")
        ),
        published_at=dt.datetime.fromisoformat(str(raw["pubDate"])).astimezone(dt.UTC),
    )


def wants(job: JobicyJob) -> bool:
    """A developer role (title filter) that mentions Python somewhere."""
    if not re.search(sources_titles.DEFAULT_TITLE, job.title, re.IGNORECASE):
        return False
    if re.search(sources_titles.BOARD_EXCLUDE_TITLE, job.title, re.IGNORECASE):
        return False
    return bool(
        re.search(sources_titles.DEFAULT_KEYWORDS, f"{job.title}\n{job.description}", re.IGNORECASE)
    )


@dataclasses.dataclass
class JobicyStats:
    """Counters of one import, for the sync status line."""

    pages: int = 0
    jobs: int = 0
    new: int = 0
    known: int = 0
    skipped: int = 0
    error: str | None = None


def _add(session: sa_orm.Session, job: JobicyJob) -> None:
    import job_radar.review.auto_ignore as review_auto_ignore

    score = scoring_rules.score_job(job.title, job.location)
    opportunity = models.Opportunity(
        title=job.title[:255],
        company=dedup.get_or_create_company(session, job.company or f"Unknown (Jobicy {job.id})"),
        location=job.location,
        score=score.value,
        score_reasons=[r.as_dict() for r in score.reasons],
    )
    record = models.SourceRecord(
        source_type=models.SourceType.JOBICY,
        external_id=job.id,
        url=job.url,
        opportunity=opportunity,
    )
    session.add(record)
    session.flush()
    if job.description:
        dedup.add_snapshot(session, record, job.description)
    session.refresh(opportunity)
    review_auto_ignore.apply_rules(session, opportunity)


def import_jobicy(
    session: sa_orm.Session,
    client: Client,
    *,
    now: dt.datetime | None = None,
    max_pages: int = MAX_PAGES,
    delay: float = DELAY_SECONDS,
) -> JobicyStats:
    now = now or dt.datetime.now(dt.UTC)
    state = session.get(models.SyncCursor, CURSOR)
    if state and state.last_run_at:
        since = state.last_run_at.replace(tzinfo=state.last_run_at.tzinfo or dt.UTC) - MARGIN
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
                sa.select(models.SourceRecord.id).where(
                    models.SourceRecord.source_type == models.SourceType.JOBICY,
                    models.SourceRecord.external_id == job.id,
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
        state = state or models.SyncCursor(name=CURSOR)
        state.last_run_at = now
        session.add(state)
        session.commit()
    return stats
