"""Tests for job_radar.manual: events and vacancies added by hand, from the code and
the web UI (docs/queue.md#add-by-hand).
"""

import datetime as dt

import pytest
import sqlalchemy as sa

import job_radar.contacts as contacts
import job_radar.dedup as dedup
import job_radar.manual as manual
import job_radar.models as models


def vacancy(session, title="Python Developer", company="Acme"):
    o = models.Opportunity(title=title, company=dedup.get_or_create_company(session, company))
    session.add(o)
    session.commit()
    return o


@pytest.mark.parametrize(
    "link, expected",
    [
        (
            "https://www.linkedin.com/jobs/view/agentic-product-engineer-at-altery-4466058649/",
            (models.SourceType.LINKEDIN_LINK, "4466058649"),
        ),
        (
            "https://www.linkedin.com/jobs/view/4466058649/?ref=x",
            (models.SourceType.LINKEDIN_LINK, "4466058649"),
        ),
        ("https://hh.ru/vacancy/137431262?from=x", (models.SourceType.HH, "137431262")),
        ("https://jobs.example.com/42", (models.SourceType.MANUAL, "https://jobs.example.com/42")),
    ],
)
def test_source_for_link(link, expected):
    assert manual.source_for_link(link)[:2] == expected
    assert manual.source_for_link("  ") is None


def test_events_show_in_history_and_applied_rejection_use_their_models(session):
    o = vacancy(session)
    at = dt.datetime(2026, 10, 8, 13, 30, tzinfo=dt.UTC)
    manual.add_event(session, o, "interview", at, "intro call with HR")
    manual.add_event(
        session, o, "applied", dt.datetime(2026, 9, 12, tzinfo=dt.UTC), "via careers page"
    )
    manual.add_event(session, o, "rejection", dt.datetime(2026, 10, 9, tzinfo=dt.UTC))
    session.refresh(o)
    assert o.state is models.OpportunityState.APPLIED
    assert session.scalars(sa.select(models.CompanyEvent)).one().kind == "interview"
    app = session.scalars(sa.select(models.Application)).one()
    assert app.source is models.ApplicationSource.MANUAL and app.role == o.title
    assert session.scalars(sa.select(models.Rejection)).one().opportunity_id == o.id
    history = contacts.company_history(session, o.company_id)
    assert [e.kind for e in history] == ["rejection", "interview", "applied"]
    assert history[1].text == "intro call with HR" and history[1].event_id is not None
    with pytest.raises(ValueError):
        manual.add_event(session, o, "party", at)


def test_add_vacancy_scores_links_and_refuses_duplicates(session):
    o = manual.add_vacancy(
        session,
        title="Senior Python Developer",
        company="Altery",
        link="https://www.linkedin.com/jobs/view/agentic-product-engineer-at-altery-4466058649/",
        location="Remote",
        state=models.OpportunityState.APPLIED,
        comment="found on a Telegram channel",
    )
    assert o.score and o.score_reasons and o.state is models.OpportunityState.APPLIED
    assert {(r.source_type, r.external_id) for r in o.source_records} == {
        (models.SourceType.LINKEDIN_LINK, "4466058649")
    }
    assert (
        session.scalars(sa.select(models.Application)).one().notes == "found on a Telegram channel"
    )
    # The same LinkedIn job from an alert is the same vacancy.
    other = vacancy(session, "Backend", "Globex")
    dedup.upsert_source_record(
        session,
        source_type=models.SourceType.LINKEDIN_ALERT,
        external_id="777",
        url=None,
        opportunity=other,
    )
    session.commit()
    assert manual.find_by_link(session, "https://www.linkedin.com/jobs/view/777/") == other
    with pytest.raises(ValueError, match=f"#{o.id}"):
        manual.add_vacancy(
            session, title="x", company="y", link="https://linkedin.com/jobs/view/4466058649"
        )
    with pytest.raises(ValueError):
        manual.add_vacancy(session, title=" ", company="y")


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
    event = session.scalars(sa.select(models.CompanyEvent)).one()
    assert f'hx-post="/history/event/{event.id}/delete"' in response.text
    data = {"from": "panel", "opportunity": o.id}
    page = client.post(f"/history/event/{event.id}/delete", data=data).text
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
    o = session.scalars(
        sa.select(models.Opportunity).where(models.Opportunity.id != first.id)
    ).one()
    assert response.headers["location"] == f"/opportunities/{o.id}"
    dup = client.post(
        "/opportunities",
        data={"title": "x", "company": "y", "link": "https://hh.ru/vacancy/1", "state": "new"},
    )
    # followed the redirect to the new vacancy
    assert dup.status_code == 200
    again = client.post(
        "/opportunities",
        data={"title": "x", "company": "y", "link": "https://hh.ru/vacancy/1", "state": "new"},
        follow_redirects=False,
    )
    assert again.status_code == 422 and "Already here" in again.text
