import threading
import time

from fastapi.testclient import TestClient

from job_radar.config import Settings
from job_radar.gmail.auth import GmailAuthError
from job_radar.web.app import create_app


def make_client(database_url, runner):
    return TestClient(create_app(Settings(database_url=database_url), sync_runner=runner))


def wait_done(client, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        text = client.get("/sync/status").text
        if "running" not in text:
            return text
        time.sleep(0.05)
    raise AssertionError("sync did not finish")


def test_header_has_sync_button(client):
    text = client.get("/").text
    assert 'hx-post="/sync"' in text
    assert ">Sync</button>" in text and "Sync mail" not in text


def test_status_lists_steps(engine, database_url):
    from job_radar.pipeline import Step

    def runner(echo, on_steps):
        steps = [Step("gmail", "Gmail"), Step("telegram", "Telegram"), Step("ai", "AI analysis")]
        steps[0].state, steps[0].summary = (
            "failed",
            "Gmail access expired or revoked: run `./jr auth`",
        )
        steps[1].state, steps[1].summary = "done", "48 new posts, 7 vacancies"
        steps[2].state = "off"
        on_steps(steps)
        echo("telegram: 48 new posts, 7 vacancies")

    with make_client(database_url, runner) as client:
        client.post("/sync")
        text = wait_done(client)
    assert "Gmail" in text and "./jr auth" in text and "✗" in text
    assert "✓" in text and "48 new posts, 7 vacancies" in text
    assert "AI analysis" in text and ">off<" in text
    # A step failed: the sync is done, with problems, not "failed" as a whole.
    assert "Sync done" in text and "1 step failed" in text


def test_sync_runs_pipeline_and_reports_output(engine, database_url):
    def runner(echo, on_steps):
        echo("linkedin: 2 alert emails, 12 jobs, 3 new")
        echo("job_proposal: 7 threads, 1 new, 6 already stored")

    with make_client(database_url, runner) as client:
        assert client.post("/sync").status_code == 200
        text = wait_done(client)
    assert "3 new" in text and "job_proposal: 7 threads" in text
    assert 'href="/"' in text  # reload link


def test_only_one_sync_at_a_time(engine, database_url):
    release, calls = threading.Event(), []

    def runner(echo, on_steps):
        calls.append(1)
        echo("working")
        release.wait(5)

    with make_client(database_url, runner) as client:
        client.post("/sync")
        second = client.post("/sync").text
        assert "running" in second and "working" in second
        release.set()
        wait_done(client)
    assert calls == [1]


def test_sync_error_is_shown(engine, database_url):
    def runner(echo, on_steps):
        raise GmailAuthError("No Gmail token. Run `job-radar gmail auth` first.")

    with make_client(database_url, runner) as client:
        client.post("/sync")
        text = wait_done(client)
    assert "job-radar gmail auth" in text and "failed" in text.lower()


def test_status_before_any_sync(client):
    assert client.get("/sync/status").status_code == 200
