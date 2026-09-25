from job_radar.dedup import get_or_create_company
from job_radar.models import Opportunity, OpportunityState


def add_opportunity(
    session, title="Senior Python Engineer", company="Acme", score=None
) -> Opportunity:
    opportunity = Opportunity(
        title=title, company=get_or_create_company(session, company), score=score
    )
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


def test_reject_flags_all_company_opportunities(client, session):
    one = add_opportunity(session, title="Python Engineer", company="Globex", score=9)
    add_opportunity(session, title="Backend Engineer", company="Globex", score=8)
    add_opportunity(session, title="Other role", company="Initech", score=5)

    response = client.post(f"/opportunities/{one.id}/rejected")
    assert response.status_code == 200
    text = response.text
    # Both Globex rows are flagged, Initech is not.
    assert text.count("rejected ·") == 2
    # Penalised score is shown (9 - 5 = 4.0), not the intrinsic 9.
    assert "4.0" in text
    assert "Rejected by Globex" in text
    # The button now offers to undo.
    assert "un-reject" in text


def test_reject_sorts_to_bottom_but_stays_visible(client, session):
    rejected = add_opportunity(session, title="Great but rejected", company="Globex", score=10)
    add_opportunity(session, title="Modest match", company="Initech", score=3)

    client.post(f"/opportunities/{rejected.id}/rejected")
    text = client.get("/").text
    # Still listed, but below the lower-scored non-rejected one.
    assert "Great but rejected" in text
    assert text.index("Modest match") < text.index("Great but rejected")


def test_reject_is_a_toggle(client, session):
    o = add_opportunity(session, title="Python Engineer", company="Globex", score=9)
    client.post(f"/opportunities/{o.id}/rejected")
    text = client.post(f"/opportunities/{o.id}/rejected").text
    assert "rejected ·" not in text
    assert "un-reject" not in text


def test_queue_shows_company_history(client, session):
    from datetime import UTC, datetime

    from job_radar.models import Application, ApplicationReview, ApplicationSource

    known = add_opportunity(session, title="Known company role")
    session.add(
        Application(
            company_id=known.company_id,
            applied_at=datetime(2026, 3, 12, tzinfo=UTC),
            role="Python Developer",
            source=ApplicationSource.LINKEDIN,
            review=ApplicationReview.CONFIRMED,
        )
    )
    session.commit()
    text = client.get("/").text
    assert "contacted · 1" in text
    assert "already applied 2026-03-12" in text


def test_queue_says_no_contacts(client, session):
    add_opportunity(session, title="Fresh company role")
    assert "no contacts found" in client.get("/").text


def test_queue_rows_have_state_buttons(client, session):
    opportunity = add_opportunity(session)
    text = client.get("/").text
    for state in ("interested", "ignore", "applied"):
        assert (
            f'hx-post="/opportunities/{opportunity.id}/state" hx-vals=\'{{"state": "{state}"}}\''
            in text
        )


def test_history_is_expanded_and_can_be_toggled(client, session):
    from datetime import UTC, datetime

    from job_radar.models import Application, ApplicationReview, ApplicationSource

    known = add_opportunity(session)
    session.add(
        Application(
            company_id=known.company_id,
            applied_at=datetime(2026, 3, 12, tzinfo=UTC),
            source=ApplicationSource.LINKEDIN,
            review=ApplicationReview.CONFIRMED,
        )
    )
    session.commit()
    text = client.get("/").text
    assert '<details class="history small" open>' in text
    assert 'id="expand-all"' in text and 'id="collapse-all"' in text
