"""Background LinkedIn description job, random pause, request log (#205)."""

import threading
import time
from datetime import UTC, datetime

from fastapi.testclient import TestClient
from sqlalchemy import select
from test_public_descriptions import POSTING, FakeHttp, vacancy

from job_radar import pipeline
from job_radar.config import Settings
from job_radar.linkedin.public import (
    PauseSettings,
    PublicSearchClient,
    import_public_descriptions,
    load_pause,
    save_pause,
)
from job_radar.models import LinkedInFetch
from job_radar.web.app import create_app

NOW = datetime(2026, 10, 17, 3, 0, tzinfo=UTC)


def no_sleep(_):
    return None


def wait_until(check, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if check():
            return True
        time.sleep(0.02)
    return False


def test_pause_defaults_and_is_saved(session):
    assert load_pause(session) == PauseSettings(10, 40)
    save_pause(session, PauseSettings(5, 25))
    assert load_pause(session) == PauseSettings(5, 25)


def test_log_keeps_the_vacancy_the_time_and_the_reason(session):
    ok = vacancy(session, "1", score=9.0)
    empty = vacancy(session, "2", score=8.0)
    blocked = vacancy(session, "3", score=7.0)
    responses = iter([(200, "u", POSTING), (200, "u", "<html>moved</html>"), (429, "u", "")])
    client = PublicSearchClient(http_get=lambda url: next(responses), sleep=no_sleep)
    stats = import_public_descriptions(session, client, max_per_day=10)
    assert (stats.total, stats.requests, stats.saved, stats.errors) == (3, 3, 1, 1)
    assert stats.blocked and stats.reason == "HTTP 429"
    rows = session.scalars(select(LinkedInFetch).order_by(LinkedInFetch.id)).all()
    assert [(r.opportunity_id, r.status, r.detail) for r in rows] == [
        (ok.id, "ok", None),
        (empty.id, "error", "no description on page"),
        (blocked.id, "blocked", "HTTP 429"),
    ]
    assert rows[0].at <= rows[2].at  # each request has its own time


def test_progress_is_reported_after_each_request(session):
    for n in range(3):
        vacancy(session, str(n), score=float(n))
    seen = []
    client = PublicSearchClient(http_get=FakeHttp(), sleep=no_sleep)
    import_public_descriptions(
        session, client, now=NOW, max_per_day=10, on_progress=lambda s: seen.append(s.requests)
    )
    assert seen[0] == 0 and seen[-1] == 3


def test_stop_ends_the_job_without_a_request_and_logs_it(session):
    for n in range(3):
        vacancy(session, str(n), score=float(n))
    stop = threading.Event()
    http = FakeHttp()

    def get(url):
        stop.set()  # the user presses Stop during the first request
        return http(url)

    client = PublicSearchClient(http_get=get, stop=stop)
    stats = import_public_descriptions(session, client, now=NOW, max_per_day=10)
    assert stats.stopped and (stats.requests, stats.saved) == (1, 1)
    assert len(http.urls) == 1
    last = session.scalars(select(LinkedInFetch).order_by(LinkedInFetch.id.desc())).first()
    assert (last.status, last.detail) == ("stopped", "stopped by you")
    # A stop is not a block and does not use the daily budget.
    again = import_public_descriptions(
        session, PublicSearchClient(http_get=FakeHttp(), sleep=no_sleep), now=NOW, max_per_day=3
    )
    assert not again.blocked and again.requests == 2


def test_the_wait_is_announced_before_it_starts(session):
    vacancy(session, "1", score=2.0)
    vacancy(session, "2", score=1.0)
    waits = []
    client = PublicSearchClient(
        http_get=FakeHttp(), sleep=no_sleep, pause=(10, 40), rand=lambda a, b: 12.0,
        on_wait=waits.append,
    )  # fmt: skip
    import_public_descriptions(session, client, now=NOW, max_per_day=10)
    assert waits == [12.0]


def test_sync_starts_the_background_job_instead_of_fetching(tmp_path, engine, database_url):
    started = []
    settings = Settings(
        database_url=database_url,
        gmail_token=tmp_path / "missing.json",
        himalayas=False,
        fdetimes=False,  # no network in tests
        ai_analysis=False,
        linkedin_public_search=True,
    )
    steps = pipeline.run_sync(settings, print, start_descriptions=lambda: started.append(1) or True)
    step = next(s for s in steps if s.key == "descriptions")
    assert started == [1] and (step.state, step.summary) == ("done", "started in the background")


# --- web -------------------------------------------------------------------


def web(database_url, tmp_path, runner, **extra):
    options = {"linkedin_public_search": True} | extra
    settings = Settings(database_url=database_url, profile=tmp_path / "p.yaml", **options)
    return TestClient(create_app(settings, start_worker=False, description_runner=runner))


def test_button_starts_the_job_and_shows_progress(engine, database_url, tmp_path):
    release = threading.Event()

    def runner(job):
        job.progress(total=29, requests=7, saved=6, errors=0)
        job.waiting(23)
        release.wait(5)

    with web(database_url, tmp_path, runner) as client:
        page = client.get("/").text
        assert 'hx-post="/descriptions"' in page and ">Descriptions</button>" in page
        text = client.post("/descriptions").text
        assert wait_until(lambda: "7 of 29" in client.get("/descriptions/status").text)
        text = client.get("/descriptions/status").text
        assert "6 saved" in text and "next in 2" in text and 'hx-post="/descriptions/stop"' in text
        release.set()
        assert wait_until(lambda: "running" not in client.get("/descriptions/status").text)


def test_stop_button_stops_the_job(engine, database_url, tmp_path):
    def runner(job):
        job.progress(total=5, requests=1, saved=1, errors=0)
        job.stop_event.wait(5)
        job.finish(stopped=True)

    with web(database_url, tmp_path, runner) as client:
        client.post("/descriptions")
        client.post("/descriptions/stop")
        assert wait_until(lambda: "stopped by you" in client.get("/descriptions/status").text)


def test_finished_job_says_why_it_ended(engine, database_url, tmp_path):
    def runner(job):
        job.progress(total=10, requests=3, saved=2, errors=0)
        job.finish(blocked=True, reason="HTTP 429")

    with web(database_url, tmp_path, runner) as client:
        client.post("/descriptions")
        assert wait_until(lambda: "HTTP 429" in client.get("/descriptions/status").text)
        text = client.get("/descriptions/status").text
        assert "LinkedIn blocked" in text and "2 saved" in text and 'href="/linkedin/log"' in text


def test_a_failing_job_shows_the_error(engine, database_url, tmp_path):
    def runner(job):
        raise RuntimeError("database is locked")

    with web(database_url, tmp_path, runner) as client:
        client.post("/descriptions")
        assert wait_until(lambda: "database is locked" in client.get("/descriptions/status").text)


def test_button_says_when_the_feature_is_off(engine, database_url, tmp_path):
    off = {"linkedin_public_search": False, "fdetimes": False}
    with web(database_url, tmp_path, lambda job: None, **off) as c:
        text = c.post("/descriptions").text
        assert "JOB_RADAR_LINKEDIN_PUBLIC_SEARCH" in text and "JOB_RADAR_FDETIMES" in text


def test_button_runs_with_fdetimes_alone(engine, database_url, tmp_path):
    """FDE Times descriptions come through the same job (docs/fdetimes.md)."""
    ran = threading.Event()
    with web(database_url, tmp_path, lambda job: ran.set(), linkedin_public_search=False) as c:
        c.post("/descriptions")
        assert ran.wait(5)


def test_log_page_lists_requests_with_reasons(engine, database_url, tmp_path, session):
    o = vacancy(session, "77", score=5.0, title="Python Backend Engineer")
    now = datetime.now(UTC)
    session.add_all(
        [
            LinkedInFetch(at=now, kind="description", job_id="77", start=0, status="ok",
                          jobs=1, opportunity_id=o.id),
            LinkedInFetch(at=now, kind="description", job_id="78", start=0, status="blocked",
                          http_status=429, detail="HTTP 429"),
        ]
    )  # fmt: skip
    session.commit()
    with web(database_url, tmp_path, lambda job: None) as client:
        text = client.get("/linkedin/log").text
    assert "Python Backend Engineer" in text and f'href="/opportunities/{o.id}"' in text
    assert "HTTP 429" in text and "blocked" in text
    assert "2 requests" in text and "1 saved" in text
    # Rows logged before #205 have only the job id; the vacancy is found by it.
    old = vacancy(session, "79", score=5.0, title="Django Developer")
    session.add(LinkedInFetch(at=now, kind="description", job_id="79", start=0, status="ok"))
    session.commit()
    with web(database_url, tmp_path, lambda job: None) as client:
        assert f'href="/opportunities/{old.id}"' in client.get("/linkedin/log").text


def test_settings_save_the_pause(engine, database_url, tmp_path, session):
    with web(database_url, tmp_path, lambda job: None) as client:
        page = client.get("/settings").text
        assert 'name="pause_from"' in page and 'value="10' in page and "/linkedin/log" in page
        client.post("/settings/linkedin", data={"pause_from": "5", "pause_to": "25"})
        assert load_pause(session) == PauseSettings(5, 25)
        bad = client.post("/settings/linkedin", data={"pause_from": "30", "pause_to": "20"})
        assert bad.status_code == 422
