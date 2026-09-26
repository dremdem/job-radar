from sqlalchemy import select

from job_radar.models import Opportunity

CARD = "Staff Software Engineer (AI Backend Services)\nPfizer\nSofia\nBe an early applicant"


def test_capture_page_offers_the_bookmarklet(client):
    text = client.get("/capture").text
    assert 'href="javascript:' in text
    assert "→ Job Radar" in text
    assert "http://testserver&#39;" in text and "/capture#" in text  # base URL of this server
    assert "/api/capture" in text


def test_api_capture(client, session):
    response = client.post(
        "/api/capture",
        json={
            "source_url": "https://www.linkedin.com/jobs/search-results/",
            "cards": [{"job_id": "5", "text": CARD}],
        },
    )
    assert response.status_code == 200
    assert response.json() == {"cards": 1, "new": 1, "known": 0, "skipped": 0, "descriptions": 0}
    assert (
        session.scalar(select(Opportunity.title)) == "Staff Software Engineer (AI Backend Services)"
    )


def test_api_capture_rejects_non_json(client):
    assert client.post("/api/capture", content="cards=1").status_code == 422


def test_nav_links_to_capture(client):
    assert 'href="/capture"' in client.get("/").text
