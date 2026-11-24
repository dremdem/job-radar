"""Tests for the bookmarklet page and the capture API of the web UI
(docs/linkedin-capture.md).
"""

import sqlalchemy as sa

import job_radar.models as models

CARD = "Staff Software Engineer (AI Backend Services)\nPfizer\nSofia\nBe an early applicant"


def test_capture_page_offers_the_bookmarklet(client):
    text = client.get("/capture").text
    assert 'href="javascript:' in text
    assert "→ Job Radar" in text
    # base URL of this server
    assert "http://testserver&#39;" in text and "/capture#" in text
    assert "/api/capture" in text


def test_capture_page_uses_the_public_url(tmp_path, engine, database_url):
    import fastapi.testclient as fastapi_testclient

    import job_radar.config as config
    import job_radar.web.app as web_app

    settings = config.Settings(
        database_url=database_url,
        capture_debug_dir=tmp_path / "debug",
        public_url="https://box.example.ts.net/",
    )
    app = web_app.create_app(settings, start_worker=False)
    with fastapi_testclient.TestClient(app) as client:
        text = client.get("/capture").text
    # both bookmarklets, whatever address the page was opened from
    assert text.count("https://box.example.ts.net&#39;") == 2
    assert "testserver" not in text


def test_api_capture(client, session):
    response = client.post(
        "/api/capture",
        json={
            "source_url": "https://www.linkedin.com/jobs/search-results/",
            "cards": [{"job_id": "5", "text": CARD}],
        },
    )
    assert response.status_code == 200
    assert response.json() == {
        "cards": 1,
        "new": 1,
        "known": 0,
        "skipped": 0,
        "descriptions": 0,
        "vacancy": "",
        "opportunity_ids": [session.scalar(sa.select(models.Opportunity.id))],
    }
    assert (
        session.scalar(sa.select(models.Opportunity.title))
        == "Staff Software Engineer (AI Backend Services)"
    )


def test_api_capture_rejects_non_json(client):
    assert client.post("/api/capture", content="cards=1").status_code == 422


def test_nav_links_to_capture(client):
    assert 'href="/capture"' in client.get("/").text


def debug_client(tmp_path, database_url):
    import fastapi.testclient as fastapi_testclient

    import job_radar.config as config
    import job_radar.web.app as web_app

    settings = config.Settings(database_url=database_url, capture_debug_dir=tmp_path / "debug")
    return fastapi_testclient.TestClient(web_app.create_app(settings)), tmp_path / "debug"


def test_debug_capture_saves_page_locally(tmp_path, engine, database_url):
    client, folder = debug_client(tmp_path, database_url)
    with client:
        response = client.post(
            "/api/capture/debug",
            json={
                "source_url": "https://www.linkedin.com/jobs/search-results/",
                "html": "<main>cards</main>",
            },
        )
        assert response.status_code == 200
    files = sorted(folder.glob("*.html"))
    assert len(files) == 1 and "<main>cards</main>" in files[0].read_text()
    assert "linkedin.com/jobs/search-results/" in files[0].read_text()


def test_last_capture_payload_is_kept_locally(tmp_path, engine, database_url):
    client, folder = debug_client(tmp_path, database_url)
    with client:
        client.post("/api/capture", json={"cards": [{"job_id": "1", "text": ""}]})
    assert '"job_id": "1"' in (folder / "last_payload.json").read_text()


def test_capture_page_offers_debug_bookmarklet(client):
    text = client.get("/capture").text
    assert "→ Job Radar (debug)" in text
    assert "/capture/debug" in text
    debug_page = client.get("/capture/debug").text
    assert "postMessage" in debug_page and r"linkedin\.com" in debug_page


def test_capture_page_shows_the_vacancy_result(client):
    text = client.get("/capture").text
    # the vacancy line is rendered from the API answer
    assert "s.vacancy" in text
