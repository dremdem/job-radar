"""Fresh worldwide Python jobs from the Himalayas public API. See docs/himalayas.md."""

import json
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from bs4 import BeautifulSoup
from sqlalchemy import select
from sqlalchemy.orm import Session

from .dedup import add_snapshot, get_or_create_company
from .models import Opportunity, SourceRecord, SourceType, SyncCursor
from .scoring import score_job
from .telegram.sources import DEFAULT_EXCLUDE_TITLE, DEFAULT_KEYWORDS, DEFAULT_TITLE

SEARCH_URL = "https://himalayas.app/jobs/api/search?q=python&worldwide=true&sort=recent&page={page}"
USER_AGENT = "job-radar (personal job search, one run a day)"
CURSOR = "himalayas"
MAX_PAGES = 10
DELAY_SECONDS = 2.0
# A job can reach the daily cache later than its pubDate: re-read one day before the last run.
MARGIN = timedelta(days=1)
# Measured noise of this search (docs/himalayas.md), on top of the Telegram title filter.
EXCLUDE_TITLE = (
    DEFAULT_EXCLUDE_TITLE + r"|secur|appsec|scientist|research|mathematic|physicist|"
    r"civil|structural|aerodynam|control system"
)
_PERIODS = {"annual": "year", "yearly": "year", "monthly": "month", "hourly": "hour"}
_BLOCKS = ["p", "div", "li", "br", "h1", "h2", "h3", "h4", "h5", "h6", "ul", "ol", "tr"]


class HimalayasError(Exception):
    pass


class HimalayasRateLimited(HimalayasError):
    pass


class Client(Protocol):
    def search(self, page_number: int) -> dict[str, Any]: ...


class HimalayasClient:
    def search(self, page_number: int) -> dict[str, Any]:
        request = urllib.request.Request(
            SEARCH_URL.format(page=page_number), headers={"User-Agent": USER_AGENT}
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


@dataclass(frozen=True)
class HimalayasJob:
    url: str
    title: str
    company: str
    location: str
    salary: str | None
    description: str
    categories: list[str]
    published_at: datetime


def _text(html: str) -> str:
    soup = BeautifulSoup(html or "", "html.parser")
    for a in soup.find_all("a", href=True):
        # Hacker News shows a long link cut short with "..."; keep the whole address (#243).
        shown, href = a.get_text(), str(a["href"])
        if shown.endswith("...") and href.startswith(shown[:-3]):
            a.string = href
    for tag in soup.find_all(_BLOCKS):
        if tag.name == "li":
            tag.insert_before("• ")
        tag.insert_after("\n")
    text = re.sub(r"[ \t]+\n", "\n", soup.get_text())
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _location(raw: dict[str, Any]) -> str:
    # Only where the candidate may live counts; any timezone is fine for the user (#91).
    countries = [str(c) for c in raw.get("locationRestrictions") or []]
    return ", ".join(["Remote", *countries]) if countries else "Worldwide (Remote)"


def _salary(raw: dict[str, Any]) -> str | None:
    low, high = raw.get("minSalary"), raw.get("maxSalary")
    if low is None and high is None:
        return None
    amounts = [f"{int(v):,}" for v in (low, high) if v is not None]
    amount = amounts[0] if len(set(amounts)) == 1 else "-".join(amounts)
    currency = f" {raw['currency']}" if raw.get("currency") else ""
    period = raw.get("salaryPeriod")
    return f"{amount}{currency}" + (f" / {_PERIODS.get(period, period)}" if period else "")


def parse_job(raw: dict[str, Any]) -> HimalayasJob:
    return HimalayasJob(
        url=str(raw.get("guid") or raw.get("applicationLink")),
        title=" ".join(str(raw.get("title") or "").split()),
        company=str(raw.get("companyName") or "").strip(),
        location=_location(raw),
        salary=_salary(raw),
        description=_text(raw.get("description") or raw.get("excerpt") or ""),
        categories=[str(c) for c in raw.get("categories") or []],
        published_at=datetime.fromtimestamp(int(raw["pubDate"]), UTC),
    )


def wants(job: HimalayasJob) -> bool:
    """A developer role (title filter) that mentions Python somewhere."""
    if not re.search(DEFAULT_TITLE, job.title, re.IGNORECASE):
        return False
    if re.search(EXCLUDE_TITLE, job.title, re.IGNORECASE):
        return False
    text = "\n".join([job.title, job.description, " ".join(job.categories)])
    return bool(re.search(DEFAULT_KEYWORDS, text, re.IGNORECASE))


@dataclass
class HimalayasStats:
    pages: int = 0
    jobs: int = 0
    new: int = 0
    known: int = 0
    skipped: int = 0
    blocked: bool = False
    error: str | None = None


def _add(session: Session, job: HimalayasJob) -> None:
    from .auto_ignore import apply_rules

    score = score_job(job.title, job.location)
    opportunity = Opportunity(
        title=job.title[:255],
        company=get_or_create_company(session, job.company or f"Unknown (Himalayas {job.url})"),
        location=job.location,
        salary=job.salary,
        score=score.value,
        score_reasons=[r.as_dict() for r in score.reasons],
    )
    record = SourceRecord(
        source_type=SourceType.HIMALAYAS, external_id=job.url, url=job.url, opportunity=opportunity
    )
    session.add(record)
    session.flush()
    if job.description:
        add_snapshot(session, record, job.description)
    session.refresh(opportunity)
    apply_rules(session, opportunity)


def import_himalayas(
    session: Session,
    client: Client,
    *,
    now: datetime | None = None,
    first_run_days: float = 1,
    max_pages: int = MAX_PAGES,
    delay: float = DELAY_SECONDS,
) -> HimalayasStats:
    now = now or datetime.now(UTC)
    cursor = session.get(SyncCursor, CURSOR)
    if cursor and cursor.last_run_at:
        since = cursor.last_run_at.replace(tzinfo=cursor.last_run_at.tzinfo or UTC) - MARGIN
    else:
        since = now - timedelta(days=first_run_days)
    stats = HimalayasStats()
    seen: set[str] = set()
    newest: datetime | None = None
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
                select(SourceRecord.id).where(
                    SourceRecord.source_type == SourceType.HIMALAYAS,
                    SourceRecord.external_id == job.url,
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
        cursor = cursor or SyncCursor(name=CURSOR)
        cursor.last_run_at = now
        cursor.last_message_at = newest or cursor.last_message_at
        session.add(cursor)
        session.commit()
    return stats
