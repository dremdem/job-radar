"""Chrome fill (#247): descriptions of To review vacancies, read in the user's own Chrome.

Playwright MCP in `--extension` mode drives the user's Chrome. The run opens a tab of its
own, goes through LinkedIn vacancies that have no description one at a time, expands the
folded description, and reads the page with the bookmarklet's reader (web/capture_read.js)
into the same `import_capture`. Limits and reasons: docs/adr/0001-chrome-description-fill.md,
docs/linkedin-chrome-fill.md.
"""

import asyncio
import contextlib
import json
import random
import re
import shlex
import threading
import time
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..auto_ignore import mark_unavailable
from ..models import (
    FILL_PROBLEM,
    AppSetting,
    CompanyEvent,
    LinkedInFetch,
    Opportunity,
    OpportunityState,
    SourceRecord,
    SourceType,
)
from .capture import import_capture
from .public import PauseSettings, _without_description

READER = (Path(__file__).parent.parent / "web" / "capture_read.js").read_text().strip()
JOB_URL = "https://www.linkedin.com/jobs/view/{job_id}/"
HH_URL = "https://hh.ru/vacancy/{job_id}"  # hh.ru vacancies too (#260, ADR 0002)
MAX_PER_RUN = 50
# Its own pause between pages, apart from the guest one (#252): by hand the user opens a
# vacancy and clicks the bookmarklet within seconds.
PAUSE_KEY = "linkedin_chrome"
DEFAULT_PAUSE = PauseSettings(2.0, 4.0)
# browser_navigate itself waits for the page's `load`; then the description is polled for,
# a short random step at a time, at most LOAD_TIMEOUT seconds (#254).
LOAD_STEP = (0.4, 0.8)
LOAD_TIMEOUT = 10.0
EXPAND_WAIT = (0.6, 1.0)  # the click comes 0.3 s after EXPAND returns; unfolding is in-page
MAX_ERRORS_IN_A_ROW = 3  # the browser or the layout is at fault: stop rather than keep going
PAGE_TIMEOUT = 60.0  # one vacancy, start to end; a normal one takes under 20 s (#272)
# Only these Playwright MCP tools are ever called (#247): no clicks by reference, no typing.
ALLOWED_TOOLS = frozenset({"browser_tabs", "browser_navigate", "browser_evaluate"})
# Added to the command unless it sets them itself (#254). Without them every navigate
# sends back a full aria snapshot of the (big) page, which the fill never reads, and every
# evaluate waits 0.5 s, then for each request the page started meanwhile (up to 5 s:
# LinkedIn's beacons and long-poll), then 0.5 s more.
SPEED_FLAGS = {"--snapshot-mode": "none", "--timeout-settle": "100"}

_BLOCK_URL = re.compile(
    r"authwall|/login|/signup|checkpoint|/uas/|challenge|captcha|security-verification"
    r"|hh\.ru/account/",
    re.IGNORECASE,
)
_BLOCK_TITLE = re.compile(
    r"security verification|sign in|log in|sign up|captcha|let's do a quick|verify"
    r"|не робот|подтвердите",
    re.IGNORECASE,
)
_HH_PAGE = re.compile(r"^https?://(?:[\w-]+\.)?hh\.(?:ru|kz|by|uz)/vacancy/(\d+)")
# With the token the extension connects its own connect page to the client (#250).
_CONNECT_PAGE = re.compile(r"^chrome-extension://[a-p]{32}/connect\.html")
_TAB_LINE = re.compile(r"^- \d+:.*\]\((\S+)\)\s*$")
_JOB_ID = re.compile(r"currentJobId=(\d+)|/jobs/view/(?:[^/?#]*-)?(\d+)")

# The description's box on a job page; EXPAND looks for "more" only inside it.
DESCRIPTION_BOXES = (
    '[componentkey^="JobDetails_AboutTheJob"], [data-testid="expandable-text-box"], '
    "#job-details, .jobs-description__content, .jobs-description-content__text, "
    ".jobs-description"
)
# Where the page is, how it loaded, what it is, and whether the description is there yet:
# read before anything else is done.
PROBE = """() => {
  const nav = performance.getEntriesByType('navigation')[0];
  const boxes = [...document.querySelectorAll('__BOXES__, [data-qa="vacancy-description"]')];
  const ready = boxes.some((b) => b.innerText.trim() !== '');
  const text = document.body ? document.body.innerText : '';
  return { href: location.href, title: document.title,
           status: nav && nav.responseStatus ? nav.responseStatus : null,
           ready: ready,
           unavailable: !ready && /(^|\\.)hh\\.(ru|kz|by|uz)$/.test(location.hostname)
             && /вам\\s+недоступна\\s+эта\\s+вакансия/i.test(text) };
}""".replace("__BOXES__", DESCRIPTION_BOXES)
# Unfold the description: only a "more" button inside the description box itself, never
# anything else on the page (e.g. "Show more options" opens the job's action menu).
# The click comes just after the evaluate has returned (#258): LinkedIn sends requests on
# the click, and MCP would wait for them up to 5 s before answering.
EXPAND = """() => {
  const boxes = document.querySelectorAll('__BOXES__');
  const more = /^\\s*(\\u2026\\s*)?((see|show)\\s+)?more\\s*$/i;
  for (const box of boxes) {
    let scope = box;
    for (let up = 0; up < 3 && scope; up++, scope = scope.parentElement) {
      for (const b of scope.querySelectorAll('button')) {
        const label = (b.innerText || b.getAttribute('aria-label') || '').trim();
        const aria = b.getAttribute('aria-label') || '';
        if (more.test(label) && !/option/i.test(label + ' ' + aria)) {
          setTimeout(() => b.click(), CLICK_DELAY_MS);
          return label;
        }
      }
    }
  }
  return '';
}""".replace("__BOXES__", DESCRIPTION_BOXES).replace("CLICK_DELAY_MS", "300")


class Browser(Protocol):
    """The few things the fill asks of the browser."""

    async def list_tabs(self) -> list[str]: ...
    async def open_tab(self, url: str) -> None: ...
    async def navigate(self, url: str) -> None: ...
    async def evaluate(self, function: str) -> Any: ...
    async def close_tab(self) -> None: ...


class BrowserError(RuntimeError):
    pass


@dataclass
class SourceProgress:
    """How far a run got with one site (#272)."""

    label: str
    total: int = 0
    opened: int = 0
    saved: int = 0
    problems: int = 0


@dataclass
class FillStats:
    total: int = 0  # vacancies this run will try
    requests: int = 0  # pages opened
    saved: int = 0
    errors: int = 0
    blocked: bool = False
    reason: str | None = None  # why it stopped early: "HTTP 429", "authwall", "another page"
    stopped: bool = False  # by the user
    sources: dict[str, SourceProgress] = field(default_factory=dict)  # "linkedin", "hh"
    failed: list[dict[str, Any]] = field(default_factory=list)  # which vacancies, why (#274)


def load_chrome_pause(session: Session) -> PauseSettings:
    row = session.get(AppSetting, PAUSE_KEY)
    if row is None or not isinstance(row.value, dict):
        return DEFAULT_PAUSE
    known = {k: v for k, v in row.value.items() if k in PauseSettings.__dataclass_fields__}
    return PauseSettings(**{**asdict(DEFAULT_PAUSE), **known})


def save_chrome_pause(session: Session, value: PauseSettings) -> None:
    row = session.get(AppSetting, PAUSE_KEY) or AppSetting(key=PAUSE_KEY)
    row.value = asdict(value)
    session.add(row)
    session.commit()


def job_id_of(href: str) -> str | None:
    if m := _JOB_ID.search(href or ""):
        return m.group(1) or m.group(2)
    return None


def _is_linkedin(href: str) -> bool:
    """True for linkedin.com and its subdomains, not for a URL that only mentions it."""
    host = urlsplit(href or "").hostname or ""
    return host == "linkedin.com" or host.endswith(".linkedin.com")


def page_of(href: str) -> tuple[str, str] | None:
    """("hh", id) for an hh.ru vacancy page, ("linkedin", id) for a LinkedIn one."""
    if m := _HH_PAGE.match(href or ""):
        return ("hh", m.group(1))
    if _is_linkedin(href) and (job_id := job_id_of(href)):
        return ("linkedin", job_id)
    return None


def block_reason(probe: dict[str, Any]) -> str | None:
    """Why the site is not showing the vacancy, or None when it is."""
    status = probe.get("status")
    if status in (403, 429, 999):
        return f"HTTP {status}"
    href, title = str(probe.get("href") or ""), str(probe.get("title") or "")
    if _BLOCK_URL.search(href):
        return f"verification page ({href.split('?')[0]})"
    if _BLOCK_TITLE.search(title):
        return f"verification page ({title})"
    return None


def unavailable_reason(probe: dict[str, Any]) -> str | None:
    """Why this one vacancy cannot be read, when the site itself is fine (#272): an hh.ru
    page saying it is not available to you (HTTP 403 on the vacancy's own URL), or a 404/410
    on a vacancy page. Anything else that fails is for block_reason."""
    if page_of(str(probe.get("href") or "")) is None:
        return None
    status = probe.get("status")
    if status in (404, 410):
        return f"vacancy not available any more (HTTP {status})"
    if probe.get("unavailable"):
        return "vacancy not available to you" + (f" (HTTP {status})" if status else "")
    return None


def parse_tabs(text: str) -> list[str]:
    """Tab URLs from `browser_tabs list`: '- 0: (current) [Title](url)' per line."""
    return [m.group(1) for line in text.splitlines() if (m := _TAB_LINE.match(line.strip()))]


def mcp_argv(command: str) -> list[str]:
    """The Playwright MCP command line, with SPEED_FLAGS unless it sets them itself."""
    argv = shlex.split(command)
    for flag, value in SPEED_FLAGS.items():
        if not any(a == flag or a.startswith(flag + "=") for a in argv):
            argv += [flag, value]
    return argv


def own_payload(payload: dict[str, Any], job_id: str) -> dict[str, Any]:
    """Only the vacancy itself: the "similar jobs" on its page are not imported."""
    cards = [c for c in payload.get("cards") or [] if str(c.get("job_id")) == job_id]
    return {**payload, "cards": cards}


Sleep = Callable[[float], Awaitable[None]]


async def fill_descriptions(
    session: Session,
    browser: Browser,
    *,
    pause: tuple[float, float],
    stop: threading.Event | None = None,
    max_per_run: int = MAX_PER_RUN,
    sleep: Sleep = asyncio.sleep,
    on_wait: Callable[[float], None] | None = None,
    on_progress: Callable[[FillStats], None] | None = None,
    echo: Callable[[str], None] | None = None,
) -> FillStats:
    stop = stop or threading.Event()
    stats = FillStats()

    def say(line: str) -> None:
        if echo:
            echo(line)

    def progress() -> None:
        if on_progress:
            on_progress(stats)

    async def wait(span: tuple[float, float]) -> None:
        await sleep(random.uniform(*span))

    async def human_pause() -> bool:
        """The pause between pages, cut short by Stop. False when stopped."""
        seconds = random.uniform(*pause)
        if on_wait:
            on_wait(seconds)
        waited = 0.0
        while waited < seconds:
            if stop.is_set():
                return False
            step = min(1.0, seconds - waited)
            await sleep(step)
            waited += step
        return not stop.is_set()

    async def loaded() -> dict[str, Any]:
        """The probe once the description is on the page, a block page shows, or time is
        up: navigate already waited for `load`, LinkedIn fills the page after that."""
        waited = 0.0
        while True:
            probe = await browser.evaluate(PROBE)
            final = probe.get("ready") or unavailable_reason(probe) or block_reason(probe)
            if final or waited >= LOAD_TIMEOUT:
                return probe
            step = random.uniform(*LOAD_STEP)
            await sleep(step)
            waited += step

    def log(
        opportunity: Opportunity, job_id: str, url: str, status: str, detail: str | None = None
    ) -> None:
        session.add(
            LinkedInFetch(kind="chrome", job_id=job_id, start=0, opportunity_id=opportunity.id,
                          status=status, detail=detail and detail[:255], at=datetime.now(UTC),
                          jobs=1 if status == "ok" else 0)
        )  # fmt: skip
        if status != "ok":
            _mark(session, opportunity, status, detail or status)
            stats.failed.append(
                {"opportunity_id": opportunity.id, "title": opportunity.title, "url": url,
                 "site": "hh.ru" if "hh.ru" in url else "LinkedIn", "reason": detail or status}
            )  # fmt: skip
        session.commit()

    def problem(source: SourceProgress) -> None:
        nonlocal errors_in_a_row
        stats.errors += 1
        source.problems += 1
        errors_in_a_row += 1

    async def visit(opportunity: Opportunity, record: SourceRecord, url: str) -> bool:
        """One vacancy, start to end. False when the whole run has to stop."""
        nonlocal tab_url, attempted, uncertain, errors_in_a_row
        job_id = record.external_id
        hh = record.source_type == SourceType.HH
        source = stats.sources["hh" if hh else "linkedin"]
        site = source.label
        expected = ("hh" if hh else "linkedin", job_id)
        stats.requests += 1
        source.opened += 1
        progress()
        started = time.monotonic()
        if tab_url is None and reuse:
            attempted = url
            await browser.navigate(url)
        elif tab_url is None:
            attempted = url
            await browser.open_tab(url)
        else:
            # The extension follows one tab; make sure it is still ours before leaving it.
            here = str((await browser.evaluate(PROBE)).get("href"))
            ours = page_of(here) in (page_of(tab_url), page_of(attempted or tab_url))
            if not ours and not (uncertain and here.startswith("chrome-error:")):
                stats.blocked, stats.reason = True, "another page in the tab: stopped"
                say(f"stopped: the tab shows {here}, not our vacancy")
                return False
            attempted = url
            await browser.navigate(url)
        tab_url = url
        uncertain = False
        opened = time.monotonic()
        probe = await loaded()
        ready = time.monotonic()
        if reason := unavailable_reason(probe):
            problem(source)
            log(opportunity, job_id, url, "unavailable", reason)
            say(f"{site}: vacancy {job_id}: {reason}; marked, going on")
            return True
        if reason := block_reason(probe):
            source.problems += 1
            stats.blocked, stats.reason = True, reason
            log(opportunity, job_id, url, "blocked", reason)
            say(f"{site}: {reason} — stopped; the tab is left open for you to see")
            tab_url = None  # leave it open
            return False
        if page_of(str(probe.get("href"))) != expected:
            source.problems += 1
            stats.blocked, stats.reason = True, "another page in the tab: stopped"
            log(opportunity, job_id, url, "error", f"tab shows {probe.get('href')}")
            say(f"stopped: expected job {job_id}, the tab shows {probe.get('href')}")
            tab_url = None
            return False
        if not hh and await browser.evaluate(EXPAND):  # hh.ru shows it all (#260)
            await wait(EXPAND_WAIT)
        expanded = time.monotonic()
        payload = await browser.evaluate(f"() => ({READER})()")
        detail = (payload or {}).get("detail") or {}
        if str(detail.get("job_id")) != job_id:  # the page changed under us
            source.problems += 1
            stats.blocked, stats.reason = True, "another page in the tab: stopped"
            log(opportunity, job_id, url, "error", "the page changed while reading")
            say(f"stopped: the page changed while reading job {job_id}")
            tab_url = None
            return False
        result = import_capture(session, own_payload(payload, job_id))
        timing = (
            f"open {opened - started:.1f} s · load {ready - opened:.1f} s"
            f" · expand {expanded - ready:.1f} s · read {time.monotonic() - expanded:.1f} s"
        )
        say(f"{result.vacancy or f'job {job_id}'} ({timing})")
        if result.descriptions:
            stats.saved += 1
            source.saved += 1
            errors_in_a_row = 0
            log(opportunity, job_id, url, "ok", timing)
        else:
            problem(source)
            log(opportunity, job_id, url, "error", "no description on the page")
        return True

    todo = _todo(session)[:max_per_run]
    stats.total = len(todo)
    for key, label in (("linkedin", "LinkedIn"), ("hh", "hh.ru")):
        count = sum((record.source_type == SourceType.HH) == (key == "hh") for _, record in todo)
        if count:
            stats.sources[key] = SourceProgress(label, total=count)
    progress()
    tab_url: str | None = None  # the page our own tab should be on; None: no tab yet
    attempted: str | None = None  # the page last asked for; it may have loaded after all
    uncertain = False  # the last vacancy failed: the tab may show the browser's error page
    # The extension's connect page is the run's own tab: use it, and close it at the end,
    # so nothing is left open (#250). Any other tab is the user's: open a new one instead.
    try:
        tabs = await browser.list_tabs() if todo else []
    except Exception:  # unknown: be safe and open a tab of our own
        tabs = []
    reuse = len(tabs) == 1 and bool(_CONNECT_PAGE.match(tabs[0]))
    errors_in_a_row = 0
    try:
        for n, (opportunity, record) in enumerate(todo):
            if stop.is_set() or (n and not await human_pause()):
                stats.stopped = True
                break
            hh = record.source_type == SourceType.HH
            url = (HH_URL if hh else JOB_URL).format(job_id=record.external_id)
            # One vacancy's trouble stays that vacancy's (#272): its own time limit, and
            # anything it raises is written down on it.
            try:
                go_on = await asyncio.wait_for(visit(opportunity, record, url), PAGE_TIMEOUT)
            except Exception as e:  # a browser or import error of this page
                session.rollback()  # or the failed flush poisons everything after it
                reason = (
                    f"no answer from the browser in {PAGE_TIMEOUT:.0f} s"
                    if isinstance(e, TimeoutError)
                    else f"{type(e).__name__}: {e}"
                )
                uncertain = True
                problem(stats.sources["hh" if hh else "linkedin"])
                log(opportunity, record.external_id, url, "error", reason)
                say(f"job {record.external_id}: {reason}; marked, going on")
                go_on = True
            progress()
            if not go_on:
                break
            if errors_in_a_row >= MAX_ERRORS_IN_A_ROW:
                stats.blocked, stats.reason = True, f"{errors_in_a_row} vacancies in a row failed"
                say("stopped: vacancies in a row failed; the browser or the layout may be at fault")
                break
    finally:
        progress()
        if tab_url is not None:
            with contextlib.suppress(Exception):  # the tab may be gone already
                await browser.close_tab()
    return stats


def _mark(session: Session, opportunity: Opportunity, status: str, reason: str) -> None:
    """Flag an unavailable vacancy (#276); write any other problem into its event log, once
    per distinct problem (#272)."""
    if status == "unavailable":
        mark_unavailable(session, opportunity)
        return
    comment = f"{opportunity.title}: {reason}"
    known = session.scalar(
        select(CompanyEvent.id).where(
            CompanyEvent.opportunity_id == opportunity.id,
            CompanyEvent.kind == FILL_PROBLEM,
            CompanyEvent.comment == comment,
        )
    )
    if known is None:
        session.add(
            CompanyEvent(
                company_id=opportunity.company_id,
                opportunity_id=opportunity.id,
                kind=FILL_PROBLEM,
                at=datetime.now(UTC),
                comment=comment,
            )
        )


def _todo(session: Session) -> list[tuple[Opportunity, SourceRecord]]:
    """LinkedIn and hh.ru vacancies in To review without a description, best score first.
    One marked unavailable or not accepting waits until you clear its badge (#276, #278)."""
    closed = Opportunity.unavailable_at.is_not(None) | Opportunity.not_accepting_at.is_not(None)
    skip = set(session.scalars(select(Opportunity.id).where(closed)))
    hh = []
    for o in session.scalars(select(Opportunity).where(Opportunity.state == OpportunityState.NEW)):
        if any(r.snapshots for r in o.source_records):
            continue
        record = next((r for r in o.source_records if r.source_type == SourceType.HH), None)
        if record is not None and record.external_id.isdigit():
            hh.append((o, record))
    linkedin = _without_description(session)
    seen = {o.id for o, _ in linkedin}
    both = linkedin + [pair for pair in hh if pair[0].id not in seen]
    both = [pair for pair in both if pair[0].id not in skip]
    return sorted(both, key=lambda pair: -(pair[0].score if pair[0].score is not None else -1))


def _text(result: Any) -> str:
    items = getattr(result, "content", None) or []
    return "\n".join(c.text for c in items if getattr(c, "text", None))


def parse_result(text: str) -> Any:
    """The value from a Playwright MCP answer: '### Result\\n<json>\\n### Ran Playwright code…'."""
    m = re.search(r"### Result\s*\n(.*?)(?:\n### |\Z)", text, re.S)
    if not m:
        raise BrowserError(f"unexpected answer from the browser: {text[:200]}")
    body = m.group(1).strip()
    if body.startswith("```"):
        body = body.strip("`").removeprefix("json").strip()
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        return body


class McpBrowser:
    """The user's Chrome through Playwright MCP (`--extension`), over stdio."""

    def __init__(self, session: Any):
        self.session = session

    async def _call(self, tool: str, args: dict[str, Any]) -> str:
        if tool not in ALLOWED_TOOLS:
            raise BrowserError(f"tool {tool!r} is not allowed")
        result = await asyncio.wait_for(self.session.call_tool(tool, args), timeout=120)
        text = _text(result)
        if getattr(result, "isError", False):
            raise BrowserError(f"{tool}: {text[:300]}")
        return text

    async def list_tabs(self) -> list[str]:
        return parse_tabs(await self._call("browser_tabs", {"action": "list"}))

    async def open_tab(self, url: str) -> None:
        await self._call("browser_tabs", {"action": "new", "url": url})

    async def navigate(self, url: str) -> None:
        await self._call("browser_navigate", {"url": url})

    async def evaluate(self, function: str) -> Any:
        return parse_result(await self._call("browser_evaluate", {"function": function}))

    async def close_tab(self) -> None:
        await self._call("browser_tabs", {"action": "close"})


async def run_with_chrome(
    command: str, env: dict[str, str], fill: Callable[[Browser], Awaitable[FillStats]]
) -> FillStats:
    """Start Playwright MCP once, connect to the extension once, run the fill."""
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    argv = mcp_argv(command)
    params = StdioServerParameters(command=argv[0], args=argv[1:], env=env)
    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        await asyncio.wait_for(session.initialize(), timeout=90)
        return await fill(McpBrowser(session))
