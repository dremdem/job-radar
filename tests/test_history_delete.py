"""Deleting a record from the company history (#225): any record, and it stays deleted."""

from datetime import UTC, datetime

from sqlalchemy import func, select
from test_contacts import add_email
from test_linkedin_rejections import add_email as add_linkedin_email
from test_linkedin_rejections import fixture
from test_manual import vacancy

from job_radar.applications import import_applications
from job_radar.contacts import company_history, link_and_label
from job_radar.linkedin.rejections import import_rejections
from job_radar.manual import add_event
from job_radar.models import (
    Application,
    ApplicationReview,
    ApplicationSource,
    NoteKind,
    OpportunityNote,
    OpportunityState,
    Rejection,
)
from job_radar.notes import ignore_with_reason
from job_radar.rejections import latest_rejections


def kinds(session, company_id) -> list[str]:
    return [e.kind for e in company_history(session, company_id)]


def test_ignored_then_interested_the_ignore_can_be_deleted_from_the_card(client, session):
    o = vacancy(session)
    note = ignore_with_reason(session, o, NoteKind.SALARY, comment="too low")
    o.state = OpportunityState.INTERESTED  # changed your mind
    session.commit()
    panel = client.get(f"/opportunities/{o.id}/panel").text
    assert f'hx-post="/history/note/{note.id}/delete"' in panel
    assert "hx-confirm=" in panel  # one record at a time, and asked first

    response = client.post(
        f"/history/note/{note.id}/delete", data={"from": "panel", "opportunity": o.id}
    )
    assert response.status_code == 200
    assert "too low" not in response.text
    # Really gone: a fresh page does not bring it back, and the state stays yours.
    assert "too low" not in client.get(f"/opportunities/{o.id}").text
    assert session.scalar(select(func.count()).select_from(OpportunityNote)) == 0
    session.refresh(o)
    assert o.state is OpportunityState.INTERESTED


def test_every_record_on_the_card_has_a_delete_button(client, session):
    o = vacancy(session)
    add_email(session, "m1", from_addr="anna@acme.com", subject="Quick chat?", company=o.company)
    add_event(session, o, "call", datetime(2026, 10, 1, tzinfo=UTC), "intro")
    add_event(session, o, "applied", datetime(2026, 9, 1, tzinfo=UTC))
    add_event(session, o, "rejection", datetime(2026, 10, 2, tzinfo=UTC))
    history = company_history(session, o.company_id)
    assert len(history) == 4  # the email, the call, the application and the rejection
    page = client.get(f"/opportunities/{o.id}").text
    for e in history:
        assert e.ref is not None, e.kind
        kind, item_id = e.ref
        assert f'hx-post="/history/{kind}/{item_id}/delete"' in page, e.kind


def test_mail_records_stay_deleted_after_the_next_sync(session):
    from job_radar.contacts import delete_history_item

    o = vacancy(session, title="Python engineer", company="SCUBE")
    # A LinkedIn rejection, an ATS confirmation and a plain email.
    add_linkedin_email(
        session, "r1", subject="Your application to Python engineer at SCUBE", html=fixture("scube")
    )
    import_rejections(session)
    plain = add_email(
        session, "e1", from_addr="hr@scube.com", subject="Hello from SCUBE", company=o.company
    )
    session.add(
        Application(
            company_id=o.company_id,
            applied_at=datetime(2026, 9, 1, tzinfo=UTC),
            role="Python engineer",
            source=ApplicationSource.ATS,
            review=ApplicationReview.CONFIRMED,
        )
    )
    session.commit()
    link_and_label(session)
    assert set(kinds(session, o.company_id)) >= {"rejection", "applied"}
    assert o.company_id in latest_rejections(session)

    for e in company_history(session, o.company_id):
        assert delete_history_item(session, *e.ref), e.kind
    assert kinds(session, o.company_id) == []
    # The rejection no longer counts for the score.
    assert o.company_id not in latest_rejections(session)
    assert session.scalar(select(func.count()).select_from(Rejection)) == 0

    # The mail is still in the database and in Gmail; the next sync must not bring it back.
    import_rejections(session)
    import_applications(session)
    link_and_label(session)
    assert kinds(session, o.company_id) == []
    assert session.scalar(select(func.count()).select_from(Rejection)) == 0
    session.refresh(plain)
    assert plain.hidden_at is not None


def test_unknown_record(client, session):
    o = vacancy(session)
    data = {"from": "panel", "opportunity": o.id}
    assert client.post("/history/note/999/delete", data=data).status_code == 404
    assert client.post("/history/party/1/delete", data=data).status_code == 404
