"""Deleting a record from the company history (#225): any record, and it stays deleted."""

import datetime as dt

import contacts_fakes
import linkedin_rejections_fakes
import manual_fakes
import sqlalchemy as sa

import job_radar.history.applications as history_applications
import job_radar.history.contacts as history_contacts
import job_radar.history.rejections as history_rejections
import job_radar.models as models
import job_radar.review.notes as review_notes
import job_radar.sources.linkedin.rejections as linkedin_rejections
import job_radar.sources.manual as sources_manual


def kinds(session, company_id) -> list[str]:
    return [e.kind for e in history_contacts.company_history(session, company_id)]


def test_ignored_then_interested_the_ignore_can_be_deleted_from_the_card(client, session):
    o = manual_fakes.vacancy(session)
    note = review_notes.ignore_with_reason(session, o, models.NoteKind.SALARY, comment="too low")
    # changed your mind
    o.state = models.OpportunityState.INTERESTED
    session.commit()
    panel = client.get(f"/opportunities/{o.id}/panel").text
    assert f'hx-post="/history/note/{note.id}/delete"' in panel
    # one record at a time, and asked first
    assert "hx-confirm=" in panel

    response = client.post(
        f"/history/note/{note.id}/delete", data={"from": "panel", "opportunity": o.id}
    )
    assert response.status_code == 200
    assert "too low" not in response.text
    # Really gone: a fresh page does not bring it back, and the state stays yours.
    assert "too low" not in client.get(f"/opportunities/{o.id}").text
    assert session.scalar(sa.select(sa.func.count()).select_from(models.OpportunityNote)) == 0
    session.refresh(o)
    assert o.state is models.OpportunityState.INTERESTED


def test_every_record_on_the_card_has_a_delete_button(client, session):
    o = manual_fakes.vacancy(session)
    contacts_fakes.add_email(
        session, "m1", from_addr="anna@acme.com", subject="Quick chat?", company=o.company
    )
    sources_manual.add_event(session, o, "call", dt.datetime(2026, 10, 1, tzinfo=dt.UTC), "intro")
    sources_manual.add_event(session, o, "applied", dt.datetime(2026, 9, 1, tzinfo=dt.UTC))
    sources_manual.add_event(session, o, "rejection", dt.datetime(2026, 10, 2, tzinfo=dt.UTC))
    history = history_contacts.company_history(session, o.company_id)
    # the email, the call, the application and the rejection
    assert len(history) == 4
    page = client.get(f"/opportunities/{o.id}").text
    for e in history:
        assert e.ref is not None, e.kind
        kind, item_id = e.ref
        assert f'hx-post="/history/{kind}/{item_id}/delete"' in page, e.kind


def test_mail_records_stay_deleted_after_the_next_sync(session):

    o = manual_fakes.vacancy(session, title="Python engineer", company="SCUBE")
    # A LinkedIn rejection, an ATS confirmation and a plain email.
    linkedin_rejections_fakes.add_email(
        session,
        "r1",
        subject="Your application to Python engineer at SCUBE",
        html=linkedin_rejections_fakes.fixture("scube"),
    )
    linkedin_rejections.import_rejections(session)
    plain = contacts_fakes.add_email(
        session, "e1", from_addr="hr@scube.com", subject="Hello from SCUBE", company=o.company
    )
    session.add(
        models.Application(
            company_id=o.company_id,
            applied_at=dt.datetime(2026, 9, 1, tzinfo=dt.UTC),
            role="Python engineer",
            source=models.ApplicationSource.ATS,
            review=models.ApplicationReview.CONFIRMED,
        )
    )
    session.commit()
    history_contacts.link_and_label(session)
    assert set(kinds(session, o.company_id)) >= {"rejection", "applied"}
    assert o.company_id in history_rejections.latest_rejections(session)

    for e in history_contacts.company_history(session, o.company_id):
        assert history_contacts.delete_history_item(session, *e.ref), e.kind
    assert kinds(session, o.company_id) == []
    # The rejection no longer counts for the score.
    assert o.company_id not in history_rejections.latest_rejections(session)
    assert session.scalar(sa.select(sa.func.count()).select_from(models.Rejection)) == 0

    # The mail is still in the database and in Gmail; the next sync must not bring it back.
    linkedin_rejections.import_rejections(session)
    history_applications.import_applications(session)
    history_contacts.link_and_label(session)
    assert kinds(session, o.company_id) == []
    assert session.scalar(sa.select(sa.func.count()).select_from(models.Rejection)) == 0
    session.refresh(plain)
    assert plain.hidden_at is not None


def test_unknown_record(client, session):
    o = manual_fakes.vacancy(session)
    data = {"from": "panel", "opportunity": o.id}
    assert client.post("/history/note/999/delete", data=data).status_code == 404
    assert client.post("/history/party/1/delete", data=data).status_code == 404
