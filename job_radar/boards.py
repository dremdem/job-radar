"""Small remote job boards with a public feed: Remotive, Working Nomads, We Work Remotely.

Each brings a few Python jobs a month, so they share one sync step. See docs/boards.md.
"""

import json
import re
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from .dedup import add_snapshot, get_or_create_company
from .himalayas import EXCLUDE_TITLE, USER_AGENT, _text
from .models import Opportunity, SourceRecord, SourceType, SyncCursor
from .scoring import score_job
from .telegram.sources import DEFAULT_KEYWORDS, DEFAULT_TITLE

MAX_AGE = timedelta(days=30)
# The feeds change a few times a day; Remotive asks for at most 4 requests a day.
MIN_INTERVAL = timedelta(hours=6)
# Lemon.io and the like list Python in every post's boilerplate: a title that names
# another stack and no Python is not a Python job.
OTHER_STACK_TITLE = re.compile(
    # Not \b: it does not match before "." or after "+" / "#".
    r"(?<!\w)(php|\.net|java|ruby|rails|swift|swiftui|kotlin|android|ios|angular|vue|react|node(\.js)?|"
    r"golang|go|rust|scala|elixir|c\+\+|c#|salesforce|shopify|wordpress|sap)(?!\w)",
    re.IGNORECASE,
)
_WORLDWIDE = re.compile(r"^(worldwide|anywhere( in the world)?|global)$", re.IGNORECASE)


class BoardError(Exception):
    pass


class Client(Protocol):
    def get(self, url: str) -> bytes: ...


class HttpClient:
    def get(self, url: str) -> bytes:
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return response.read()
        except urllib.error.HTTPError as e:
            raise BoardError(f"HTTP {e.code}") from e
        except (urllib.error.URLError, TimeoutError) as e:
            raise BoardError(str(e)) from e


@dataclass(frozen=True)
class BoardJob:
    id: str
    url: str
    title: str
    company: str
    location: str
    salary: str | None
    description: str
    tags: str
    published_at: datetime


def location(place: str) -> str:
    place = " ".join(place.split()).strip(" ,")
    if not place or _WORLDWIDE.match(place):
        return "Worldwide (Remote)"
    return f"Remote, {place}"


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def parse_remotive(data: bytes) -> list[BoardJob]:
    try:
        jobs = json.loads(data).get("jobs") or []
    except (ValueError, AttributeError) as e:
        raise BoardError(f"unexpected Remotive feed: {e}") from e
    return [
        BoardJob(
            id=str(raw["id"]),
            url=str(raw["url"]),
            title=" ".join(str(raw.get("title") or "").split()),
            company=" ".join(str(raw.get("company_name") or "").split()),
            location=location(str(raw.get("candidate_required_location") or "")),
            salary=str(raw.get("salary") or "").strip() or None,
            description=_text(str(raw.get("description") or "")),
            tags=" ".join(str(t) for t in raw.get("tags") or []),
            # Remotive gives UTC without an offset.
            published_at=_utc(datetime.fromisoformat(str(raw["publication_date"]))),
        )
        for raw in jobs
    ]


def parse_workingnomads(data: bytes) -> list[BoardJob]:
    try:
        jobs = json.loads(data)
        assert isinstance(jobs, list)
    except (ValueError, AssertionError) as e:
        raise BoardError("unexpected Working Nomads feed") from e
    out = []
    for raw in jobs:
        url = str(raw.get("url") or "")
        found = re.search(r"/(\d+)/?$", url)
        out.append(
            BoardJob(
                id=found.group(1) if found else url,
                url=url,
                title=" ".join(str(raw.get("title") or "").split()),
                company=" ".join(str(raw.get("company_name") or "").split()),
                location=location(str(raw.get("location") or "")),
                salary=None,
                description=_text(str(raw.get("description") or "")),
                tags=f"{raw.get('tags') or ''} {raw.get('category_name') or ''}",
                published_at=_utc(datetime.fromisoformat(str(raw["pub_date"]))),
            )
        )
    return out


def parse_weworkremotely(data: bytes) -> list[BoardJob]:
    try:
        root = ET.fromstring(data)
    except ET.ParseError as e:
        raise BoardError(f"unexpected We Work Remotely feed: {e}") from e
    out = []
    for item in root.iter("item"):
        text = {child.tag: (child.text or "").strip() for child in item}
        # "Company: Role"
        company, _, title = text.get("title", "").partition(": ")
        if not title:
            company, title = "", company
        # The country list can name 70 countries with flags; the region says it in short.
        place = text.get("region") or text.get("country") or ""
        out.append(
            BoardJob(
                id=text.get("guid") or text.get("link", ""),
                url=text.get("link") or text.get("guid", ""),
                title=" ".join(title.split()),
                company=" ".join(company.split()),
                location=location(place),
                salary=None,
                description=_text(text.get("description", "")),
                tags=f"{text.get('skills', '')} {text.get('category', '')}",
                published_at=_utc(parsedate_to_datetime(text["pubDate"])),
            )
        )
    return out


@dataclass(frozen=True)
class Board:
    key: str
    label: str
    source_type: SourceType
    urls: tuple[str, ...]
    parse: Callable[[bytes], list[BoardJob]]


BOARDS = (
    Board(
        "remotive",
        "Remotive",
        SourceType.REMOTIVE,
        ("https://remotive.com/api/remote-jobs?category=software-dev",),
        parse_remotive,
    ),
    Board(
        "workingnomads",
        "Working Nomads",
        SourceType.WORKINGNOMADS,
        ("https://www.workingnomads.com/api/exposed_jobs/",),
        parse_workingnomads,
    ),
    Board(
        "weworkremotely",
        "We Work Remotely",
        SourceType.WEWORKREMOTELY,
        (
            "https://weworkremotely.com/categories/remote-back-end-programming-jobs.rss",
            "https://weworkremotely.com/categories/remote-full-stack-programming-jobs.rss",
        ),
        parse_weworkremotely,
    ),
)


def wants(job: BoardJob) -> bool:
    """A developer role (title filter) that mentions Python somewhere."""
    if not re.search(DEFAULT_TITLE, job.title, re.IGNORECASE):
        return False
    if re.search(EXCLUDE_TITLE, job.title, re.IGNORECASE):
        return False
    if OTHER_STACK_TITLE.search(job.title) and not re.search(
        DEFAULT_KEYWORDS, job.title, re.IGNORECASE
    ):
        return False
    text = "\n".join([job.title, job.tags, job.description])
    return bool(re.search(DEFAULT_KEYWORDS, text, re.IGNORECASE))


@dataclass
class BoardStats:
    label: str
    jobs: int = 0
    old: int = 0
    new: int = 0
    known: int = 0
    skipped: int = 0
    waited: bool = False  # read less than MIN_INTERVAL ago
    error: str | None = None


def _add(session: Session, board: Board, job: BoardJob) -> None:
    from .auto_ignore import apply_rules

    score = score_job(job.title, job.location)
    opportunity = Opportunity(
        title=job.title[:255],
        company=get_or_create_company(session, job.company or f"Unknown ({board.label} {job.id})"),
        location=job.location,
        salary=job.salary,
        score=score.value,
        score_reasons=[r.as_dict() for r in score.reasons],
    )
    record = SourceRecord(
        source_type=board.source_type, external_id=job.id, url=job.url, opportunity=opportunity
    )
    session.add(record)
    session.flush()
    if job.description:
        add_snapshot(session, record, job.description)
    session.refresh(opportunity)
    apply_rules(session, opportunity)


def import_board(
    session: Session, board: Board, client: Client, *, now: datetime | None = None
) -> BoardStats:
    now = now or datetime.now(UTC)
    stats = BoardStats(board.label)
    state = session.get(SyncCursor, f"board:{board.key}")
    if state and state.last_run_at and now - _utc(state.last_run_at) < MIN_INTERVAL:
        stats.waited = True
        return stats
    jobs: list[BoardJob] = []
    try:
        for url in board.urls:
            jobs += board.parse(client.get(url))
    except BoardError as e:
        stats.error = str(e)
        return stats
    seen: set[str] = set()
    for job in jobs:
        if job.id in seen:  # the same job in two categories
            continue
        seen.add(job.id)
        stats.jobs += 1
        if job.published_at < now - MAX_AGE:
            stats.old += 1
        elif not wants(job):
            stats.skipped += 1
        elif session.scalar(
            select(SourceRecord.id).where(
                SourceRecord.source_type == board.source_type,
                SourceRecord.external_id == job.id,
            )
        ):
            stats.known += 1
        else:
            _add(session, board, job)
            stats.new += 1
    state = state or SyncCursor(name=f"board:{board.key}")
    state.last_run_at = now
    session.add(state)
    session.commit()
    return stats


def import_boards(
    session: Session, client: Client, *, now: datetime | None = None
) -> list[BoardStats]:
    """Every board on its own: a failed board does not stop the others."""
    return [import_board(session, board, client, now=now) for board in BOARDS]
