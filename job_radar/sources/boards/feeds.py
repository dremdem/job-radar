"""Small remote job boards with a public feed: Remotive, Working Nomads, We Work Remotely.

Each board brings a few Python jobs a month, so they share one sync step. A ``Board``
describes one feed (its URLs and parser); ``parse_remotive``, ``parse_workingnomads`` and
``parse_weworkremotely`` turn a feed into ``BoardJob`` items, ``wants`` keeps developer
roles that mention Python, and ``import_boards`` stores new ones, each board on its own so
a failed board does not stop the others. See docs/boards.md.

Usage:
    import job_radar.sources.boards.feeds as boards_feeds
"""

import collections.abc as cabc
import dataclasses
import datetime as dt
import email.utils as email_utils
import json
import re
import typing as t
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET

import defusedxml
import defusedxml.ElementTree as defusedxml_ElementTree
import sqlalchemy as sa
import sqlalchemy.orm as sa_orm

import job_radar.dedup as dedup
import job_radar.models as models
import job_radar.scoring.rules as scoring_rules
import job_radar.sources.http as sources_http
import job_radar.sources.titles as sources_titles

MAX_AGE = dt.timedelta(days=30)
# The feeds change a few times a day; Remotive asks for at most 4 requests a day.
MIN_INTERVAL = dt.timedelta(hours=6)
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
    """A board's feed could not be read or parsed."""

    pass


class Client(t.Protocol):
    """What the import needs from HTTP: the bytes at a URL. Tests pass a fake."""

    def get(self, url: str) -> bytes: ...


class HttpClient:
    """The real client: plain HTTPS GET with a timeout."""

    def get(self, url: str) -> bytes:
        request = urllib.request.Request(url, headers={"User-Agent": sources_http.USER_AGENT})
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return response.read()
        except urllib.error.HTTPError as e:
            raise BoardError(f"HTTP {e.code}") from e
        except (urllib.error.URLError, TimeoutError) as e:
            raise BoardError(str(e)) from e


@dataclasses.dataclass(frozen=True)
class BoardJob:
    """One job from a board feed, in the shape the import stores."""

    id: str
    url: str
    title: str
    company: str
    location: str
    salary: str | None
    description: str
    tags: str
    published_at: dt.datetime


def location(place: str) -> str:
    place = " ".join(place.split()).strip(" ,")
    if not place or _WORLDWIDE.match(place):
        return "Worldwide (Remote)"
    return f"Remote, {place}"


def _utc(value: dt.datetime) -> dt.datetime:
    return value.replace(tzinfo=dt.UTC) if value.tzinfo is None else value.astimezone(dt.UTC)


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
            description=sources_http.html_text(str(raw.get("description") or "")),
            tags=" ".join(str(tag) for tag in raw.get("tags") or []),
            # Remotive gives UTC without an offset.
            published_at=_utc(dt.datetime.fromisoformat(str(raw["publication_date"]))),
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
                description=sources_http.html_text(str(raw.get("description") or "")),
                tags=f"{raw.get('tags') or ''} {raw.get('category_name') or ''}",
                published_at=_utc(dt.datetime.fromisoformat(str(raw["pub_date"]))),
            )
        )
    return out


def parse_weworkremotely(data: bytes) -> list[BoardJob]:
    try:
        root = defusedxml_ElementTree.fromstring(data)
    except (ET.ParseError, defusedxml.DefusedXmlException) as e:
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
                description=sources_http.html_text(text.get("description", "")),
                tags=f"{text.get('skills', '')} {text.get('category', '')}",
                published_at=_utc(email_utils.parsedate_to_datetime(text["pubDate"])),
            )
        )
    return out


@dataclasses.dataclass(frozen=True)
class Board:
    """One board: its key, label, source type, feed URLs and parser."""

    key: str
    label: str
    source_type: models.SourceType
    urls: tuple[str, ...]
    parse: cabc.Callable[[bytes], list[BoardJob]]


BOARDS = (
    Board(
        "remotive",
        "Remotive",
        models.SourceType.REMOTIVE,
        ("https://remotive.com/api/remote-jobs?category=software-dev",),
        parse_remotive,
    ),
    Board(
        "workingnomads",
        "Working Nomads",
        models.SourceType.WORKINGNOMADS,
        ("https://www.workingnomads.com/api/exposed_jobs/",),
        parse_workingnomads,
    ),
    Board(
        "weworkremotely",
        "We Work Remotely",
        models.SourceType.WEWORKREMOTELY,
        (
            "https://weworkremotely.com/categories/remote-back-end-programming-jobs.rss",
            "https://weworkremotely.com/categories/remote-full-stack-programming-jobs.rss",
        ),
        parse_weworkremotely,
    ),
)


def wants(job: BoardJob) -> bool:
    """A developer role (title filter) that mentions Python somewhere."""
    if not re.search(sources_titles.DEFAULT_TITLE, job.title, re.IGNORECASE):
        return False
    if re.search(sources_titles.BOARD_EXCLUDE_TITLE, job.title, re.IGNORECASE):
        return False
    if OTHER_STACK_TITLE.search(job.title) and not re.search(
        sources_titles.DEFAULT_KEYWORDS, job.title, re.IGNORECASE
    ):
        return False
    text = "\n".join([job.title, job.tags, job.description])
    return bool(re.search(sources_titles.DEFAULT_KEYWORDS, text, re.IGNORECASE))


@dataclasses.dataclass
class BoardStats:
    """Counters of one board's import, for the sync status line."""

    label: str
    jobs: int = 0
    old: int = 0
    new: int = 0
    known: int = 0
    skipped: int = 0
    # read less than MIN_INTERVAL ago
    waited: bool = False
    error: str | None = None


def _add(session: sa_orm.Session, board: Board, job: BoardJob) -> None:
    import job_radar.review.auto_ignore as review_auto_ignore

    score = scoring_rules.score_job(job.title, job.location)
    opportunity = models.Opportunity(
        title=job.title[:255],
        company=dedup.get_or_create_company(
            session, job.company or f"Unknown ({board.label} {job.id})"
        ),
        location=job.location,
        salary=job.salary,
        score=score.value,
        score_reasons=[r.as_dict() for r in score.reasons],
    )
    record = models.SourceRecord(
        source_type=board.source_type, external_id=job.id, url=job.url, opportunity=opportunity
    )
    session.add(record)
    session.flush()
    if job.description:
        dedup.add_snapshot(session, record, job.description)
    session.refresh(opportunity)
    review_auto_ignore.apply_rules(session, opportunity)


def import_board(
    session: sa_orm.Session, board: Board, client: Client, *, now: dt.datetime | None = None
) -> BoardStats:
    now = now or dt.datetime.now(dt.UTC)
    stats = BoardStats(board.label)
    state = session.get(models.SyncCursor, f"board:{board.key}")
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
        # the same job in two categories
        if job.id in seen:
            continue
        seen.add(job.id)
        stats.jobs += 1
        if job.published_at < now - MAX_AGE:
            stats.old += 1
        elif not wants(job):
            stats.skipped += 1
        elif session.scalar(
            sa.select(models.SourceRecord.id).where(
                models.SourceRecord.source_type == board.source_type,
                models.SourceRecord.external_id == job.id,
            )
        ):
            stats.known += 1
        else:
            _add(session, board, job)
            stats.new += 1
    state = state or models.SyncCursor(name=f"board:{board.key}")
    state.last_run_at = now
    session.add(state)
    session.commit()
    return stats


def import_boards(
    session: sa_orm.Session, client: Client, *, now: dt.datetime | None = None
) -> list[BoardStats]:
    """Every board on its own: a failed board does not stop the others."""
    return [import_board(session, board, client, now=now) for board in BOARDS]
