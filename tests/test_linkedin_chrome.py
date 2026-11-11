"""Chrome fill (#247) with a fake browser: what it opens, reads, saves, and when it stops."""

import asyncio
import re
import threading
import time
from dataclasses import asdict
from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select

from job_radar.auto_ignore import UNAVAILABLE_REASON, Rules, clear_unavailable, save_rules
from job_radar.linkedin.alerts import AlertJob, add_linkedin_job
from job_radar.linkedin.chrome import (
    EXPAND,
    PROBE,
    READER,
    BrowserError,
    block_reason,
    fill_descriptions,
    page_of,
    parse_result,
)
from job_radar.models import (
    FILL_PROBLEM,
    CompanyEvent,
    JobSnapshot,
    LinkedInFetch,
    Opportunity,
    OpportunityState,
    SourceRecord,
    SourceType,
)

VISIBLE = "We build payments.\nRequirements:\n- Python, FastAPI"
FOLDED = "\n- PostgreSQL\nFully remote, worldwide."


class FakePage:
    def __init__(
        self, job_id, *, status=200, href=None, title=None, folded=True, text=None, ready_after=0,
        unavailable=False, fail=None,
    ):  # fmt: skip
        self.job_id = job_id
        self.unavailable = unavailable  # hh.ru: "Вам недоступна эта вакансия" (#272)
        self.fail = fail  # raised as soon as the page is opened
        self.hh = bool(href and "hh.ru" in href)
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
        hh = "hh.ru/vacancy/" in url
        default = FakePage(job_id, href=url, title=f"Вакансия Python-разработчик {job_id}") if hh \
            else FakePage(job_id)  # fmt: skip
        self.page = self.pages.get(job_id) or default
        if self.after_open:
            self.page = self.after_open(self.page) or self.page
        if self.page.fail:
            raise self.page.fail

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
            return {"href": p.href, "title": p.title, "status": p.status, "ready": ready,
                    "unavailable": p.unavailable}  # fmt: skip
        if function == EXPAND:
            self.calls.append(("expand", ""))
            if p.folded:
                p.folded = False
                return "… more"
            return ""
        assert READER in function, "only the bookmarklet's own reader reads the page"
        self.calls.append(("read", ""))
        if p.hh:  # the bookmarklet's reader on an hh.ru vacancy page (#136)
            m = re.search(r"hh\.ru/vacancy/(\d+)", p.href)
            return {
                "source": "hh",
                "source_url": p.href,
                "cards": [],
                "detail": {"job_id": m.group(1) if m else None,
                           "title": "Python-разработчик", "company": f"Компания {p.job_id}",
                           "work_format": "удалённо", "address": "Москва",
                           "description": p.text if p.text is not None else "Пишем на Python."},
            }  # fmt: skip
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
    assert detail and all(word in detail for word in ("open", "load", "expand", "read")), detail


# --- hh.ru (#260) ------------------------------------------------------------


def hh_vacancy(session, vacancy_id, score=5.0):
    from job_radar.dedup import get_or_create_company

    o = Opportunity(
        title="Python-разработчик",
        score=score,
        company=get_or_create_company(session, f"Компания {vacancy_id}"),
    )
    session.add(
        SourceRecord(
            source_type=SourceType.HH,
            external_id=vacancy_id,
            url=f"https://hh.ru/vacancy/{vacancy_id}",
            opportunity=o,
        )
    )
    session.commit()  # fmt: skip
    return o


def test_hh_vacancy_is_opened_read_and_saved(session):
    hh_vacancy(session, "4235")
    browser = FakeBrowser()
    stats = run(session, browser)
    assert (stats.total, stats.saved) == (1, 1)
    assert ("open_tab", "https://hh.ru/vacancy/4235") in browser.calls
    assert ("expand", "") not in browser.calls  # nothing to unfold or click on hh.ru
    assert descriptions(session) == {"4235": "Пишем на Python."}
    assert session.scalar(select(func.count(Opportunity.id))) == 1  # no second vacancy


def test_linkedin_and_hh_in_one_run_best_score_first(session):
    vacancy(session, "101")  # LinkedIn, scored by its title
    li_score = session.scalar(select(Opportunity.score))
    hh_vacancy(session, "4235", score=li_score + 1)
    hh_vacancy(session, "4236", score=li_score - 1)
    browser = FakeBrowser()
    stats = run(session, browser)
    opened = [u for k, u in browser.calls if k in ("open_tab", "navigate")]
    assert opened == [
        "https://hh.ru/vacancy/4235",
        "https://www.linkedin.com/jobs/view/101/",
        "https://hh.ru/vacancy/4236",
    ]
    assert stats.saved == 3


def test_hh_captcha_and_login_stop_the_run(session):
    for page in (
        FakePage("4235", href="https://hh.ru/account/captcha?backurl=%2Fvacancy%2F4235"),
        FakePage("4235", href="https://hh.ru/account/login?backurl=%2Fvacancy%2F4235"),
        FakePage("4235", href="https://hh.ru/vacancy/4235", status=403),
        FakePage("4235", href="https://hh.ru/vacancy/4235", title="Подтвердите, что вы не робот"),
    ):
        session.rollback()
        for row in session.scalars(select(LinkedInFetch)):
            session.delete(row)
        if not session.scalar(select(func.count(Opportunity.id))):
            hh_vacancy(session, "4235", score=9)
            vacancy(session, "101")
        browser = FakeBrowser({"4235": page})
        stats = run(session, browser)
        assert stats.blocked and stats.reason, page.__dict__
        assert ("read", "") not in browser.calls and descriptions(session) == {}
        opened = [u for k, u in browser.calls if k in ("open_tab", "navigate")]
        assert len(opened) == 1, f"went on to LinkedIn after {stats.reason}"


def test_hh_page_of_another_vacancy_saves_nothing(session):
    hh_vacancy(session, "4235")
    browser = FakeBrowser({"4235": FakePage("4235", href="https://hh.ru/vacancy/999")})
    stats = run(session, browser)
    assert stats.blocked and "another page" in stats.reason
    assert descriptions(session) == {}


# --- one vacancy's trouble is that vacancy's (#272) ------------------------------------


def unavailable_page(vacancy_id):
    """What hh.ru showed for vacancy 139171674: HTTP 403, the URL unchanged, title HeadHunter."""
    return FakePage(
        vacancy_id,
        href=f"https://hh.ru/vacancy/{vacancy_id}",
        title="HeadHunter",
        status=403,
        unavailable=True,
    )


def events(session):
    return {
        e.opportunity_id: e for e in session.scalars(select(CompanyEvent).order_by(CompanyEvent.id))
    }


def opportunity_of(session, external_id):
    return session.scalar(
        select(SourceRecord.opportunity_id).where(SourceRecord.external_id == external_id)
    )


def test_unavailable_hh_vacancy_is_marked_and_the_run_goes_on(session):
    hh_vacancy(session, "4235", score=9)
    hh_vacancy(session, "4236", score=8)
    browser = FakeBrowser({"4235": unavailable_page("4235")})
    stats = run(session, browser)
    assert not stats.blocked and stats.reason is None
    assert (stats.requests, stats.saved, stats.errors) == (2, 1, 1)
    assert set(descriptions(session)) == {"4236"}
    assert browser.calls[-1] == ("close_tab", "")  # no tab is left open for an ordinary miss
    fetch = session.scalar(select(LinkedInFetch).where(LinkedInFetch.job_id == "4235"))
    assert fetch.status == "unavailable" and "403" in fetch.detail
    opportunity = session.get(Opportunity, opportunity_of(session, "4235"))
    assert opportunity.unavailable_at is not None
    assert opportunity_of(session, "4235") not in events(session)  # not in the history (#276)
    assert (opportunity.state, opportunity.auto_ignore_reason) == (
        OpportunityState.AUTO_IGNORE,
        UNAVAILABLE_REASON,
    )


def test_unavailable_vacancy_stays_in_review_when_the_rule_is_off(session):
    save_rules(session, Rules(unavailable=False))
    hh_vacancy(session, "4235")
    run(session, FakeBrowser({"4235": unavailable_page("4235")}))
    opportunity = session.get(Opportunity, opportunity_of(session, "4235"))
    assert opportunity.unavailable_at is not None
    assert (opportunity.state, opportunity.auto_ignore_reason) == (OpportunityState.NEW, None)


def test_unavailable_vacancy_is_not_opened_again_until_cleared(session):
    save_rules(session, Rules(unavailable=False))
    hh_vacancy(session, "4235")
    run(session, FakeBrowser({"4235": unavailable_page("4235")}))
    again = FakeBrowser()
    assert run(session, again).total == 0
    assert not [c for c in again.calls if c[0] in ("open_tab", "navigate")]
    clear_unavailable(session.get(Opportunity, opportunity_of(session, "4235")))
    session.commit()
    assert run(session, FakeBrowser()).saved == 1


def test_clearing_brings_back_a_vacancy_only_this_rule_moved(session):
    hh_vacancy(session, "4235")
    run(session, FakeBrowser({"4235": unavailable_page("4235")}))
    opportunity = session.get(Opportunity, opportunity_of(session, "4235"))
    clear_unavailable(opportunity)
    assert opportunity.unavailable_at is None
    assert (opportunity.state, opportunity.auto_ignore_reason) == (OpportunityState.NEW, None)
    opportunity.unavailable_at, opportunity.state = datetime.now(UTC), OpportunityState.IGNORE
    clear_unavailable(opportunity)
    assert opportunity.state is OpportunityState.IGNORE  # your own state is yours


def test_a_vacancy_that_is_gone_is_marked_like_an_unavailable_one(session):
    hh_vacancy(session, "4235", score=9)
    hh_vacancy(session, "4236", score=8)
    gone = FakePage("4235", href="https://hh.ru/vacancy/4235", status=404, ready_after=99)
    stats = run(session, FakeBrowser({"4235": gone}))
    assert not stats.blocked and stats.saved == 1
    assert session.get(Opportunity, opportunity_of(session, "4235")).unavailable_at is not None


def test_a_plain_403_still_stops_the_run_and_marks_the_vacancy(session):
    hh_vacancy(session, "4235", score=9)
    hh_vacancy(session, "4236", score=8)
    plain = FakePage("4235", href="https://hh.ru/vacancy/4235", status=403)
    stats = run(session, FakeBrowser({"4235": plain}))
    assert stats.blocked and stats.reason == "HTTP 403" and stats.requests == 1
    assert events(session)[opportunity_of(session, "4235")].kind == FILL_PROBLEM
    # Not "unavailable": the next run tries it again.
    assert run(session, FakeBrowser()).saved == 2


def test_browser_error_on_one_vacancy_is_marked_and_the_run_goes_on(session):
    hh_vacancy(session, "4235", score=9)
    hh_vacancy(session, "4236", score=8)
    broken = FakePage("4235", href="https://hh.ru/vacancy/4235", fail=BrowserError("net::ERR"))
    stats = run(session, FakeBrowser({"4235": broken}))
    assert not stats.blocked and (stats.requests, stats.saved, stats.errors) == (2, 1, 1)
    event = events(session)[opportunity_of(session, "4235")]
    assert event.kind == FILL_PROBLEM and "net::ERR" in event.comment
    fetch = session.scalar(select(LinkedInFetch).where(LinkedInFetch.job_id == "4235"))
    assert fetch.status == "error"


def test_a_page_that_never_answers_times_out_and_the_run_goes_on(session, monkeypatch):
    from job_radar.linkedin import chrome

    monkeypatch.setattr(chrome, "PAGE_TIMEOUT", 0.05)

    class Hanging(FakeBrowser):
        async def open_tab(self, url):
            if url.endswith("4235"):
                await asyncio.Event().wait()
            await super().open_tab(url)

    hh_vacancy(session, "4235", score=9)
    hh_vacancy(session, "4236", score=8)
    stats = run(session, Hanging())
    assert not stats.blocked and stats.saved == 1 and stats.errors == 1
    assert "no answer" in events(session)[opportunity_of(session, "4235")].comment


def test_failure_while_saving_leaves_the_session_usable(session, monkeypatch):
    from job_radar.linkedin import chrome

    real = chrome.import_capture

    def boom(s, payload):
        if payload["detail"]["job_id"] == "4235":
            s.add(LinkedInFetch(start=None, status="x"))  # NOT NULL: the flush raises
            s.flush()
        return real(s, payload)

    monkeypatch.setattr(chrome, "import_capture", boom)
    hh_vacancy(session, "4235", score=9)
    hh_vacancy(session, "4236", score=8)
    stats = run(session, FakeBrowser())
    assert stats.saved == 1 and stats.errors == 1
    assert set(descriptions(session)) == {"4236"}


def test_three_failures_in_a_row_stop_the_run(session):
    for n in range(5):
        hh_vacancy(session, f"423{n}", score=9 - n)
    pages = {
        f"423{n}": FakePage(
            f"423{n}", href=f"https://hh.ru/vacancy/423{n}", fail=BrowserError("gone")
        )
        for n in range(5)
    }
    stats = run(session, FakeBrowser(pages))
    assert stats.blocked and stats.requests == 3 and stats.errors == 3
    assert "3" in stats.reason


def test_after_a_failed_navigation_the_error_page_does_not_stop_the_run(session):
    """Chrome shows chrome-error:// after net::ERR; the tab is still ours."""

    class ErrorPage(FakeBrowser):
        async def navigate(self, url):
            if url.endswith("4236"):
                self.calls.append(("navigate", url))
                self.page = FakePage("4236", href="chrome-error://chromewebdata/")
                raise BrowserError("net::ERR_FAILED")
            await super().navigate(url)

    for n, score in (("4235", 9), ("4236", 8), ("4237", 7)):
        hh_vacancy(session, n, score=score)
    stats = run(session, ErrorPage())
    assert not stats.blocked and stats.saved == 2 and stats.errors == 1


def test_progress_is_counted_per_source(session):
    vacancy(session, "101")
    hh_vacancy(session, "4235", score=9)
    hh_vacancy(session, "4236", score=8)
    seen = []
    browser = FakeBrowser({"4235": unavailable_page("4235")})
    stats = run(session, browser, on_progress=lambda s: seen.append(asdict(s)["sources"]))
    assert seen[0]["hh"]["total"] == 2 and seen[0]["linkedin"]["total"] == 1
    assert seen[0]["hh"]["opened"] == 0
    hh, linkedin = stats.sources["hh"], stats.sources["linkedin"]
    assert (hh.label, hh.total, hh.opened, hh.saved, hh.problems) == ("hh.ru", 2, 2, 1, 1)
    assert (linkedin.label, linkedin.opened, linkedin.saved) == ("LinkedIn", 1, 1)


def test_status_shows_a_bar_and_counts_per_source(engine, database_url, tmp_path):
    release = threading.Event()

    def runner(job):
        job.progress(
            total=4, requests=1, saved=0, errors=1,
            sources=[{"label": "LinkedIn", "total": 3, "opened": 0, "saved": 0, "problems": 0},
                     {"label": "hh.ru", "total": 1, "opened": 1, "saved": 0, "problems": 1}],
        )  # fmt: skip
        release.wait(5)

    with web(database_url, tmp_path, runner, linkedin_chrome=True) as client:
        client.post("/chrome-fill")
        deadline = time.time() + 5
        while "hh.ru" not in (page := client.get("/chrome-fill/status").text):
            assert time.time() < deadline
        release.set()
    assert 'class="progress"' in page and 'aria-valuenow="25"' in page
    assert re.search(r"LinkedIn:\s*0 of 3", page) and re.search(r"hh\.ru:\s*1 of 1", page)
    assert "1 with a problem" in page


def test_fill_marks_are_not_contacts_or_your_actions(engine, session):
    from job_radar.contacts import company_history
    from job_radar.web.app import _visible_history_count, last_actions

    hh_vacancy(session, "4235")
    broken = FakePage("4235", href="https://hh.ru/vacancy/4235", fail=BrowserError("net::ERR"))
    run(session, FakeBrowser({"4235": broken}))
    oid = opportunity_of(session, "4235")
    company_id = session.get(Opportunity, oid).company_id
    history = company_history(session, company_id)
    assert [e.kind for e in history] == [FILL_PROBLEM]  # shown with the vacancy
    assert "HeadHunter" not in history[0].text and "net::ERR" in history[0].text
    assert _visible_history_count(history) == 0  # no "contacted · 1" badge
    assert FILL_PROBLEM.replace("_", " ") not in (last_actions(session).get(oid) or ("", ""))[1]


def test_not_accepting_vacancy_is_not_opened_again_until_cleared(session):
    save_rules(session, Rules(unavailable=False))
    hh_vacancy(session, "4235")
    opportunity = session.get(Opportunity, opportunity_of(session, "4235"))
    opportunity.not_accepting_at = datetime.now(UTC)
    session.commit()
    assert run(session, FakeBrowser()).total == 0
    clear_unavailable(opportunity)
    session.commit()
    assert run(session, FakeBrowser()).total == 1


def test_probe_asks_the_page_whether_the_vacancy_is_unavailable():
    assert "недоступна" in PROBE and "unavailable" in PROBE


def test_repeated_problem_is_recorded_once(session):
    hh_vacancy(session, "4235", score=9)
    hh_vacancy(session, "4236", score=8)
    broken = FakePage("4235", href="https://hh.ru/vacancy/4235", fail=BrowserError("net::ERR"))
    run(session, FakeBrowser({"4235": broken}))
    run(session, FakeBrowser({"4235": broken}))
    assert session.scalar(select(func.count(CompanyEvent.id))) == 1


def test_failed_vacancies_are_listed_with_links(session):
    """Which vacancies had a problem, not only how many (#274)."""
    hh_vacancy(session, "4235", score=9)
    hh_vacancy(session, "4236", score=8)
    stats = run(session, FakeBrowser({"4235": unavailable_page("4235")}))
    assert [f["url"] for f in stats.failed] == ["https://hh.ru/vacancy/4235"]
    failed = stats.failed[0]
    assert failed["opportunity_id"] == opportunity_of(session, "4235")
    assert failed["title"] and "not available" in failed["reason"]


def test_status_lists_the_failed_vacancies(engine, database_url, tmp_path):
    def runner(job):
        job.progress(
            total=1, requests=1, saved=0, errors=1,
            failed=[{"opportunity_id": 77, "title": "Dev <b>", "site": "hh.ru",
                     "url": "https://hh.ru/vacancy/1", "reason": "not available to you"}],
        )  # fmt: skip

    with web(database_url, tmp_path, runner, linkedin_chrome=True) as client:
        client.post("/chrome-fill")
        deadline = time.time() + 5
        while "done" not in (page := client.get("/chrome-fill/status").text):
            assert time.time() < deadline
    assert 'href="/opportunities/77"' in page and "Dev &lt;b&gt;" in page
    assert 'href="https://hh.ru/vacancy/1"' in page and "not available to you" in page


def test_page_of_checks_the_host_not_a_substring():
    # CodeQL py/incomplete-url-substring-sanitization: "linkedin.com" anywhere in the URL
    # made another site's page look like a LinkedIn vacancy (#11).
    assert page_of("https://www.linkedin.com/jobs/view/123/") == ("linkedin", "123")
    assert page_of("https://linkedin.com/jobs/view/123/") == ("linkedin", "123")
    assert page_of("https://example.com/linkedin.com/jobs/view/123/") is None
    assert page_of("https://linkedin.com.example.com/jobs/view/123/") is None


def test_finished_status_can_be_dismissed_until_the_next_run(engine, database_url, tmp_path):
    """The ✕ on a finished Chrome fill hides it until the next run (#19).

    Runs the page's own script in headless Chromium; see tests/test_chat_keys.py for how.
    """
    from test_chat_keys import CHROME

    sync_api = pytest.importorskip("playwright.sync_api")
    if not CHROME.exists():
        pytest.skip(f"no Chromium at {CHROME}")

    def runner(job):
        job.progress(total=2, requests=2, saved=1, errors=1)
        job.finish()

    with web(database_url, tmp_path, runner, linkedin_chrome=True) as client:
        client.post("/chrome-fill")
        for _ in range(50):
            status = client.get("/chrome-fill/status").text
            if "status-dismiss" in status:
                break
            time.sleep(0.05)
        page_html = client.get("/").text
    assert "data-finished=" in status

    with sync_api.sync_playwright() as p:
        browser = p.chromium.launch(executable_path=str(CHROME))
        page = browser.new_page()
        # Only the app's own page is served; LinkedIn, hh.ru and the CDN are never reached.
        # A real origin (not about:blank) is needed for localStorage.
        page.route(
            "**/*",
            lambda route, request: (
                route.fulfill(body=page_html, content_type="text/html")
                if request.url == "http://job-radar.test/"
                else route.abort()
            ),
        )
        page.goto("http://job-radar.test/")
        errors: list[str] = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        # htmx is blocked above, so put the finished status in place as htmx would.
        swap = """html => { document.getElementById('chrome-status').outerHTML = html;
                   document.body.dispatchEvent(new Event('htmx:afterSettle')); }"""
        page.evaluate(swap, status)
        assert page.locator("#chrome-status").is_visible()

        page.click("#chrome-status .status-dismiss")
        assert errors == []
        assert not page.locator("#chrome-status").is_visible()

        page.evaluate(swap, status)  # the same run drawn again, e.g. after a reload
        assert not page.locator("#chrome-status").is_visible()

        page.evaluate(swap, status.replace('data-finished="', 'data-finished="next-'))
        assert page.locator("#chrome-status").is_visible()  # a new run shows again
        browser.close()
