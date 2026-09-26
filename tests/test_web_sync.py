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
    assert 'hx-post="/sync"' in client.get("/").text


def test_sync_runs_pipeline_and_reports_output(engine, database_url):
    def runner(echo):
        echo("linkedin: 2 alert emails, 12 jobs, 3 new")
        echo("job_proposal: 7 threads, 1 new, 6 already stored")

    with make_client(database_url, runner) as client:
        assert client.post("/sync").status_code == 200
        text = wait_done(client)
    assert "3 new" in text and "job_proposal: 7 threads" in text
    assert 'href="/"' in text  # reload link


def test_only_one_sync_at_a_time(engine, database_url):
    release, calls = threading.Event(), []

    def runner(echo):
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
    def runner(echo):
        raise GmailAuthError("No Gmail token. Run `job-radar gmail auth` first.")

    with make_client(database_url, runner) as client:
        client.post("/sync")
        text = wait_done(client)
    assert "job-radar gmail auth" in text and "failed" in text.lower()


def test_status_before_any_sync(client):
    assert client.get("/sync/status").status_code == 200
