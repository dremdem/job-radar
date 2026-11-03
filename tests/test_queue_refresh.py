"""#263: when a background job the page was watching ends, the queue redraws itself."""

import time

from fastapi.testclient import TestClient

from job_radar.config import Settings
from job_radar.web.app import create_app

TRIGGER = "jr-refresh-queue"


def app(database_url, tmp_path, **kw):
    settings = Settings(
        database_url=database_url,
        profile=tmp_path / "p.yaml",
        linkedin_public_search=True,
        linkedin_chrome=True,
    )
    return TestClient(create_app(settings, start_worker=False, **kw))


def finish(client, start, status, timeout=5.0):
    client.post(start)
    deadline = time.time() + timeout
    while time.time() < deadline:
        if "Stop" not in client.get(status).text:
            return
        time.sleep(0.05)
    raise AssertionError("the job did not finish")


def saving(saved):
    def runner(job):
        job.progress(total=3, requests=3, saved=saved, errors=0)
        job.finish()

    return runner


def test_finished_job_tells_a_watching_page_to_redraw(engine, database_url, tmp_path):
    for start, status, kw in (
        ("/chrome-fill", "/chrome-fill/status", {"chrome_runner": saving(2)}),
        ("/descriptions", "/descriptions/status", {"description_runner": saving(2)}),
    ):
        with app(database_url, tmp_path, **kw) as client:
            finish(client, start, status)
            # The poll that sees it finished: the page was watching it run.
            polled = client.get(status, params={"polled": 1}, headers={"HX-Request": "true"})
            assert TRIGGER in polled.headers.get("HX-Trigger", ""), status
            # A page loaded later shows the new state anyway: no redraw needed.
            assert "HX-Trigger" not in client.get(status).headers, status


def test_nothing_saved_no_redraw(engine, database_url, tmp_path):
    with app(database_url, tmp_path, chrome_runner=saving(0)) as client:
        finish(client, "/chrome-fill", "/chrome-fill/status")
        polled = client.get("/chrome-fill/status", params={"polled": 1})
        assert "HX-Trigger" not in polled.headers


def test_running_status_polls_with_the_flag(engine, database_url, tmp_path):
    import threading

    release = threading.Event()

    def runner(job):
        job.progress(total=3, requests=1, saved=0, errors=0)
        release.wait(5)
        job.finish()

    with app(database_url, tmp_path, chrome_runner=runner) as client:
        text = client.post("/chrome-fill").text
        release.set()
    assert 'hx-get="/chrome-fill/status?polled=1"' in text


def test_sync_end_redraws_the_queue(engine, database_url, tmp_path):
    def runner(echo, on_steps):
        echo("done")

    with app(database_url, tmp_path, sync_runner=runner) as client:
        client.post("/sync")
        for _ in range(100):
            if "running" not in client.get("/sync/status").text:
                break
            time.sleep(0.05)
        polled = client.get("/sync/status", params={"polled": 1})
        assert TRIGGER in polled.headers.get("HX-Trigger", "")


def test_queue_page_listens_for_the_redraw(client, session):
    from job_radar.dedup import get_or_create_company
    from job_radar.models import Opportunity

    session.add(
        Opportunity(title="Python Engineer", company=get_or_create_company(session, "Acme"))
    )
    session.commit()
    text = client.get("/").text
    assert 'id="queue-body"' in text
    # The same redraw as the search box: the list, the tabs and the description switch.
    listener = text[text.index(f"{TRIGGER} from:body") - 400 :]
    assert 'hx-select="#queue-body"' in listener and "#tabs,#desc-switch" in listener
