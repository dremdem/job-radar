"""Remote Python posts from the monthly "Ask HN: Who is hiring?" thread.

``latest_thread`` finds this month's thread through the public Hacker News API,
``parse_post`` reads the header line of a comment (company, title, location) and
``wants`` keeps open remote developer posts that mention Python. ``import_hackernews``
stores new ones; a post's id is its comment id. See docs/hackernews.md.

Usage:
    import job_radar.hackernews as hackernews
"""

import dataclasses
import datetime as dt
import html
import json
import re
import typing as t
import urllib.error
import urllib.request

import sqlalchemy as sa
import sqlalchemy.orm as sa_orm

import job_radar.dedup as dedup
import job_radar.models as models
import job_radar.scoring as scoring
import job_radar.sources.http as sources_http
import job_radar.sources.titles as sources_titles

THREADS_URL = "https://hn.algolia.com/api/v1/search_by_date?tags=story,author_whoishiring"
ITEM_URL = "https://hn.algolia.com/api/v1/items/{id}"
POST_URL = "https://news.ycombinator.com/item?id={id}"
THREAD_TITLE = "Ask HN: Who is hiring?"
# | or an em / en dash
_SEPARATOR = re.compile(r"\s*\|\s*|\s+[\u2014\u2013]\s+")
_WORK_FORMAT = re.compile(r"remote|on-?site|hybrid|in[- ]office", re.IGNORECASE)
_REMOTE = re.compile(r"remote", re.IGNORECASE)
_CLOSED = re.compile(r"\b(closed|filled)\b", re.IGNORECASE)
_URL = re.compile(r"https?://\S+")
_REMOTE_NOTE = re.compile(r"\(([^)]*remote[^)]*)\)", re.IGNORECASE)
# A header part is a few words. A longer last part is the text running on: some posts put
# the header and the text in one paragraph, with no line break between them (#256).
MAX_HEADER_PART = 100


class HackerNewsError(Exception):
    """The Hacker News API could not be read."""

    pass


class Client(t.Protocol):
    """What the import needs from the Hacker News API. Tests pass a fake."""

    def threads(self) -> dict[str, t.Any]: ...

    def item(self, item_id: int) -> dict[str, t.Any]: ...


class HackerNewsClient:
    """The real client over the public Hacker News API."""

    def _get(self, url: str) -> dict[str, t.Any]:
        request = urllib.request.Request(url, headers={"User-Agent": sources_http.USER_AGENT})
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return json.loads(response.read())
        except urllib.error.HTTPError as e:
            raise HackerNewsError(f"HTTP {e.code}") from e
        except (urllib.error.URLError, TimeoutError, ValueError) as e:
            raise HackerNewsError(str(e)) from e

    def threads(self) -> dict[str, t.Any]:
        return self._get(THREADS_URL)

    def item(self, item_id: int) -> dict[str, t.Any]:
        return self._get(ITEM_URL.format(id=item_id))


@dataclasses.dataclass(frozen=True)
class HackerNewsPost:
    """One job comment of the thread, with its parsed header."""

    id: int
    url: str
    header: str
    title: str
    company: str
    location: str | None
    description: str
    has_role: bool
    published_at: dt.datetime


def _split_top_level(text: str, pattern: str) -> list[str]:
    """Split on ``pattern`` outside brackets: "REMOTE (US or Canada) or SF" -> 2 parts."""
    parts, depth, start = [], 0, 0
    for match in re.finditer(rf"[()]|{pattern}", text, re.IGNORECASE):
        token = match.group()
        if token == "(":
            depth += 1
        elif token == ")":
            depth = max(0, depth - 1)
        elif depth == 0:
            parts.append(text[start : match.start()])
            start = match.end()
    parts.append(text[start:])
    return [p.strip() for p in parts if p.strip()]


def _location(parts: list[str]) -> str | None:
    remote: list[str] = []
    other: list[str] = []
    # "PrairieLearn (Remote US)": a remote note in brackets after the company.
    remote += [m.strip() for m in _REMOTE_NOTE.findall(parts[0])]
    for part in (_URL.sub("", p) for p in parts[1:]):
        for option in _split_top_level(part, r"\s+or\s+|\s+/\s+"):
            if _REMOTE.search(option):
                remote.append(option)
            elif _WORK_FORMAT.search(option):
                other.append(option)
    found = remote or other
    return " · ".join(found) if found else None


def _header(raw_html: str) -> str:
    first = re.split(r"<p>", raw_html or "", maxsplit=1)[0]
    text = html.unescape(re.sub(r"<[^>]+>", "", first))
    return " ".join(text.splitlines()[0].split()) if text.strip() else ""


def parse_post(raw: dict[str, t.Any]) -> HackerNewsPost:
    header = _header(raw.get("text") or "")
    parts = [p for p in _SEPARATOR.split(header) if p.strip()] or [header]
    if len(parts) > 1 and len(parts[-1]) > MAX_HEADER_PART:
        parts = parts[:-1]
        header = " | ".join(parts)
    company = " ".join(_REMOTE_NOTE.sub("", _URL.sub("", parts[0])).split())
    role = next(
        (
            p
            for p in parts[1:]
            if re.search(sources_titles.DEFAULT_TITLE, p, re.IGNORECASE)
            and not _WORK_FORMAT.search(p)
        ),
        None,
    )
    post_id = int(raw["id"])
    return HackerNewsPost(
        id=post_id,
        url=POST_URL.format(id=post_id),
        header=header,
        title=role or f"{company}: roles in the post",
        company=company,
        location=_location(parts),
        # HN separates paragraphs with a bare <p>: keep the header on its own line.
        description=sources_http.html_text((raw.get("text") or "").replace("<p>", "\n<p>")),
        has_role=role is not None,
        published_at=dt.datetime.fromtimestamp(int(raw["created_at_i"]), dt.UTC),
    )


def wants(post: HackerNewsPost) -> bool:
    """An open remote developer post that mentions Python (docs/hackernews.md)."""
    if _CLOSED.search(post.header):
        return False
    if post.location is not None:
        if not _REMOTE.search(post.location):
            return False
    elif not _REMOTE.search(post.description):
        return False
    if not re.search(sources_titles.DEFAULT_KEYWORDS, post.description, re.IGNORECASE):
        return False
    if post.has_role:
        if not re.search(sources_titles.DEFAULT_TITLE, post.title, re.IGNORECASE):
            return False
        if re.search(sources_titles.DEFAULT_EXCLUDE_TITLE, post.title, re.IGNORECASE):
            return False
    return True


@dataclasses.dataclass
class HackerNewsStats:
    """Counters of one import, for the sync status line."""

    thread: str | None = None
    posts: int = 0
    new: int = 0
    known: int = 0
    skipped: int = 0
    error: str | None = None


def latest_thread(data: dict[str, t.Any]) -> dict[str, t.Any] | None:
    hits = [h for h in data.get("hits") or [] if str(h.get("title", "")).startswith(THREAD_TITLE)]
    return max(hits, key=lambda h: int(h.get("created_at_i") or 0), default=None)


def _add(session: sa_orm.Session, post: HackerNewsPost) -> None:
    import job_radar.auto_ignore as auto_ignore

    score = scoring.score_job(post.title, post.location)
    opportunity = models.Opportunity(
        title=post.title[:255],
        company=dedup.get_or_create_company(session, post.company or f"Unknown (HN {post.id})"),
        location=post.location,
        score=score.value,
        score_reasons=[r.as_dict() for r in score.reasons],
    )
    record = models.SourceRecord(
        source_type=models.SourceType.HACKERNEWS,
        external_id=str(post.id),
        url=post.url,
        opportunity=opportunity,
    )
    session.add(record)
    session.flush()
    if post.description:
        dedup.add_snapshot(session, record, post.description)
    session.refresh(opportunity)
    auto_ignore.apply_rules(session, opportunity)


def import_hackernews(session: sa_orm.Session, client: Client) -> HackerNewsStats:
    stats = HackerNewsStats()
    try:
        thread = latest_thread(client.threads())
        if thread is None:
            stats.error = f"no '{THREAD_TITLE}' thread found"
            return stats
        stats.thread = str(thread.get("title"))
        data = client.item(int(thread["objectID"]))
    except HackerNewsError as e:
        stats.error = str(e)
        return stats
    for raw in data.get("children") or []:
        # deleted or dead
        if not raw.get("text"):
            continue
        post = parse_post(raw)
        stats.posts += 1
        if not wants(post):
            stats.skipped += 1
        elif known := session.scalar(
            sa.select(models.SourceRecord.id).where(
                models.SourceRecord.source_type == models.SourceType.HACKERNEWS,
                models.SourceRecord.external_id == str(post.id),
            )
        ):
            stats.known += 1
            # Edited on HN, or read by an older version (#243): the new text becomes current,
            # and so does the location from the header (#256).
            record = session.get(models.SourceRecord, known)
            if post.description:
                dedup.add_snapshot(session, record, post.description)
            if record.opportunity.location != post.location:
                record.opportunity.location = post.location
        else:
            _add(session, post)
            stats.new += 1
    session.commit()
    return stats
