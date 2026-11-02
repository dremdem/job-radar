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
import threading
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from sqlalchemy.orm import Session

from ..models import LinkedInFetch
from .capture import import_capture
from .public import _without_description

READER = (Path(__file__).parent.parent / "web" / "capture_read.js").read_text().strip()
JOB_URL = "https://www.linkedin.com/jobs/view/{job_id}/"
MAX_PER_RUN = 50
LOAD_WAIT = (2.5, 5.0)  # seconds after opening a page, as a person waits for it to load
EXPAND_WAIT = (0.8, 1.6)
MAX_ERRORS_IN_A_ROW = 3  # the page layout probably changed: stop rather than keep going
# Only these Playwright MCP tools are ever called (#247): no clicks by reference, no typing.
ALLOWED_TOOLS = frozenset({"browser_tabs", "browser_navigate", "browser_evaluate"})

_BLOCK_URL = re.compile(
    r"authwall|/login|/signup|checkpoint|/uas/|challenge|captcha|security-verification",
    re.IGNORECASE,
)
_BLOCK_TITLE = re.compile(
    r"security verification|sign in|log in|sign up|captcha|let's do a quick|verify", re.IGNORECASE
)
# With the token the extension connects its own connect page to the client (#250).
_CONNECT_PAGE = re.compile(r"^chrome-extension://[a-p]{32}/connect\.html")
_TAB_LINE = re.compile(r"^- \d+:.*\]\((\S+)\)\s*$")
_JOB_ID = re.compile(r"currentJobId=(\d+)|/jobs/view/(?:[^/?#]*-)?(\d+)")

# Where the page is, how it loaded, and what it is: read before anything else is done.
PROBE = """() => {
  const nav = performance.getEntriesByType('navigation')[0];
  return { href: location.href, title: document.title,
           status: nav && nav.responseStatus ? nav.responseStatus : null };
}"""
# Unfold the description: only a "more" button inside the description box itself, never
# anything else on the page (e.g. "Show more options" opens the job's action menu).
EXPAND = """() => {
  const boxes = document.querySelectorAll(
    '[componentkey^="JobDetails_AboutTheJob"], [data-testid="expandable-text-box"], ' +
    '#job-details, .jobs-description__content, .jobs-description-content__text, ' +
    '.jobs-description');
  const more = /^\\s*(\\u2026\\s*)?((see|show)\\s+)?more\\s*$/i;
  for (const box of boxes) {
    let scope = box;
    for (let up = 0; up < 3 && scope; up++, scope = scope.parentElement) {
      for (const b of scope.querySelectorAll('button')) {
        const label = (b.innerText || b.getAttribute('aria-label') || '').trim();
        const aria = b.getAttribute('aria-label') || '';
        if (more.test(label) && !/option/i.test(label + ' ' + aria)) {
          b.click();
          return label;
        }
      }
    }
  }
  return '';
}"""


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
class FillStats:
    total: int = 0  # vacancies this run will try
    requests: int = 0  # pages opened
    saved: int = 0
    errors: int = 0
    blocked: bool = False
    reason: str | None = None  # why it stopped early: "HTTP 429", "authwall", "another page"
    stopped: bool = False  # by the user


def job_id_of(href: str) -> str | None:
    if m := _JOB_ID.search(href or ""):
        return m.group(1) or m.group(2)
    return None


def block_reason(probe: dict[str, Any]) -> str | None:
    """Why LinkedIn is not showing the vacancy, or None when it is."""
    status = probe.get("status")
    if status in (429, 999):
        return f"HTTP {status}"
    href, title = str(probe.get("href") or ""), str(probe.get("title") or "")
    if _BLOCK_URL.search(href):
        return f"verification page ({href.split('?')[0]})"
    if _BLOCK_TITLE.search(title):
        return f"verification page ({title})"
    return None


def parse_tabs(text: str) -> list[str]:
    """Tab URLs from `browser_tabs list`: '- 0: (current) [Title](url)' per line."""
    return [m.group(1) for line in text.splitlines() if (m := _TAB_LINE.match(line.strip()))]


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

    def log(job_id: str, opportunity_id: int, status: str, detail: str | None = None) -> None:
        session.add(
            LinkedInFetch(kind="chrome", job_id=job_id, start=0, opportunity_id=opportunity_id,
                          status=status, detail=detail, at=datetime.now(UTC),
                          jobs=1 if status == "ok" else 0)
        )  # fmt: skip
        session.commit()

    todo = _without_description(session)[:max_per_run]
    stats.total = len(todo)
    progress()
    tab_url: str | None = None  # the page our own tab should be on; None: no tab yet
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
            job_id = record.external_id
            url = JOB_URL.format(job_id=job_id)
            if tab_url is None and reuse:
                await browser.navigate(url)
            elif tab_url is None:
                await browser.open_tab(url)
            else:
                # The extension follows one tab; make sure it is still ours before leaving it.
                here = await browser.evaluate(PROBE)
                if job_id_of(str(here.get("href"))) != job_id_of(tab_url):
                    stats.blocked, stats.reason = True, "another page in the tab: stopped"
                    say(f"stopped: the tab shows {here.get('href')}, not our vacancy")
                    break
                await browser.navigate(url)
            tab_url = url
            stats.requests += 1
            await wait(LOAD_WAIT)
            probe = await browser.evaluate(PROBE)
            if reason := block_reason(probe):
                stats.blocked, stats.reason = True, reason
                log(job_id, opportunity.id, "blocked", reason)
                say(f"LinkedIn: {reason} — stopped; the tab is left open for you to see")
                tab_url = None  # leave it open
                break
            if job_id_of(str(probe.get("href"))) != job_id:
                stats.blocked, stats.reason = True, "another page in the tab: stopped"
                log(job_id, opportunity.id, "error", f"tab shows {probe.get('href')}")
                say(f"stopped: expected job {job_id}, the tab shows {probe.get('href')}")
                tab_url = None
                break
            if await browser.evaluate(EXPAND):
                await wait(EXPAND_WAIT)
            payload = await browser.evaluate(f"() => ({READER})()")
            detail = (payload or {}).get("detail") or {}
            if str(detail.get("job_id")) != job_id:  # the page changed under us
                stats.blocked, stats.reason = True, "another page in the tab: stopped"
                log(job_id, opportunity.id, "error", "the page changed while reading")
                say(f"stopped: the page changed while reading job {job_id}")
                tab_url = None
                break
            result = import_capture(session, own_payload(payload, job_id))
            say(result.vacancy or f"job {job_id}")
            if result.descriptions:
                stats.saved += 1
                errors_in_a_row = 0
                log(job_id, opportunity.id, "ok")
            else:
                stats.errors += 1
                errors_in_a_row += 1
                log(job_id, opportunity.id, "error", "no description on the page")
            progress()
            if errors_in_a_row >= MAX_ERRORS_IN_A_ROW:
                stats.blocked, stats.reason = True, f"{errors_in_a_row} pages without a description"
                say("stopped: pages in a row had no description; the layout may have changed")
                break
    finally:
        progress()
        if tab_url is not None:
            with contextlib.suppress(Exception):  # the tab may be gone already
                await browser.close_tab()
    return stats


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
    import shlex

    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    argv = shlex.split(command)
    params = StdioServerParameters(command=argv[0], args=argv[1:], env=env)
    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        await asyncio.wait_for(session.initialize(), timeout=90)
        return await fill(McpBrowser(session))
