from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from job_radar.contacts import company_history
from job_radar.dedup import get_or_create_company, upsert_source_record
from job_radar.manual import add_event, add_vacancy, find_by_link, source_for_link
from job_radar.models import (
    Application,
    ApplicationSource,
    CompanyEvent,
    Opportunity,
    OpportunityState,
    Rejection,
    SourceType,
)


def vacancy(session, title="Python Developer", company="Acme"):
    o = Opportunity(title=title, company=get_or_create_company(session, company))
    session.add(o)
    session.commit()
    return o


@pytest.mark.parametrize(
    "link, expected",
    [
        (
            "https://www.linkedin.com/jobs/view/agentic-product-engineer-at-altery-4466058649/",
            (SourceType.LINKEDIN_LINK, "4466058649"),
        ),
        (
            "https://www.linkedin.com/jobs/view/4466058649/?ref=x",
            (SourceType.LINKEDIN_LINK, "4466058649"),
        ),
        ("https://hh.ru/vacancy/137431262?from=x", (SourceType.HH, "137431262")),
        ("https://jobs.example.com/42", (SourceType.MANUAL, "https://jobs.example.com/42")),
    ],
)
def test_source_for_link(link, expected):
    assert source_for_link(link)[:2] == expected
    assert source_for_link("  ") is None


def test_events_show_in_history_and_applied_rejection_use_their_models(session):
    o = vacancy(session)
    at = datetime(2026, 10, 8, 13, 30, tzinfo=UTC)
    add_event(session, o, "interview", at, "intro call with HR")
    add_event(session, o, "applied", datetime(2026, 9, 12, tzinfo=UTC), "via careers page")
    add_event(session, o, "rejection", datetime(2026, 10, 9, tzinfo=UTC))
    session.refresh(o)
    assert o.state is OpportunityState.APPLIED
    assert session.scalars(select(CompanyEvent)).one().kind == "interview"
    app = session.scalars(select(Application)).one()
    assert app.source is ApplicationSource.MANUAL and app.role == o.title
    assert session.scalars(select(Rejection)).one().opportunity_id == o.id
    history = company_history(session, o.company_id)
    assert [e.kind for e in history] == ["rejection", "interview", "applied"]
    assert history[1].text == "intro call with HR" and history[1].event_id is not None
    with pytest.raises(ValueError):
        add_event(session, o, "party", at)


def test_add_vacancy_scores_links_and_refuses_duplicates(session):
    o = add_vacancy(
        session,
        title="Senior Python Developer",
        company="Altery",
        link="https://www.linkedin.com/jobs/view/agentic-product-engineer-at-altery-4466058649/",
        location="Remote",
        state=OpportunityState.APPLIED,
        comment="found on a Telegram channel",
    )
    assert o.score and o.score_reasons and o.state is OpportunityState.APPLIED
    assert {(r.source_type, r.external_id) for r in o.source_records} == {
        (SourceType.LINKEDIN_LINK, "4466058649")
    }
    assert session.scalars(select(Application)).one().notes == "found on a Telegram channel"
    # The same LinkedIn job from an alert is the same vacancy.
    other = vacancy(session, "Backend", "Globex")
    upsert_source_record(
        session,
        source_type=SourceType.LINKEDIN_ALERT,
        external_id="777",
        url=None,
        opportunity=other,
    )
    session.commit()
    assert find_by_link(session, "https://www.linkedin.com/jobs/view/777/") == other
    with pytest.raises(ValueError, match=f"#{o.id}"):
        add_vacancy(
            session, title="x", company="y", link="https://linkedin.com/jobs/view/4466058649"
        )
    with pytest.raises(ValueError):
        add_vacancy(session, title=" ", company="y")


def test_web_add_event_and_delete_it(client, session):
    o = vacancy(session)
    panel = client.get(f"/opportunities/{o.id}/panel").text
    assert (
        f'hx-post="/opportunities/{o.id}/events"' in panel and '<option value="interview">' in panel
    )
    response = client.post(
        f"/opportunities/{o.id}/events",
        data={
            "kind": "call",
            "at": "2026-10-08",
            "comment": "recruiter on Telegram",
            "from": "panel",
        },
    )
    assert response.status_code == 200
    assert "recruiter on Telegram" in response.text and "<b>call</b>" in response.text
    event = session.scalars(select(CompanyEvent)).one()
    assert f'hx-post="/events/{event.id}/delete"' in response.text
    page = client.post(f"/events/{event.id}/delete", data={"from": "panel"}).text
    assert "recruiter on Telegram" not in page
    assert client.post(f"/opportunities/{o.id}/events", data={"kind": "party"}).status_code == 422


def test_web_add_vacancy(client, session):
    first = vacancy(session)
    queue = client.get("/").text
    assert 'id="add-vacancy"' in queue and '<dialog id="add-dialog"' in queue
    response = client.post(
        "/opportunities",
        data={
            "title": "Python Engineer",
            "company": "Initech",
            "link": "",
            "location": "Remote",
            "state": "new",
            "comment": "",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    o = session.scalars(select(Opportunity).where(Opportunity.id != first.id)).one()
    assert response.headers["location"] == f"/opportunities/{o.id}"
    dup = client.post(
        "/opportunities",
        data={"title": "x", "company": "y", "link": "https://hh.ru/vacancy/1", "state": "new"},
    )
    assert dup.status_code == 200  # followed the redirect to the new vacancy
    again = client.post(
        "/opportunities",
        data={"title": "x", "company": "y", "link": "https://hh.ru/vacancy/1", "state": "new"},
        follow_redirects=False,
    )
    assert again.status_code == 422 and "Already here" in again.text
