"""Opt-in: LinkedIn's public (guest) job search and job pages, without login.

``import_public_search`` reads the first page of the search behind each new alert email;
``import_public_descriptions`` reads the public page of To review vacancies without a
description. Plain HTTP without login, cookies or a browser, with random pauses between
requests; the run stops for the day on the first sign of blocking, and every request is
logged in ``LinkedInFetch``. See docs/linkedin-public-search.md and the LinkedIn rule in
CLAUDE.md.

Usage:
    import job_radar.sources.linkedin.public as linkedin_public
"""

import collections.abc as cabc
import dataclasses
import datetime as dt
import random
import re
import threading
import time
import urllib.error
import urllib.parse as urllib_parse
import urllib.request

import bs4
import sqlalchemy as sa
import sqlalchemy.orm as sa_orm

import job_radar.models as models
import job_radar.sources.linkedin.alerts as linkedin_alerts

GUEST_SEARCH = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
GUEST_POSTING = "https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{job_id}"
PAGE_SIZE = 10
ALERT_MAX_AGE = dt.timedelta(days=3)
MIN_RANGE, MAX_RANGE = 3600, 7 * 24 * 3600
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/140.0 Safari/537.36"
)
_BLOCK_URL = re.compile(r"authwall|/login|checkpoint|/uas/", re.IGNORECASE)
_CHALLENGE = re.compile(r"challenge|captcha", re.IGNORECASE)
_JOB_URN = re.compile(r"jobPosting:(\d+)")

# (status, final url after redirects, body)
HttpGet = cabc.Callable[[str], tuple[int, str, str]]
PAUSE_KEY = "linkedin"
STOPPED_BY_YOU = "stopped by you"


@dataclasses.dataclass(frozen=True)
class PauseSettings:
    """Random pause between guest requests, seconds; edited on Settings (#205)."""

    pause_from: float = 10.0
    pause_to: float = 40.0


def load_pause(session: sa_orm.Session) -> PauseSettings:
    row = session.get(models.AppSetting, PAUSE_KEY)
    if row is None or not isinstance(row.value, dict):
        return PauseSettings()
    known = {k: v for k, v in row.value.items() if k in PauseSettings.__dataclass_fields__}
    return PauseSettings(**known)


def save_pause(session: sa_orm.Session, value: PauseSettings) -> None:
    row = session.get(models.AppSetting, PAUSE_KEY) or models.AppSetting(key=PAUSE_KEY)
    row.value = dataclasses.asdict(value)
    session.add(row)
    session.commit()


@dataclasses.dataclass(frozen=True)
class Search:
    """The search behind an alert: keywords, geo id and time range."""

    keywords: str
    geo_id: str | None
    # guest f_TPR, e.g. "r86400"
    time_range: str

    def url(self, start: int) -> str:
        params = {"keywords": self.keywords, "f_TPR": self.time_range, "start": start}
        if self.geo_id:
            params["geoId"] = self.geo_id
        return f"{GUEST_SEARCH}?{urllib_parse.urlencode(params)}"


def search_from_alert(html: str, *, now: dt.datetime) -> Search | None:
    """The search behind an alert email; only keywords, geoId and period are kept."""
    for a in bs4.BeautifulSoup(html, "html.parser").find_all("a", href=True):
        url = urllib_parse.urlparse(a["href"])
        if "/jobs/search" not in url.path:
            continue
        query = urllib_parse.parse_qs(url.query)
        if not query.get("keywords"):
            continue
        period = (query.get("f_TPR") or [""])[0]
        if match := re.fullmatch(r"a(\d+)-", period):
            seconds = int(now.timestamp()) - int(match.group(1))
        elif match := re.fullmatch(r"r(\d+)", period):
            seconds = int(match.group(1))
        else:
            seconds = 86400
        seconds = min(MAX_RANGE, max(MIN_RANGE, seconds))
        return Search(query["keywords"][0], (query.get("geoId") or [None])[0], f"r{seconds}")
    return None


def _text(card, selector: str) -> str | None:
    node = card.select_one(selector)
    return node.get_text(" ", strip=True) if node else None


def parse_search_page(html: str) -> list[linkedin_alerts.AlertJob]:
    jobs = []
    for card in bs4.BeautifulSoup(html, "html.parser").select("[data-entity-urn]"):
        match = _JOB_URN.search(card["data-entity-urn"])
        title = _text(card, ".base-search-card__title")
        company = _text(card, ".base-search-card__subtitle")
        if not (match and title and company):
            continue
        jobs.append(
            linkedin_alerts.AlertJob(
                job_id=match.group(1),
                title=title,
                company=company,
                location=_text(card, ".job-search-card__location"),
                salary=_text(card, ".job-search-card__salary-info"),
                easy_apply=None,
            )
        )
    return jobs


class Blocked(Exception):
    """LinkedIn showed a sign of blocking (authwall, 429, 999); stop for the day."""

    def __init__(self, http_status: int | None, reason: str):
        super().__init__(reason)
        self.http_status = http_status


class Stopped(Exception):
    """The user pressed Stop; raised before the next request."""


class FetchError(Exception):
    """One request failed for another reason; the run goes on."""

    def __init__(self, http_status: int | None):
        super().__init__(f"HTTP {http_status}")
        self.http_status = http_status


def urllib_get(url: str) -> tuple[int, str, str]:
    # A fresh request every time: no cookie jar, no session, no login.
    request = urllib.request.Request(
        url, headers={"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.9"}
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return response.status, response.geturl(), response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, url, ""


class PublicSearchClient:
    """Plain HTTPS client for the guest search and job pages, with pauses."""

    def __init__(
        self,
        http_get: HttpGet = urllib_get,
        sleep: cabc.Callable[[float], object] | None = None,
        pause: tuple[float, float] = (PauseSettings.pause_from, PauseSettings.pause_to),
        rand: cabc.Callable[[float, float], float] = random.uniform,
        stop: threading.Event | None = None,
        on_wait: cabc.Callable[[float], None] | None = None,
    ):
        """``stop``: Stop pressed in the web UI; it also cuts the pause short.
        ``on_wait`` hears each pause before it starts, for the progress line."""
        self._get, self._sleep, self._pause, self._rand = http_get, sleep, pause, rand
        self._stop, self._on_wait = stop, on_wait
        self._requested = False

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

    def _request(self, url: str, has_content: cabc.Callable[[str], bool]) -> tuple[int, str]:
        """(http status, body); raises Blocked on any sign of blocking, Stopped on Stop."""
        if self._stop is not None and self._stop.is_set():
            raise Stopped
        if self._requested:
            self._wait()
            if self._stop is not None and self._stop.is_set():
                raise Stopped
        self._requested = True
        status, final_url, body = self._get(url)
        if status in (429, 999):
            raise Blocked(status, f"HTTP {status}")
        if _BLOCK_URL.search(urllib_parse.urlparse(final_url).path or "") or _BLOCK_URL.search(
            final_url
        ):
            raise Blocked(status, "redirected to login")
        if status != 200:
            raise FetchError(status)
        if not has_content(body) and _CHALLENGE.search(body):
            raise Blocked(status, "challenge page")
        return status, body

    def fetch(self, search: Search, start: int) -> tuple[int, str]:
        return self._request(search.url(start), lambda body: bool(parse_search_page(body)))

    def fetch_posting(self, job_id: str) -> tuple[int, str]:
        url = GUEST_POSTING.format(job_id=job_id)
        return self._request(url, lambda body: parse_job_posting(body) is not None)


@dataclasses.dataclass(frozen=True)
class JobPosting:
    """A vacancy's public page: title, company, location and description."""

    title: str
    company: str
    location: str | None
    description: str


def parse_job_posting(html: str) -> JobPosting | None:
    """A vacancy's public page: title, company, location and the full description."""
    soup = bs4.BeautifulSoup(html, "html.parser")
    markup = soup.select_one(".show-more-less-html__markup")
    if markup is None:
        return None
    description = markup.get_text("\n", strip=True)
    if not description:
        return None
    return JobPosting(
        title=_text(soup, ".top-card-layout__title") or "",
        company=_text(soup, ".topcard__org-name-link") or "",
        location=_text(soup, ".topcard__flavor--bullet"),
        description=description,
    )


@dataclasses.dataclass
class PublicSearchStats:
    """Counters of one guest search run."""

    searches: int = 0
    requests: int = 0
    jobs: int = 0
    new: int = 0
    blocked: bool = False
    errors: int = 0


def _day_start(now: dt.datetime) -> dt.datetime:
    return now.astimezone(dt.UTC).replace(hour=0, minute=0, second=0, microsecond=0)


def import_public_search(
    session: sa_orm.Session,
    client: PublicSearchClient,
    *,
    now: dt.datetime | None = None,
    max_searches: int = 5,
    max_jobs: int = 25,
) -> PublicSearchStats:
    now = now or dt.datetime.now(dt.UTC)
    stats = PublicSearchStats()
    today = _day_start(now)
    fetches_today = sa.select(models.LinkedInFetch).where(models.LinkedInFetch.at >= today)
    if session.scalar(fetches_today.where(models.LinkedInFetch.status == "blocked").limit(1)):
        stats.blocked = True
        return stats
    done_today = session.scalar(
        sa.select(sa.func.count())
        .select_from(models.LinkedInFetch)
        .where(
            models.LinkedInFetch.kind == "search",
            models.LinkedInFetch.at >= today,
            models.LinkedInFetch.start == 0,
            models.LinkedInFetch.status == "ok",
        )
    )
    searched = sa.select(models.LinkedInFetch.email_message_id).where(
        models.LinkedInFetch.kind == "search",
        models.LinkedInFetch.start == 0,
        models.LinkedInFetch.status == "ok",
    )
    alerts = session.scalars(
        sa.select(models.EmailMessage)
        .where(
            models.EmailMessage.from_addr.contains(linkedin_alerts.ALERT_SENDER),
            models.EmailMessage.body_html.is_not(None),
            models.EmailMessage.sent_at >= now - ALERT_MAX_AGE,
            models.EmailMessage.id.not_in(searched),
        )
        .order_by(models.EmailMessage.sent_at.desc())
    ).all()

    for alert in alerts:
        if done_today + stats.searches >= max_searches:
            break
        search = search_from_alert(alert.body_html or "", now=now)
        if search is None:
            continue
        stats.searches += 1
        taken = 0
        start = 0
        while taken < max_jobs:
            log = models.LinkedInFetch(
                at=now,
                email_message_id=alert.id,
                keywords=search.keywords,
                geo_id=search.geo_id,
                start=start,
            )
            session.add(log)
            stats.requests += 1
            try:
                log.http_status, body = client.fetch(search, start)
            except Blocked as e:
                log.status, log.http_status, log.detail = "blocked", e.http_status, str(e)
                stats.blocked = True
                session.commit()
                return stats
            except FetchError as e:
                log.status, log.http_status, log.detail = "error", e.http_status, str(e)
                stats.errors += 1
                break
            page = parse_search_page(body)
            log.status, log.jobs = "ok", len(page)
            for job in page[: max_jobs - taken]:
                taken += 1
                stats.jobs += 1
                if linkedin_alerts.add_linkedin_job(
                    session, job, models.SourceType.LINKEDIN_SEARCH, remote_known=False
                ):
                    stats.new += 1
            session.commit()
            if len(page) < PAGE_SIZE:
                break
            start += PAGE_SIZE
    session.commit()
    return stats


@dataclasses.dataclass
class DescriptionStats:
    """Counters and the stop reason of one public description run."""

    # vacancies this run will try, within the daily limit
    total: int = 0
    requests: int = 0
    saved: int = 0
    errors: int = 0
    blocked: bool = False
    # why LinkedIn blocked, e.g. "HTTP 429"
    reason: str | None = None
    # by the user
    stopped: bool = False
    limit_reached: bool = False


def _without_description(
    session: sa_orm.Session,
) -> list[tuple[models.Opportunity, models.SourceRecord]]:
    """To review vacancies seen on LinkedIn that have no description yet, best score first."""
    import job_radar.sources.linkedin.alerts as linkedin_alerts

    rows = session.scalars(
        sa.select(models.Opportunity).where(models.Opportunity.state == models.OpportunityState.NEW)
    ).all()
    out = []
    for o in rows:
        if any(r.snapshots for r in o.source_records):
            continue
        record = next(
            (r for r in o.source_records if r.source_type in linkedin_alerts.LINKEDIN_SOURCES), None
        )
        if record is not None and record.external_id.isdigit():
            out.append((o, record))
    return sorted(out, key=lambda pair: -(pair[0].score if pair[0].score is not None else -1))


def import_public_descriptions(
    session: sa_orm.Session,
    client: PublicSearchClient,
    *,
    now: dt.datetime | None = None,
    max_per_day: int = 100,
    on_progress: cabc.Callable[[DescriptionStats], None] | None = None,
) -> DescriptionStats:
    """Description from the public job page for To review vacancies without one. Opt-in; see
    docs/linkedin-public-search.md#descriptions-public-job-page. Every request is logged with
    its vacancy, time and, when not ok, the reason (#205)."""
    import job_radar.dedup as dedup
    import job_radar.review.auto_ignore as review_auto_ignore

    def stamp() -> dt.datetime:
        return now or dt.datetime.now(dt.UTC)

    def progress() -> None:
        if on_progress:
            on_progress(stats)

    stats = DescriptionStats()
    today = _day_start(stamp())
    block = session.scalar(
        sa.select(models.LinkedInFetch)
        .where(models.LinkedInFetch.at >= today, models.LinkedInFetch.status == "blocked")
        .limit(1)
    )
    if block is not None:
        stats.blocked, stats.reason = True, block.detail or _status_text(block.http_status)
        return stats
    done = session.scalar(
        sa.select(sa.func.count())
        .select_from(models.LinkedInFetch)
        .where(
            models.LinkedInFetch.kind == "description",
            models.LinkedInFetch.at >= today,
            models.LinkedInFetch.status != "stopped",
        )
    )
    waiting = _without_description(session)
    todo = waiting[: max(0, max_per_day - done)]
    stats.total, stats.limit_reached = len(todo), len(todo) < len(waiting)
    progress()
    for opportunity, record in todo:
        log = models.LinkedInFetch(
            kind="description", job_id=record.external_id, start=0, opportunity_id=opportunity.id
        )
        try:
            log.http_status, body = client.fetch_posting(record.external_id)
        except Stopped:
            log.at, log.status, log.detail = stamp(), "stopped", STOPPED_BY_YOU
            session.add(log)
            session.commit()
            stats.stopped = True
            break
        except Blocked as e:
            log.status, log.http_status, log.detail = "blocked", e.http_status, str(e)
            stats.blocked, stats.reason = True, str(e)
        except FetchError as e:
            log.status, log.http_status, log.detail = "error", e.http_status, str(e)
            stats.errors += 1
        else:
            posting = parse_job_posting(body)
            if posting is None:
                log.status, log.detail = "error", "no description on page"
                stats.errors += 1
            else:
                log.status, log.jobs = "ok", 1
                dedup.add_snapshot(session, record, posting.description)
                stats.saved += 1
                session.refresh(opportunity)
                review_auto_ignore.apply_rules(session, opportunity)
        log.at = stamp()
        session.add(log)
        stats.requests += 1
        session.commit()
        progress()
        if stats.blocked:
            break
    return stats


def _status_text(http_status: int | None) -> str:
    return f"HTTP {http_status}" if http_status else "blocked"
