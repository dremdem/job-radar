"""Clients for the official public job board APIs of Greenhouse, Lever, Ashby and Workable.

``AtsClient.read_board(provider, slug)`` makes one plain HTTPS request (two for a
Greenhouse board whose company name is wanted) and returns a ``Board`` with its
``Posting`` list: id, title, location, link and the description as plain text. Every
provider has its own parser, because the response shapes differ (Greenhouse and Workable
wrap the jobs in ``jobs[]``, Lever returns a bare array). Errors are never "no jobs":
a 404 raises ``BoardNotFound``; 429, 5xx, a timeout, a non-JSON body, an unexpected shape
or a body over ``MAX_BYTES`` raise ``AtsError``; only a valid empty list is an empty
board. The client pauses between requests and stops at its request budget. Nothing here
touches LinkedIn. See docs/ats.md.

Usage:
    import job_radar.sources.ats.boards as ats_boards
"""

import collections.abc as cabc
import dataclasses
import html
import json
import random
import time
import typing as t
import urllib.error
import urllib.parse
import urllib.request

import job_radar.sources.http as sources_http

PROVIDERS = ("greenhouse", "lever", "ashby", "workable")
TIMEOUT = 30
# a big board with full descriptions is a few MB; anything above is not a job board
MAX_BYTES = 20 * 1024 * 1024

# (http status, body); a non-200 status is returned, not raised.
HttpGet = cabc.Callable[[str], tuple[int, str]]


class AtsError(Exception):
    """A board could not be read: network, 429/5xx, not JSON, unexpected shape, too big."""


class BoardNotFound(AtsError):
    """The API has no board with this slug (HTTP 404)."""


class BudgetSpent(Exception):
    """The run's request budget is used up; raised before the next request."""


def urllib_get(url: str) -> tuple[int, str]:
    # A fresh request every time: no cookies, no session.
    request = urllib.request.Request(
        url, headers={"User-Agent": sources_http.USER_AGENT, "Accept": "application/json"}
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            body = response.read(MAX_BYTES + 1)
            if len(body) > MAX_BYTES:
                raise AtsError(f"response over {MAX_BYTES // (1024 * 1024)} MB")
            return response.status, body.decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, ""
    except (urllib.error.URLError, TimeoutError) as e:
        raise AtsError(str(getattr(e, "reason", e))) from e


@dataclasses.dataclass(frozen=True)
class Posting:
    """One job on an employer's board, as the vendor's public API gives it."""

    provider: str
    slug: str
    id: str
    title: str
    # None when the API gives no location at all: unknown, not "anywhere"
    location: str | None
    url: str
    text: str
    remote: bool | None = None

    @property
    def external_id(self) -> str:
        """``SourceRecord.external_id``: job ids are unique only within one board."""
        return f"{self.slug}:{self.id}"

    def as_candidate(self) -> dict[str, str]:
        return {
            "provider": self.provider,
            "slug": self.slug,
            "id": self.id,
            "title": self.title,
            "location": self.location or "",
            "url": self.url,
        }


@dataclasses.dataclass(frozen=True)
class Board:
    """What one read of a board gave: its company name (when the API has one) and jobs."""

    provider: str
    slug: str
    name: str | None
    postings: list[Posting]


def _quote(slug: str) -> str:
    return urllib.parse.quote(slug, safe="")


def jobs_url(provider: str, slug: str) -> str:
    s = _quote(slug)
    return {
        "greenhouse": f"https://boards-api.greenhouse.io/v1/boards/{s}/jobs?content=true",
        "lever": f"https://api.lever.co/v0/postings/{s}?mode=json",
        "ashby": f"https://api.ashbyhq.com/posting-api/job-board/{s}",
        "workable": f"https://apply.workable.com/api/v1/widget/accounts/{s}?details=true",
    }[provider]


def _text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _join(*parts: str) -> str:
    return "\n\n".join(p for p in (p.strip() for p in parts) if p)


def _jobs_list(data: object, key: str = "jobs") -> list[dict]:
    if isinstance(data, dict) and isinstance(data.get(key), list):
        return [j for j in data[key] if isinstance(j, dict)]
    raise AtsError("unexpected response shape")


def parse_greenhouse(slug: str, data: object) -> list[Posting]:
    """``GET /v1/boards/<slug>/jobs?content=true``: ``content`` is escaped HTML."""
    out = []
    for job in _jobs_list(data):
        location = job.get("location")
        name = _text(location.get("name")) if isinstance(location, dict) else ""
        out.append(
            Posting(
                provider="greenhouse",
                slug=slug,
                id=str(job.get("id", "")),
                title=_text(job.get("title")),
                location=name or None,
                url=_text(job.get("absolute_url")),
                text=sources_http.html_text(html.unescape(_text(job.get("content")))),
            )
        )
    return [p for p in out if p.id and p.title]


def parse_lever(slug: str, data: object) -> list[Posting]:
    """``GET /v0/postings/<slug>?mode=json``: a bare array. The text joins
    ``descriptionPlain``, every ``lists[]`` (its ``text`` heading and HTML ``content``)
    and ``additionalPlain``: the requirements usually live in the lists."""
    if not isinstance(data, list):
        raise AtsError("unexpected response shape")
    out = []
    for job in (j for j in data if isinstance(j, dict)):
        categories = job.get("categories") if isinstance(job.get("categories"), dict) else {}
        lists = [
            _join(_text(item.get("text")), sources_http.html_text(_text(item.get("content"))))
            for item in job.get("lists") or []
            if isinstance(item, dict)
        ]
        workplace = _text(job.get("workplaceType")).lower()
        out.append(
            Posting(
                provider="lever",
                slug=slug,
                id=_text(job.get("id")),
                title=_text(job.get("text")),
                location=_text(categories.get("location")) or None,
                url=_text(job.get("hostedUrl")),
                text=_join(
                    _text(job.get("descriptionPlain")), *lists, _text(job.get("additionalPlain"))
                ),
                remote=(workplace == "remote") if workplace else None,
            )
        )
    return [p for p in out if p.id and p.title]


def parse_ashby(slug: str, data: object) -> list[Posting]:
    """``GET /posting-api/job-board/<slug>``: ``jobs[]``; unlisted jobs are left out."""
    out = []
    for job in _jobs_list(data):
        if job.get("isListed") is False:
            continue
        places = [_text(job.get("location"))] + [
            _text(s.get("location"))
            for s in job.get("secondaryLocations") or []
            if isinstance(s, dict)
        ]
        text = _text(job.get("descriptionPlain")) or sources_http.html_text(
            _text(job.get("descriptionHtml"))
        )
        remote = job.get("isRemote")
        out.append(
            Posting(
                provider="ashby",
                slug=slug,
                id=_text(job.get("id")),
                title=_text(job.get("title")),
                location="; ".join(p for p in places if p) or None,
                url=_text(job.get("jobUrl")),
                text=text,
                remote=remote if isinstance(remote, bool) else None,
            )
        )
    return [p for p in out if p.id and p.title]


def parse_workable(slug: str, data: object) -> list[Posting]:
    """``GET /api/v1/widget/accounts/<slug>?details=true``: ``jobs[]`` with HTML
    ``description``; the job id is its ``shortcode``."""
    out = []
    for job in _jobs_list(data):
        places = [
            ", ".join(p for p in (_text(loc.get("city")), _text(loc.get("country"))) if p)
            for loc in job.get("locations") or []
            if isinstance(loc, dict)
        ] or [", ".join(p for p in (_text(job.get("city")), _text(job.get("country"))) if p)]
        remote = job.get("telecommuting")
        out.append(
            Posting(
                provider="workable",
                slug=slug,
                id=_text(job.get("shortcode")),
                title=_text(job.get("title")),
                location="; ".join(p for p in places if p) or None,
                url=_text(job.get("url")) or _text(job.get("shortlink")),
                text=sources_http.html_text(_text(job.get("description"))),
                remote=remote if isinstance(remote, bool) else None,
            )
        )
    return [p for p in out if p.id and p.title]


PARSERS: dict[str, cabc.Callable[[str, object], list[Posting]]] = {
    "greenhouse": parse_greenhouse,
    "lever": parse_lever,
    "ashby": parse_ashby,
    "workable": parse_workable,
}


class AtsClient:
    """Reads boards with a random pause before every request but the first, and at most
    ``max_requests`` requests per run. Tests pass a fake ``http_get`` and ``sleep``."""

    def __init__(
        self,
        http_get: HttpGet = urllib_get,
        *,
        pause: tuple[float, float] = (1.0, 3.0),
        max_requests: int = 60,
        sleep: cabc.Callable[[float], object] = time.sleep,
        rand: cabc.Callable[[float, float], float] = random.uniform,
    ):
        self._get, self._pause, self._sleep, self._rand = http_get, pause, sleep, rand
        self.max_requests = max_requests
        self.requests = 0

    @property
    def spent(self) -> bool:
        return self.requests >= self.max_requests

    def get_json(self, url: str) -> t.Any:
        if self.spent:
            raise BudgetSpent
        if self.requests:
            self._sleep(self._rand(*self._pause))
        self.requests += 1
        status, body = self._get(url)
        if status == 404:
            raise BoardNotFound("HTTP 404")
        if status != 200:
            raise AtsError(f"HTTP {status}")
        try:
            return json.loads(body)
        except ValueError as e:
            raise AtsError("not JSON") from e

    def read_board(self, provider: str, slug: str, *, want_name: bool = False) -> Board:
        """The board's postings; with ``want_name`` also its company name where the API has
        one (Greenhouse: one more request; Workable: in the same answer)."""
        data = self.get_json(jobs_url(provider, slug))
        postings = PARSERS[provider](slug, data)
        name = None
        if provider == "workable" and isinstance(data, dict):
            name = _text(data.get("name")) or None
        elif provider == "greenhouse" and want_name:
            info = self.get_json(f"https://boards-api.greenhouse.io/v1/boards/{_quote(slug)}")
            name = _text(info.get("name")) if isinstance(info, dict) else None
        return Board(provider=provider, slug=slug, name=name or None, postings=postings)
