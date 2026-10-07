from sqlalchemy import select

from job_radar.dedup import add_snapshot, get_or_create_company, upsert_source_record
from job_radar.linkedin.capture import import_capture
from job_radar.models import Opportunity, OpportunityState, SourceType


def vacancy(
    session, title="Backend Engineer", state=OpportunityState.NEW, description=None, job_id="700"
):
    o = Opportunity(title=title, company=get_or_create_company(session, "Acme"), state=state)
    session.add(o)
    session.flush()
    record = upsert_source_record(
        session,
        source_type=SourceType.LINKEDIN_ALERT,
        external_id=job_id,
        url=f"https://www.linkedin.com/jobs/view/{job_id}/",
        opportunity=o,
    )
    if description:
        add_snapshot(session, record, description)
    session.commit()
    return o


def test_row_shows_the_description_collapsed(client, session):
    o = vacancy(session, description="About the job\nWe use Python and FastAPI.\nRemote.")
    assert 'data-has-description="1"' in client.get("/").text
    panel = client.get(f"/opportunities/{o.id}/panel").text
    assert 'class="desc-text' in panel and "We use <mark>Python</mark> and FastAPI." in panel


def test_no_description_block_without_a_description(client, session):
    vacancy(session)
    assert "data-description=" not in client.get("/").text


def test_capture_returns_affected_opportunity_ids(session):
    o = vacancy(session, job_id="701")
    stats = import_capture(
        session, {"cards": [], "detail": {"job_id": "701", "description": "Go only."}}
    )
    assert stats.opportunity_ids == [o.id]
    new = import_capture(
        session, {"cards": [{"job_id": "702", "text": "Python Developer\nNew Co\nSpain (Remote)"}]}
    )
    created = session.scalar(select(Opportunity).where(Opportunity.title == "Python Developer"))
    assert new.opportunity_ids == [created.id]


def test_single_row_endpoint(client, session):
    o = vacancy(session, description="We use Python.")
    response = client.get(f"/opportunities/{o.id}/row?tab=review")
    assert response.status_code == 200
    assert f'id="opportunity-{o.id}"' in response.text and 'hx-swap-oob="true"' in response.text
    assert "<table" not in response.text


def test_single_row_endpoint_when_the_row_left_the_tab(client, session):
    o = vacancy(session, state=OpportunityState.AUTO_IGNORE)
    response = client.get(f"/opportunities/{o.id}/row?tab=review")
    assert f'id="opportunity-{o.id}"' not in response.text and 'hx-swap-oob="true"' in response.text


def test_single_row_endpoint_unknown(client):
    assert client.get("/opportunities/999/row").status_code == 404


def test_queue_listens_and_capture_page_signals_and_closes(client, session):
    vacancy(session)
    queue = client.get("/").text
    capture = client.get("/capture").text
    assert 'BroadcastChannel("job-radar")' in queue and "/row?" in queue
    assert 'BroadcastChannel("job-radar")' in capture and "window.close()" in capture


def test_description_opens_in_a_modal_with_python_highlighted(client, session):
    o = vacancy(session, description="About the job\nWe use Python & <b>FastAPI</b>.\npython3 too.")
    text = client.get(f"/opportunities/{o.id}/panel").text
    assert "<mark>Python</mark>" in text and "<mark>python</mark>3" in text
    assert "&lt;b&gt;FastAPI&lt;/b&gt;" in text  # the text is escaped, only <mark> is markup
