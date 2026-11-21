"""Chrome fill (#247): descriptions of To review vacancies, read in the user's own Chrome.

Playwright MCP in ``--extension`` mode drives the user's Chrome. The run opens a tab of
its own, goes through LinkedIn and hh.ru vacancies that have no description one at a
time, expands the folded description, and reads the page with the bookmarklet's reader
(sources/capture_read.js) into the same ``import_capture``. It stops at once on any sign of a
block (``block_reason``) and marks a vacancy the site says is gone
(``unavailable_reason``). ``McpBrowser`` talks to Playwright MCP over stdio. Limits and
reasons: docs/adr/0001-chrome-description-fill.md, docs/linkedin-chrome-fill.md.

Usage:
    import job_radar.sources.linkedin.chrome as linkedin_chrome
"""

import asyncio
import collections.abc as cabc
import contextlib
import dataclasses
import datetime as dt
import json
import pathlib
import random
import re
import shlex
import threading
import time
import typing as t
import urllib.parse as urllib_parse

import sqlalchemy as sa
import sqlalchemy.orm as sa_orm

import job_radar.models as models
import job_radar.review.auto_ignore as review_auto_ignore
import job_radar.sources.linkedin.capture as linkedin_capture
import job_radar.sources.linkedin.public as linkedin_public

READER = (pathlib.Path(__file__).parent.parent / "capture_read.js").read_text().strip()
JOB_URL = "https://www.linkedin.com/jobs/view/{job_id}/"
# hh.ru vacancies too (#260, ADR 0002)
HH_URL = "https://hh.ru/vacancy/{job_id}"
MAX_PER_RUN = 50
# Its own pause between pages, apart from the guest one (#252): by hand the user opens a
# vacancy and clicks the bookmarklet within seconds.
PAUSE_KEY = "linkedin_chrome"
DEFAULT_PAUSE = linkedin_public.PauseSettings(2.0, 4.0)
# browser_navigate itself waits for the page's `load`; then the description is polled for,
# a short random step at a time, at most LOAD_TIMEOUT seconds (#254).
LOAD_STEP = (0.4, 0.8)
LOAD_TIMEOUT = 10.0
# the click comes 0.3 s after EXPAND returns; unfolding is in-page
EXPAND_WAIT = (0.6, 1.0)
# the browser or the layout is at fault: stop rather than keep going
MAX_ERRORS_IN_A_ROW = 3
# one vacancy, start to end; a normal one takes under 20 s (#272)
PAGE_TIMEOUT = 60.0
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
# Playwright MCP 0.0.84 connects with no tab at all; the first `browser_tabs list` then opens
# a blank one in the extension's tab group (#63).
_BLANK_PAGE = "about:blank"
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


class Browser(t.Protocol):
    """The few things the fill asks of the browser."""

    async def list_tabs(self) -> list[str]: ...
    async def open_tab(self, url: str) -> None: ...
    async def navigate(self, url: str) -> None: ...
    async def evaluate(self, function: str) -> t.Any: ...
    async def close_tab(self) -> None: ...


class BrowserError(RuntimeError):
    """The browser or Playwright MCP failed; the run stops."""

    pass


@dataclasses.dataclass
class SourceProgress:
    """How far a run got with one site (#272)."""

    label: str
    total: int = 0
    opened: int = 0
    saved: int = 0
    problems: int = 0


@dataclasses.dataclass
class FillStats:
    """Counters and the stop reason of one Chrome fill run."""

    # vacancies this run will try
    total: int = 0
    # pages opened
    requests: int = 0
    saved: int = 0
    errors: int = 0
    blocked: bool = False
    # why it stopped early: "HTTP 429", "authwall", "another page"
    reason: str | None = None
    # by the user
    stopped: bool = False
    # "linkedin", "hh"
    sources: dict[str, SourceProgress] = dataclasses.field(default_factory=dict)
    # which vacancies, why (#274)
    failed: list[dict[str, t.Any]] = dataclasses.field(default_factory=list)


def load_chrome_pause(session: sa_orm.Session) -> linkedin_public.PauseSettings:
    row = session.get(models.AppSetting, PAUSE_KEY)
    if row is None or not isinstance(row.value, dict):
        return DEFAULT_PAUSE
    known = {
        k: v
        for k, v in row.value.items()
        if k in linkedin_public.PauseSettings.__dataclass_fields__
    }
    return linkedin_public.PauseSettings(**{**dataclasses.asdict(DEFAULT_PAUSE), **known})


def save_chrome_pause(session: sa_orm.Session, value: linkedin_public.PauseSettings) -> None:
    row = session.get(models.AppSetting, PAUSE_KEY) or models.AppSetting(key=PAUSE_KEY)
    row.value = dataclasses.asdict(value)
    session.add(row)
    session.commit()


def job_id_of(href: str) -> str | None:
    if m := _JOB_ID.search(href or ""):
        return m.group(1) or m.group(2)
    return None


def _is_linkedin(href: str) -> bool:
    """True for linkedin.com and its subdomains, not for a URL that only mentions it."""
    host = urllib_parse.urlsplit(href or "").hostname or ""
    return host == "linkedin.com" or host.endswith(".linkedin.com")


def page_of(href: str) -> tuple[str, str] | None:
    """("hh", id) for an hh.ru vacancy page, ("linkedin", id) for a LinkedIn one."""
    if m := _HH_PAGE.match(href or ""):
        return ("hh", m.group(1))
    if _is_linkedin(href) and (job_id := job_id_of(href)):
        return ("linkedin", job_id)
    return None


def block_reason(probe: dict[str, t.Any]) -> str | None:
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


def unavailable_reason(probe: dict[str, t.Any]) -> str | None:
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


def own_payload(payload: dict[str, t.Any], job_id: str) -> dict[str, t.Any]:
    """Only the vacancy itself: the "similar jobs" on its page are not imported."""
    cards = [c for c in payload.get("cards") or [] if str(c.get("job_id")) == job_id]
    return {**payload, "cards": cards}


Sleep = cabc.Callable[[float], cabc.Awaitable[None]]


async def fill_descriptions(
    session: sa_orm.Session,
    browser: Browser,
    *,
    pause: tuple[float, float],
    stop: threading.Event | None = None,
    max_per_run: int = MAX_PER_RUN,
    sleep: Sleep = asyncio.sleep,
    on_wait: cabc.Callable[[float], None] | None = None,
    on_progress: cabc.Callable[[FillStats], None] | None = None,
    echo: cabc.Callable[[str], None] | None = None,
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

    async def loaded() -> dict[str, t.Any]:
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
        opportunity: models.Opportunity,
        job_id: str,
        url: str,
        status: str,
        detail: str | None = None,
    ) -> None:
        session.add(
            models.LinkedInFetch(
                kind="chrome",
                job_id=job_id,
                start=0,
                opportunity_id=opportunity.id,
                status=status,
                detail=detail and detail[:255],
                at=dt.datetime.now(dt.UTC),
                jobs=1 if status == "ok" else 0,
            )
        )
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

    async def visit(opportunity: models.Opportunity, record: models.SourceRecord, url: str) -> bool:
        """One vacancy, start to end. False when the whole run has to stop."""
        nonlocal tab_url, attempted, uncertain, errors_in_a_row, own_tab, keep_open
        job_id = record.external_id
        hh = record.source_type == models.SourceType.HH
        source = stats.sources["hh" if hh else "linkedin"]
        site = source.label
        expected = ("hh" if hh else "linkedin", job_id)
        stats.requests += 1
        source.opened += 1
        progress()
        started = time.monotonic()
        if tab_url is None and own_tab:
            attempted = url
            await browser.navigate(url)
        elif tab_url is None:
            attempted = url
            await browser.open_tab(url)
            own_tab = True
        else:
            # The extension follows one tab; make sure it is still ours before leaving it.
            here = str((await browser.evaluate(PROBE)).get("href"))
            ours = page_of(here) in (page_of(tab_url), page_of(attempted or tab_url))
            if not ours and not (uncertain and here.startswith("chrome-error:")):
                stats.blocked, stats.reason = True, "another page in the tab: stopped"
                say(f"stopped: the tab shows {here}, not our vacancy")
                keep_open = True
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
            keep_open = True
            return False
        if page_of(str(probe.get("href"))) != expected:
            source.problems += 1
            stats.blocked, stats.reason = True, "another page in the tab: stopped"
            log(opportunity, job_id, url, "error", f"tab shows {probe.get('href')}")
            say(f"stopped: expected job {job_id}, the tab shows {probe.get('href')}")
            keep_open = True
            return False
        # hh.ru shows it all (#260)
        if not hh and await browser.evaluate(EXPAND):
            await wait(EXPAND_WAIT)
        expanded = time.monotonic()
        payload = await browser.evaluate(f"() => ({READER})()")
        detail = (payload or {}).get("detail") or {}
        # the page changed under us
        if str(detail.get("job_id")) != job_id:
            source.problems += 1
            stats.blocked, stats.reason = True, "another page in the tab: stopped"
            log(opportunity, job_id, url, "error", "the page changed while reading")
            say(f"stopped: the page changed while reading job {job_id}")
            keep_open = True
            return False
        result = linkedin_capture.import_capture(session, own_payload(payload, job_id))
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
        count = sum(
            (record.source_type == models.SourceType.HH) == (key == "hh") for _, record in todo
        )
        if count:
            stats.sources[key] = SourceProgress(label, total=count)
    progress()
    # the page our own tab should be on; None: no tab yet
    tab_url: str | None = None
    # the page last asked for; it may have loaded after all
    attempted: str | None = None
    # the last vacancy failed: the tab may show the browser's error page
    uncertain = False
    # The extension's connect page (#250), or the blank tab Playwright MCP opened for the
    # list itself (#63), is the run's own tab: use it, and close it at the end, so nothing is
    # left open. Any other tab is the user's: open a new one instead.
    try:
        tabs = await browser.list_tabs() if todo else []
    except Exception:
        # unknown: be safe and open a tab of our own
        tabs = []
    # the run has a tab of its own, to use and to close at the end
    own_tab = len(tabs) == 1 and (tabs[0] == _BLANK_PAGE or bool(_CONNECT_PAGE.match(tabs[0])))
    # a block or another page in it: left open for the user to see
    keep_open = False
    errors_in_a_row = 0
    try:
        for n, (opportunity, record) in enumerate(todo):
            if stop.is_set() or (n and not await human_pause()):
                stats.stopped = True
                break
            hh = record.source_type == models.SourceType.HH
            url = (HH_URL if hh else JOB_URL).format(job_id=record.external_id)
            # One vacancy's trouble stays that vacancy's (#272): its own time limit, and
            # anything it raises is written down on it.
            try:
                go_on = await asyncio.wait_for(visit(opportunity, record, url), PAGE_TIMEOUT)
            except Exception as e:
                # a browser or import error of this page
                # or the failed flush poisons everything after it
                session.rollback()
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
        # also after errors (#63): the tab may still show a failed page
        if own_tab and not keep_open:
            # the tab may be gone already
            with contextlib.suppress(Exception):
                await browser.close_tab()
    return stats


def _mark(
    session: sa_orm.Session, opportunity: models.Opportunity, status: str, reason: str
) -> None:
    """Flag an unavailable vacancy (#276); write any other problem into its event log, once
    per distinct problem (#272)."""
    if status == "unavailable":
        review_auto_ignore.mark_unavailable(session, opportunity)
        return
    comment = f"{opportunity.title}: {reason}"
    known = session.scalar(
        sa.select(models.CompanyEvent.id).where(
            models.CompanyEvent.opportunity_id == opportunity.id,
            models.CompanyEvent.kind == models.FILL_PROBLEM,
            models.CompanyEvent.comment == comment,
        )
    )
    if known is None:
        session.add(
            models.CompanyEvent(
                company_id=opportunity.company_id,
                opportunity_id=opportunity.id,
                kind=models.FILL_PROBLEM,
                at=dt.datetime.now(dt.UTC),
                comment=comment,
            )
        )


def _todo(session: sa_orm.Session) -> list[tuple[models.Opportunity, models.SourceRecord]]:
    """LinkedIn and hh.ru vacancies in To review without a description, best score first.
    One marked unavailable or not accepting waits until you clear its badge (#276, #278)."""
    closed = models.Opportunity.unavailable_at.is_not(
        None
    ) | models.Opportunity.not_accepting_at.is_not(None)
    skip = set(session.scalars(sa.select(models.Opportunity.id).where(closed)))
    hh = []
    for o in session.scalars(
        sa.select(models.Opportunity).where(models.Opportunity.state == models.OpportunityState.NEW)
    ):
        if any(r.snapshots for r in o.source_records):
            continue
        record = next((r for r in o.source_records if r.source_type == models.SourceType.HH), None)
        if record is not None and record.external_id.isdigit():
            hh.append((o, record))
    linkedin = linkedin_public._without_description(session)
    seen = {o.id for o, _ in linkedin}
    both = linkedin + [pair for pair in hh if pair[0].id not in seen]
    both = [pair for pair in both if pair[0].id not in skip]
    return sorted(both, key=lambda pair: -(pair[0].score if pair[0].score is not None else -1))


def _text(result: t.Any) -> str:
    items = getattr(result, "content", None) or []
    return "\n".join(c.text for c in items if getattr(c, "text", None))


def parse_result(text: str) -> t.Any:
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

    def __init__(self, session: t.Any):
        self.session = session

    async def _call(self, tool: str, args: dict[str, t.Any]) -> str:
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

    async def evaluate(self, function: str) -> t.Any:
        return parse_result(await self._call("browser_evaluate", {"function": function}))

    async def close_tab(self) -> None:
        await self._call("browser_tabs", {"action": "close"})


async def run_with_chrome(
    command: str, env: dict[str, str], fill: cabc.Callable[[Browser], cabc.Awaitable[FillStats]]
) -> FillStats:
    """Start Playwright MCP once, connect to the extension once, run the fill."""
    import mcp
    import mcp.client.stdio as client_stdio

    argv = mcp_argv(command)
    params = mcp.StdioServerParameters(command=argv[0], args=argv[1:], env=env)
    async with (
        client_stdio.stdio_client(params) as (read, write),
        mcp.ClientSession(read, write) as session,
    ):
        await asyncio.wait_for(session.initialize(), timeout=90)
        return await fill(McpBrowser(session))
