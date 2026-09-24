from job_radar.dedup import get_or_create_company
from job_radar.models import Opportunity, OpportunityState


def add_opportunity(session, title="Senior Python Engineer") -> Opportunity:
    opportunity = Opportunity(title=title, company=get_or_create_company(session, "Acme"))
    session.add(opportunity)
    session.commit()
    return opportunity


def test_empty_queue(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "No opportunities yet" in response.text


def test_queue_lists_opportunities(client, session):
    add_opportunity(session)
    response = client.get("/")
    assert "Senior Python Engineer" in response.text
    assert "Acme" in response.text


def test_set_state(client, session):
    opportunity = add_opportunity(session)
    response = client.post(f"/opportunities/{opportunity.id}/state", data={"state": "interested"})
    assert response.status_code == 200
    assert "interested" in response.text
    session.refresh(opportunity)
    assert opportunity.state is OpportunityState.INTERESTED


def test_set_state_rejects_unknown_state(client, session):
    opportunity = add_opportunity(session)
    response = client.post(f"/opportunities/{opportunity.id}/state", data={"state": "hired"})
    assert response.status_code == 422


def test_set_state_unknown_opportunity(client):
    response = client.post("/opportunities/999/state", data={"state": "ignore"})
    assert response.status_code == 404


def test_healthz(client):
    assert client.get("/healthz").json() == {"status": "ok"}


def test_queue_shows_details_and_sorts_by_score(client, session):
    from job_radar.dedup import upsert_source_record
    from job_radar.models import SourceType

    low = add_opportunity(session, title="Low match")
    low.score = 2
    high = add_opportunity(session, title="High match")
    high.score = 9
    high.location = "Worldwide (Remote)"
    high.easy_apply = True
    high.score_reasons = [{"sign": "+", "text": "Python"}, {"sign": "-", "text": "Frontend"}]
    upsert_source_record(
        session,
        source_type=SourceType.LINKEDIN_ALERT,
        external_id="42",
        url="https://www.linkedin.com/jobs/view/42/",
        opportunity=high,
    )
    session.commit()

    text = client.get("/").text
    assert text.index("High match") < text.index("Low match")
    assert "Worldwide (Remote)" in text
    assert "Easy Apply" in text
    assert "+ Python" in text and "\u2212 Frontend" in text
    assert 'href="https://www.linkedin.com/jobs/view/42/"' in text
    assert "verify manually" in text
