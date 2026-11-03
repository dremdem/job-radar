"""Chrome fill (#247) with a fake browser: what it opens, reads, saves, and when it stops."""

import asyncio
import threading

from sqlalchemy import func, select

from job_radar.linkedin.alerts import AlertJob, add_linkedin_job
from job_radar.linkedin.chrome import (
    EXPAND,
    PROBE,
    READER,
    block_reason,
    fill_descriptions,
    parse_result,
)
from job_radar.models import JobSnapshot, LinkedInFetch, Opportunity, SourceType

VISIBLE = "We build payments.\nRequirements:\n- Python, FastAPI"
FOLDED = "\n- PostgreSQL\nFully remote, worldwide."


class FakePage:
    def __init__(
        self, job_id, *, status=200, href=None, title=None, folded=True, text=None, ready_after=0
    ):
        self.job_id = job_id
        self.ready_after = ready_after  # probes before the description box is on the page
        self.status = status
        self.href = href or f"https://www.linkedin.com/jobs/view/{job_id}/"
        self.title = title or f"Senior Python Engineer | Acme {job_id} | LinkedIn"
        self.folded = folded
        self.text = text


class FakeBrowser:
    """One tab, like the extension. `pages` says what LinkedIn shows for each job id."""

    def __init__(self, pages=None, *, after_open=None, tabs=None):
        self.pages = pages or {}
        # What the client sees at start: by default the user's tab picked in the dialog.
        self.tabs = tabs if tabs is not None else ["https://github.com/dremdem/job-radar"]
        self.calls: list[tuple[str, str]] = []
        self.page: FakePage | None = None
        self.after_open = after_open  # e.g. the user clicks elsewhere: replace the page

    def _show(self, url):
        job_id = url.rstrip("/").split("/")[-1]
        self.page = self.pages.get(job_id) or FakePage(job_id)
        if self.after_open:
            self.page = self.after_open(self.page) or self.page

    async def list_tabs(self):
        self.calls.append(("list_tabs", ""))
        return list(self.tabs)

    async def open_tab(self, url):
        self.calls.append(("open_tab", url))
        self._show(url)

    async def navigate(self, url):
        self.calls.append(("navigate", url))
        self._show(url)

    async def close_tab(self):
        self.calls.append(("close_tab", ""))

    async def evaluate(self, function):
        p = self.page
        if function == PROBE:
            self.calls.append(("probe", ""))
            ready = p.ready_after <= 0
            p.ready_after -= 1
            return {"href": p.href, "title": p.title, "status": p.status, "ready": ready}
        if function == EXPAND:
            self.calls.append(("expand", ""))
            if p.folded:
                p.folded = False
                return "… more"
            return ""
        assert READER in function, "only the bookmarklet's own reader reads the page"
        self.calls.append(("read", ""))
        description = p.text if p.text is not None else VISIBLE + ("" if p.folded else FOLDED)
        job_id = p.href.split("/jobs/view/")[-1].strip("/") if "/jobs/view/" in p.href else None
        return {
            "source_url": p.href,
            "cards": [
                {"job_id": p.job_id, "text": f"Senior Python Engineer\nAcme {p.job_id}\nRemote"},
                {"job_id": "777", "text": "Similar job\nOther Co\nBerlin"},  # "similar jobs"
            ],
            "detail": {"job_id": job_id, "page_title": p.title, "description": description},
        }


def vacancy(session, job_id, title="Senior Python Engineer"):
    add_linkedin_job(
        session,
        AlertJob(job_id, title, f"Acme {job_id}", "Remote", None, None),
        SourceType.LINKEDIN_ALERT,
    )
    session.commit()


def run(session, browser, **kw):
    async def no_sleep(_):
        return None

    return asyncio.run(fill_descriptions(session, browser, pause=(0, 0), sleep=no_sleep, **kw))


def descriptions(session):
    return {s.source_record.external_id: s.raw_text for s in session.scalars(select(JobSnapshot))}


def test_folded_description_is_expanded_before_reading(session):
    vacancy(session, "101")
    stats = run(session, FakeBrowser())
    saved = descriptions(session)["101"]
    assert saved == VISIBLE + FOLDED and len(saved) > len(VISIBLE)
    assert (stats.total, stats.requests, stats.saved, stats.errors) == (1, 1, 1, 0)


def test_own_tab_one_page_at_a_time_then_closed(session):
    vacancy(session, "101")
    vacancy(session, "102")
    browser = FakeBrowser()
    run(session, browser)
    kinds = [c[0] for c in browser.calls if c[0] != "list_tabs"]
    assert kinds[0] == "open_tab"  # never the tab you are working in
    assert kinds.count("open_tab") == 1 and kinds.count("navigate") == 1
    assert kinds[-1] == "close_tab"
    urls = [u for k, u in browser.calls if k in ("open_tab", "navigate")]
    assert sorted(urls) == [
        "https://www.linkedin.com/jobs/view/101/",
        "https://www.linkedin.com/jobs/view/102/",
    ]
    # Expanding comes before reading on every page.
    order = [k for k in kinds if k in ("expand", "read")]
    assert order == ["expand", "read", "expand", "read"]


def test_similar_jobs_on_the_page_are_not_imported(session):
    vacancy(session, "101")
    run(session, FakeBrowser())
    assert session.scalar(select(func.count(Opportunity.id))) == 1


def test_blocks_stop_the_run(session):
    for page in (
        FakePage("101", status=429),
        FakePage("101", status=999),
        FakePage("101", href="https://www.linkedin.com/authwall?trk=x"),
        FakePage("101", href="https://www.linkedin.com/checkpoint/challenge/abc"),
        FakePage("101", title="Security Verification | LinkedIn"),
    ):
        session.rollback()
        for row in session.scalars(select(LinkedInFetch)):
            session.delete(row)
        if not session.scalar(select(func.count(Opportunity.id))):
            vacancy(session, "101")
            vacancy(session, "102")
        browser = FakeBrowser({"101": page, "102": FakePage("102")})
        stats = run(session, browser)
        assert stats.blocked and stats.reason, page.__dict__
        opened = [u for k, u in browser.calls if k in ("open_tab", "navigate")]
        assert len(opened) == 1, f"went on after {stats.reason}"
        assert ("read", "") not in browser.calls
        assert ("close_tab", "") not in browser.calls  # left open for you to see
        assert descriptions(session) == {}
        assert session.scalar(select(LinkedInFetch.status)) == "blocked"


def test_another_page_in_the_tab_saves_nothing(session):
    vacancy(session, "101")
    # You clicked another vacancy in the same tab before the run read it.
    browser = FakeBrowser(
        after_open=lambda page: FakePage(
            page.job_id, href="https://www.linkedin.com/jobs/view/555/"
        )
    )
    stats = run(session, browser)
    assert stats.blocked and "another page" in stats.reason
    assert descriptions(session) == {}
    assert ("read", "") not in browser.calls and ("expand", "") not in browser.calls


def test_tab_left_before_the_next_vacancy_stops_before_navigating(session):
    vacancy(session, "101")
    vacancy(session, "102")
    browser = FakeBrowser()
    original = browser.evaluate
    probes = []

    async def evaluate(function):
        result = await original(function)
        if function == PROBE:
            probes.append(1)
            if len(probes) == 2:  # the check before leaving page 1: you are on Gmail now
                gmail = "https://mail.google.com/mail/u/0/"
                return {"href": gmail, "title": "Inbox", "status": 200}
        return result

    browser.evaluate = evaluate
    stats = run(session, browser)
    assert stats.blocked and stats.saved == 1
    assert [k for k, _ in browser.calls].count("navigate") == 0  # your tab is never navigated


def test_rerun_skips_saved_and_does_not_duplicate(session):
    vacancy(session, "101")
    vacancy(session, "102")
    run(session, FakeBrowser(), max_per_run=1)
    assert len(descriptions(session)) == 1
    changed = {n: FakePage(n, text="changed") for n in ("101", "102")}
    browser = FakeBrowser(changed)
    stats = run(session, browser)
    assert stats.total == 1  # continues with what is left
    assert session.scalar(select(func.count(JobSnapshot.id))) == 2
    first = [d for d in descriptions(session).values() if d != "changed"]
    assert len(first) == 1  # the first description is not overwritten
    assert run(session, FakeBrowser()).total == 0


def test_stop_button(session):
    vacancy(session, "101")
    stop = threading.Event()
    stop.set()
    stats = run(session, FakeBrowser(), stop=stop)
    assert stats.stopped and stats.requests == 0


def test_pages_without_description_stop_after_three(session):
    for n in range(5):
        vacancy(session, f"10{n}")
    pages = {f"10{n}": FakePage(f"10{n}", text="") for n in range(5)}
    stats = run(session, FakeBrowser(pages))
    assert stats.blocked and stats.errors == 3 and stats.requests == 3


def test_block_reason_and_parse_result():
    job_page = "https://www.linkedin.com/jobs/view/1/"
    assert block_reason({"href": job_page, "title": "x", "status": 200}) is None
    assert block_reason({"href": "https://www.linkedin.com/uas/login", "title": "", "status": 200})
    text = '### Result\n{\n  "a": 1\n}\n### Ran Playwright code\n```js\nawait page.evaluate()\n```'
    assert parse_result(text) == {"a": 1}
    assert parse_result('### Result\n"… more"\n### Page\n- x') == "… more"


def test_bookmarklet_and_fill_share_one_reader():
    from job_radar.web.app import bookmarklet

    assert READER.startswith("function () {") and "return {" in READER
    assert "window.open" not in READER and ".click(" not in READER
    code = bookmarklet("http://127.0.0.1:8000")
    assert "#job-details" in code and "__READ__" not in code


# --- web -------------------------------------------------------------------


def web(database_url, tmp_path, runner, **extra):
    from fastapi.testclient import TestClient

    from job_radar.config import Settings
    from job_radar.web.app import create_app

    settings = Settings(database_url=database_url, profile=tmp_path / "p.yaml", **extra)
    return TestClient(create_app(settings, start_worker=False, chrome_runner=runner))


def test_chrome_fill_is_off_by_default(engine, database_url, tmp_path):
    with web(database_url, tmp_path, lambda job: None) as client:
        assert "Fill from Chrome" not in client.get("/").text
        assert "JOB_RADAR_LINKEDIN_CHROME=true" in client.post("/chrome-fill").text


def test_button_starts_the_run_shows_progress_and_stops(engine, database_url, tmp_path):
    import time

    release = threading.Event()

    def runner(job):
        job.progress(total=30, requests=7, saved=6, errors=1)
        job.echo("Acme — Engineer: description saved")
        release.wait(5)
        job.finish(stopped=job.stop_event.is_set())

    with web(database_url, tmp_path, runner, linkedin_chrome=True) as client:
        assert "Fill from Chrome" in client.get("/").text
        client.post("/chrome-fill")
        for _ in range(50):
            text = client.get("/chrome-fill/status").text
            if "7 of 30" in text:
                break
            time.sleep(0.05)
        assert "7 of 30 opened" in text and "6 saved" in text and "23 left" in text
        assert "description saved" in text and "/chrome-fill/stop" in text
        client.post("/chrome-fill/stop")
        release.set()
        for _ in range(50):
            text = client.get("/chrome-fill/status").text
            if "stopped by you" in text:
                break
            time.sleep(0.05)
        assert "stopped by you" in text


def test_blocked_run_says_why(engine, database_url, tmp_path):
    import time

    def runner(job):
        job.progress(total=5, requests=1, saved=0, errors=0)
        job.finish(blocked=True, reason="HTTP 429")

    with web(database_url, tmp_path, runner, linkedin_chrome=True) as client:
        client.post("/chrome-fill")
        for _ in range(50):
            text = client.get("/chrome-fill/status").text
            if "HTTP 429" in text:
                break
            time.sleep(0.05)
        assert "stopped: HTTP 429" in text and "until tomorrow" not in text


def test_page_changed_while_reading_saves_nothing(session):
    vacancy(session, "101")
    browser = FakeBrowser()
    original = browser.evaluate

    async def evaluate(function):
        result = await original(function)
        if function == EXPAND:  # you clicked another vacancy right after the check
            browser.page = FakePage("101", href="https://www.linkedin.com/jobs/view/555/")
        return result

    browser.evaluate = evaluate
    stats = run(session, browser)
    assert stats.blocked and descriptions(session) == {}


CONNECT_PAGE = "chrome-extension://mmlmfjhmonkocbjadbfplnigmagldckm/connect.html?mcpRelayUrl=ws"


def test_extension_connect_page_is_used_and_closed_so_nothing_is_left(session):
    """#250: with the token the extension connects its own connect page; reuse it."""
    vacancy(session, "101")
    vacancy(session, "102")
    browser = FakeBrowser(tabs=[CONNECT_PAGE])
    stats = run(session, browser)
    kinds = [k for k, _ in browser.calls]
    assert stats.saved == 2
    assert "open_tab" not in kinds  # no second tab next to the connect page
    assert kinds.count("navigate") == 2 and kinds[-1] == "close_tab"


def test_a_users_tab_is_never_navigated(session):
    cases = (["https://mail.google.com/mail/u/0/"], [CONNECT_PAGE, "https://x.com/"], [])
    for n, tabs in enumerate(cases):
        vacancy(session, f"20{n}")  # one fresh vacancy without a description per case
        browser = FakeBrowser(tabs=tabs)
        assert run(session, browser).saved == 1, tabs
        kinds = [k for k, _ in browser.calls]
        assert kinds.index("open_tab") < kinds.index("read"), tabs
        assert "navigate" not in kinds, tabs


def test_parse_tabs():
    from job_radar.linkedin.chrome import parse_tabs

    text = (
        "### Open tabs\n- 0: (current) [Playwright MCP extension](" + CONNECT_PAGE + ")\n"
        "- 1: [Issues · dremdem/yt_archive](https://github.com/dremdem/yt_archive/issues)\n"
    )
    assert parse_tabs(text) == [CONNECT_PAGE, "https://github.com/dremdem/yt_archive/issues"]
    assert parse_tabs("### Result\nNo open tabs") == []


def test_chrome_fill_has_its_own_pause(engine, database_url, tmp_path, session):
    """#252: 2-4 s by default, saved apart from the guest pause (10-40 s)."""
    from job_radar.linkedin.chrome import load_chrome_pause
    from job_radar.linkedin.public import PauseSettings, load_pause

    assert load_chrome_pause(session) == PauseSettings(2, 4)
    with web(database_url, tmp_path, lambda job: None, linkedin_chrome=True) as client:
        page = client.get("/settings").text
        assert 'name="chrome_from"' in page and 'name="chrome_to"' in page
        client.post("/settings/chrome-fill", data={"chrome_from": "1.5", "chrome_to": "3"})
        assert (
            client.post(
                "/settings/chrome-fill", data={"chrome_from": "5", "chrome_to": "1"}
            ).status_code
            == 422
        )
    session.expire_all()
    assert load_chrome_pause(session) == PauseSettings(1.5, 3)
    assert load_pause(session) == PauseSettings(10, 40)  # the guest pause is untouched


def test_pipeline_uses_the_chrome_pause(monkeypatch, database_url, session):
    from job_radar import pipeline
    from job_radar.config import Settings
    from job_radar.linkedin import chrome
    from job_radar.web.description_job import DescriptionJob

    seen = {}

    async def fake_fill(session, browser, *, pause, **kw):
        seen["pause"] = pause
        return chrome.FillStats()

    async def fake_run(command, env, fill):
        return await fill(object())

    monkeypatch.setattr(chrome, "fill_descriptions", fake_fill)
    monkeypatch.setattr(chrome, "run_with_chrome", fake_run)
    pipeline.chrome_fill_job(Settings(database_url=database_url), DescriptionJob())
    assert seen["pause"] == (2, 4)


# --- speed (#254) ------------------------------------------------------------


def run_timed(session, browser, **kw):
    """Like run(), with the sleeps recorded: the pause between pages is (0, 0) here."""
    slept: list[float] = []

    async def sleep(seconds):
        slept.append(seconds)

    stats = asyncio.run(fill_descriptions(session, browser, pause=(0, 0), sleep=sleep, **kw))
    return stats, slept


def test_no_fixed_load_wait_when_the_description_is_there(session):
    """navigate already waits for the page to load; no 2.5-5 s on top of it (#254)."""
    vacancy(session, "101")
    stats, slept = run_timed(session, FakeBrowser())
    assert stats.saved == 1
    assert sum(slept) < 1.5, slept


def test_waits_until_the_description_appears(session):
    vacancy(session, "101")
    browser = FakeBrowser({"101": FakePage("101", ready_after=3)})
    stats, _slept = run_timed(session, browser)
    assert stats.saved == 1
    kinds = [k for k, _ in browser.calls]
    assert kinds.count("probe") == 4  # three not ready yet, then ready
    assert kinds.index("expand") > max(i for i, k in enumerate(kinds) if k == "probe")


def test_description_that_never_appears_is_waited_for_at_most_the_limit(session):
    from job_radar.linkedin.chrome import LOAD_TIMEOUT

    vacancy(session, "101")
    browser = FakeBrowser({"101": FakePage("101", ready_after=10_000)})
    _stats, slept = run_timed(session, browser)
    assert LOAD_TIMEOUT - 1 <= sum(slept) <= LOAD_TIMEOUT + 2.5, sum(slept)
    assert ("read", "") in browser.calls  # read anyway: the reader decides


def test_block_page_stops_while_waiting_for_the_description(session):
    vacancy(session, "101")
    page = FakePage("101", href="https://www.linkedin.com/authwall?trk=x", ready_after=10_000)
    stats, slept = run_timed(session, FakeBrowser({"101": page}))
    assert stats.blocked and sum(slept) < 1.5


def test_mcp_is_started_without_snapshots_and_with_a_short_settle():
    """Every navigate sent a full aria snapshot of the page; every evaluate waited
    0.5 s + the page's own requests (up to 5 s) + 0.5 s (#254)."""
    from job_radar.config import Settings
    from job_radar.linkedin.chrome import mcp_argv

    argv = mcp_argv(Settings().linkedin_chrome_command)
    assert argv[:3] == ["npx", "-y", "@playwright/mcp@0.0.84"]
    assert "--extension" in argv
    assert argv[argv.index("--snapshot-mode") + 1] == "none"
    assert argv[argv.index("--timeout-settle") + 1] == "100"
    # A custom command keeps its own choice.
    custom = mcp_argv("mcp --extension --timeout-settle=300 --snapshot-mode full")
    assert custom == ["mcp", "--extension", "--timeout-settle=300", "--snapshot-mode", "full"]


def test_step_durations_are_in_the_log(session):
    vacancy(session, "101")
    run_timed(session, FakeBrowser())
    detail = session.scalar(select(LinkedInFetch.detail).where(LinkedInFetch.status == "ok"))
    assert detail and all(word in detail for word in ("open", "load", "read")), detail
