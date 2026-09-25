"""Opt-in: first page of LinkedIn's public (guest) job search for each new alert search.

Plain HTTP without login, cookies or a browser. Stops for the day on the first sign of
blocking. See docs/linkedin-public-search.md and the LinkedIn rule in CLAUDE.md.
"""

import re
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlencode, urlparse

from bs4 import BeautifulSoup
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import EmailMessage, LinkedInFetch, SourceType
from .alerts import ALERT_SENDER, AlertJob, add_linkedin_job

GUEST_SEARCH = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
PAGE_SIZE = 10
ALERT_MAX_AGE = timedelta(days=3)
MIN_RANGE, MAX_RANGE = 3600, 7 * 24 * 3600
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/140.0 Safari/537.36"
)
_BLOCK_URL = re.compile(r"authwall|/login|checkpoint|/uas/", re.IGNORECASE)
_CHALLENGE = re.compile(r"challenge|captcha", re.IGNORECASE)
_JOB_URN = re.compile(r"jobPosting:(\d+)")

# (status, final url after redirects, body)
HttpGet = Callable[[str], tuple[int, str, str]]


@dataclass(frozen=True)
class Search:
    keywords: str
    geo_id: str | None
    time_range: str  # guest f_TPR, e.g. "r86400"

    def url(self, start: int) -> str:
        params = {"keywords": self.keywords, "f_TPR": self.time_range, "start": start}
        if self.geo_id:
            params["geoId"] = self.geo_id
        return f"{GUEST_SEARCH}?{urlencode(params)}"


def search_from_alert(html: str, *, now: datetime) -> Search | None:
    """The search behind an alert email; only keywords, geoId and period are kept."""
    for a in BeautifulSoup(html, "html.parser").find_all("a", href=True):
        url = urlparse(a["href"])
        if "/jobs/search" not in url.path:
            continue
        query = parse_qs(url.query)
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


def parse_search_page(html: str) -> list[AlertJob]:
    jobs = []
    for card in BeautifulSoup(html, "html.parser").select("[data-entity-urn]"):
        match = _JOB_URN.search(card["data-entity-urn"])
        title = _text(card, ".base-search-card__title")
        company = _text(card, ".base-search-card__subtitle")
        if not (match and title and company):
            continue
        jobs.append(
            AlertJob(
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
    def __init__(self, http_status: int | None, reason: str):
        super().__init__(reason)
        self.http_status = http_status


class FetchError(Exception):
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
    def __init__(
        self,
        http_get: HttpGet = urllib_get,
        sleep: Callable[[float], None] = time.sleep,
        delay: float = 3.0,
    ):
        self._get, self._sleep, self._delay = http_get, sleep, delay
        self._requested = False

    def fetch(self, search: Search, start: int) -> tuple[int, str]:
        """(http status, body); raises Blocked on any sign of blocking."""
        if self._requested:
            self._sleep(self._delay)
        self._requested = True
        status, final_url, body = self._get(search.url(start))
        if status in (429, 999):
            raise Blocked(status, f"HTTP {status}")
        if _BLOCK_URL.search(urlparse(final_url).path or "") or _BLOCK_URL.search(final_url):
            raise Blocked(status, "redirected to login")
        if status != 200:
            raise FetchError(status)
        if not parse_search_page(body) and _CHALLENGE.search(body):
            raise Blocked(status, "challenge page")
        return status, body


@dataclass
class PublicSearchStats:
    searches: int = 0
    requests: int = 0
    jobs: int = 0
    new: int = 0
    blocked: bool = False
    errors: int = 0


def _day_start(now: datetime) -> datetime:
    return now.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)


def import_public_search(
    session: Session,
    client: PublicSearchClient,
    *,
    now: datetime | None = None,
    max_searches: int = 5,
    max_jobs: int = 25,
) -> PublicSearchStats:
    now = now or datetime.now(UTC)
    stats = PublicSearchStats()
    today = _day_start(now)
    fetches_today = select(LinkedInFetch).where(LinkedInFetch.at >= today)
    if session.scalar(fetches_today.where(LinkedInFetch.status == "blocked").limit(1)):
        stats.blocked = True
        return stats
    done_today = session.scalar(
        select(func.count())
        .select_from(LinkedInFetch)
        .where(LinkedInFetch.at >= today, LinkedInFetch.start == 0, LinkedInFetch.status == "ok")
    )
    searched = select(LinkedInFetch.email_message_id).where(
        LinkedInFetch.start == 0, LinkedInFetch.status == "ok"
    )
    alerts = session.scalars(
        select(EmailMessage)
        .where(
            EmailMessage.from_addr.contains(ALERT_SENDER),
            EmailMessage.body_html.is_not(None),
            EmailMessage.sent_at >= now - ALERT_MAX_AGE,
            EmailMessage.id.not_in(searched),
        )
        .order_by(EmailMessage.sent_at.desc())
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
            log = LinkedInFetch(
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
                log.status, log.http_status = "blocked", e.http_status
                stats.blocked = True
                session.commit()
                return stats
            except FetchError as e:
                log.status, log.http_status = "error", e.http_status
                stats.errors += 1
                break
            page = parse_search_page(body)
            log.status, log.jobs = "ok", len(page)
            for job in page[: max_jobs - taken]:
                taken += 1
                stats.jobs += 1
                if add_linkedin_job(session, job, SourceType.LINKEDIN_SEARCH, remote_known=False):
                    stats.new += 1
            session.commit()
            if len(page) < PAGE_SIZE:
                break
            start += PAGE_SIZE
    session.commit()
    return stats
