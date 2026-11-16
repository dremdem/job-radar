"""Fresh worldwide Python jobs from the Himalayas public API.

``HimalayasClient`` reads the search API page by page and raises
``HimalayasRateLimited`` when asked to slow down; ``parse_job`` turns an item into a
``HimalayasJob``, ``wants`` keeps developer roles that mention Python, and
``import_himalayas`` stores the new ones. See docs/himalayas.md.

Usage:
    import job_radar.himalayas as himalayas
"""

import dataclasses
import datetime as dt
import json
import re
import time
import typing as t
import urllib.error
import urllib.request

import sqlalchemy as sa
import sqlalchemy.orm as sa_orm

import job_radar.dedup as dedup
import job_radar.models as models
import job_radar.scoring.rules as scoring_rules
import job_radar.sources.http as sources_http
import job_radar.sources.titles as sources_titles

SEARCH_URL = "https://himalayas.app/jobs/api/search?q=python&worldwide=true&sort=recent&page={page}"
CURSOR = "himalayas"
MAX_PAGES = 10
DELAY_SECONDS = 2.0
# A job can reach the daily cache later than its pubDate: re-read one day before the last run.
MARGIN = dt.timedelta(days=1)
_PERIODS = {"annual": "year", "yearly": "year", "monthly": "month", "hourly": "hour"}


class HimalayasError(Exception):
    """The Himalayas API could not be read."""

    pass


class HimalayasRateLimited(HimalayasError):
    """Himalayas asked to slow down; the import stops for this run."""

    pass


class Client(t.Protocol):
    """What the import needs from the Himalayas API: one search page. Tests pass a fake."""

    def search(self, page_number: int) -> dict[str, t.Any]: ...


class HimalayasClient:
    """The real client over the public Himalayas search API."""

    def search(self, page_number: int) -> dict[str, t.Any]:
        request = urllib.request.Request(
            SEARCH_URL.format(page=page_number), headers={"User-Agent": sources_http.USER_AGENT}
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.loads(response.read())
        except urllib.error.HTTPError as e:
            if e.code == 429:
                raise HimalayasRateLimited("Himalayas rate limit (429)") from e
            raise HimalayasError(f"HTTP {e.code}") from e
        except (urllib.error.URLError, TimeoutError, ValueError) as e:
            raise HimalayasError(str(e)) from e


@dataclasses.dataclass(frozen=True)
class HimalayasJob:
    """One job from the Himalayas API, in the shape the import stores."""

    url: str
    title: str
    company: str
    location: str
    salary: str | None
    description: str
    categories: list[str]
    published_at: dt.datetime


def _location(raw: dict[str, t.Any]) -> str:
    # Only where the candidate may live counts; any timezone is fine for the user (#91).
    countries = [str(c) for c in raw.get("locationRestrictions") or []]
    return ", ".join(["Remote", *countries]) if countries else "Worldwide (Remote)"


def _salary(raw: dict[str, t.Any]) -> str | None:
    low, high = raw.get("minSalary"), raw.get("maxSalary")
    if low is None and high is None:
        return None
    amounts = [f"{int(v):,}" for v in (low, high) if v is not None]
    amount = amounts[0] if len(set(amounts)) == 1 else "-".join(amounts)
    currency = f" {raw['currency']}" if raw.get("currency") else ""
    period = raw.get("salaryPeriod")
    return f"{amount}{currency}" + (f" / {_PERIODS.get(period, period)}" if period else "")


def parse_job(raw: dict[str, t.Any]) -> HimalayasJob:
    return HimalayasJob(
        url=str(raw.get("guid") or raw.get("applicationLink")),
        title=" ".join(str(raw.get("title") or "").split()),
        company=str(raw.get("companyName") or "").strip(),
        location=_location(raw),
        salary=_salary(raw),
        description=sources_http.html_text(raw.get("description") or raw.get("excerpt") or ""),
        categories=[str(c) for c in raw.get("categories") or []],
        published_at=dt.datetime.fromtimestamp(int(raw["pubDate"]), dt.UTC),
    )


def wants(job: HimalayasJob) -> bool:
    """A developer role (title filter) that mentions Python somewhere."""
    if not re.search(sources_titles.DEFAULT_TITLE, job.title, re.IGNORECASE):
        return False
    if re.search(sources_titles.BOARD_EXCLUDE_TITLE, job.title, re.IGNORECASE):
        return False
    text = "\n".join([job.title, job.description, " ".join(job.categories)])
    return bool(re.search(sources_titles.DEFAULT_KEYWORDS, text, re.IGNORECASE))


@dataclasses.dataclass
class HimalayasStats:
    """Counters of one import, for the sync status line."""

    pages: int = 0
    jobs: int = 0
    new: int = 0
    known: int = 0
    skipped: int = 0
    blocked: bool = False
    error: str | None = None


def _add(session: sa_orm.Session, job: HimalayasJob) -> None:
    import job_radar.auto_ignore as auto_ignore

    score = scoring_rules.score_job(job.title, job.location)
    opportunity = models.Opportunity(
        title=job.title[:255],
        company=dedup.get_or_create_company(
            session, job.company or f"Unknown (Himalayas {job.url})"
        ),
        location=job.location,
        salary=job.salary,
        score=score.value,
        score_reasons=[r.as_dict() for r in score.reasons],
    )
    record = models.SourceRecord(
        source_type=models.SourceType.HIMALAYAS,
        external_id=job.url,
        url=job.url,
        opportunity=opportunity,
    )
    session.add(record)
    session.flush()
    if job.description:
        dedup.add_snapshot(session, record, job.description)
    session.refresh(opportunity)
    auto_ignore.apply_rules(session, opportunity)


def import_himalayas(
    session: sa_orm.Session,
    client: Client,
    *,
    now: dt.datetime | None = None,
    first_run_days: float = 1,
    max_pages: int = MAX_PAGES,
    delay: float = DELAY_SECONDS,
) -> HimalayasStats:
    now = now or dt.datetime.now(dt.UTC)
    cursor = session.get(models.SyncCursor, CURSOR)
    if cursor and cursor.last_run_at:
        since = cursor.last_run_at.replace(tzinfo=cursor.last_run_at.tzinfo or dt.UTC) - MARGIN
    else:
        since = now - dt.timedelta(days=first_run_days)
    stats = HimalayasStats()
    seen: set[str] = set()
    newest: dt.datetime | None = None
    for page_number in range(1, max_pages + 1):
        if page_number > 1 and delay:
            time.sleep(delay)
        try:
            data = client.search(page_number)
        except HimalayasRateLimited:
            stats.blocked = True
            break
        except HimalayasError as e:
            stats.error = str(e)
            break
        stats.pages += 1
        raw_jobs = data.get("jobs") or []
        jobs = [parse_job(raw) for raw in raw_jobs]
        fresh = [j for j in jobs if j.published_at >= since and j.url not in seen]
        for job in fresh:
            seen.add(job.url)
            stats.jobs += 1
            newest = max(newest, job.published_at) if newest else job.published_at
            if not wants(job):
                stats.skipped += 1
            elif session.scalar(
                sa.select(models.SourceRecord.id).where(
                    models.SourceRecord.source_type == models.SourceType.HIMALAYAS,
                    models.SourceRecord.external_id == job.url,
                )
            ):
                stats.known += 1
            else:
                _add(session, job)
                stats.new += 1
        session.commit()
        # Sorted by date only roughly: stop once a whole page is older than the cutoff.
        if not raw_jobs or all(j.published_at < since for j in jobs):
            break
    if not (stats.blocked or stats.error):
        cursor = cursor or models.SyncCursor(name=CURSOR)
        cursor.last_run_at = now
        cursor.last_message_at = newest or cursor.last_message_at
        session.add(cursor)
        session.commit()
    return stats
