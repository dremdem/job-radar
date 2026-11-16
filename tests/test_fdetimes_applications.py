"""FDE Times application confirmations from mail (#15, docs/applications.md).

Made up after the real mail's shape: sender `FDE Times <account@fdetimes.net>`, subject
`Application received: <role> at <company>`, a snippet that opens with a newsletter teaser.
"""

import datetime as dt

import sqlalchemy as sa

import job_radar.dedup as dedup
import job_radar.history.applications as history_applications
import job_radar.history.contacts as history_contacts
import job_radar.models as models

SENDER = "FDE Times <account@fdetimes.net>"
ROLE = "Senior Backend Engineer, Platform: Search"
SUBJECT = f"Application received: {ROLE} at Northwind"
SNIPPET = (
    "Engineering interviews now grade how you review AI-written code FDE Times Application "
    f"received · 8 November 2026 Your application for {ROLE} at Northwind is in. While"
)
AT = dt.datetime(2026, 11, 8, 8, 0, tzinfo=dt.UTC)


def add_mail(session, msg_id="fde1", subject=SUBJECT, from_addr=SENDER) -> models.EmailMessage:
    email = models.EmailMessage(
        gmail_message_id=msg_id,
        gmail_thread_id=f"t-{msg_id}",
        direction=models.EmailDirection.IN,
        sent_at=AT,
        from_addr=from_addr,
        to_addr="user@example.com",
        subject=subject,
        snippet=SNIPPET,
    )
    session.add(email)
    session.commit()
    return email


def vacancy(
    session, title=ROLE, company="Northwind", source=models.SourceType.FDETIMES, ext="nw-1"
):
    o = models.Opportunity(title=title, company=dedup.get_or_create_company(session, company))
    session.add(models.SourceRecord(source_type=source, external_id=ext, url=None, opportunity=o))
    session.commit()
    return o


def test_detects_the_confirmation():
    found = history_applications.detect_application(SENDER, SUBJECT, SNIPPET, None, None)
    assert (found.source, found.company, found.role) == (
        models.ApplicationSource.FDETIMES,
        "Northwind",
        ROLE,
    )
    # The last " at " splits: a role may contain "at" itself.
    found = history_applications.detect_application(
        SENDER, "Application received: Lead at Scale at Acme AI", "", None, None
    )
    assert (found.company, found.role) == ("Acme AI", "Lead at Scale")
    assert (
        history_applications.detect_application(
            SENDER, "Confirm your email for FDE Times", "", None, None
        )
        is None
    )
    assert (
        history_applications.detect_application("x <account@example.com>", SUBJECT, "", None, None)
        is None
    )


def test_links_the_fdetimes_vacancy_and_marks_it_applied(session):
    o = vacancy(session)
    other = vacancy(session, source=models.SourceType.HIMALAYAS, ext="https://himalayas.app/x")
    email = add_mail(session)
    stats = history_applications.import_applications(session)
    assert (stats.detected, stats.new) == (1, 1)
    app = session.scalar(sa.select(models.Application))
    assert (app.source, app.review, app.role, app.opportunity_id) == (
        models.ApplicationSource.FDETIMES,
        models.ApplicationReview.CONFIRMED,
        ROLE,
        o.id,
    )
    assert app.applied_at.replace(tzinfo=dt.UTC) == AT and app.email_message_id == email.id
    assert o.state is models.OpportunityState.APPLIED
    # The copy from another source joined the applied vacancy.
    assert session.get(models.Opportunity, other.id) is None
    assert {r.source_type for r in o.source_records} == {
        models.SourceType.FDETIMES,
        models.SourceType.HIMALAYAS,
    }
    assert (email.detected_event, email.company_id) == ("application_confirmation", o.company_id)


def test_another_source_when_fdetimes_has_none(session):
    o = vacancy(session, title=ROLE.upper(), source=models.SourceType.HIMALAYAS, ext="h1")
    add_mail(session)
    history_applications.import_applications(session)
    assert session.scalar(sa.select(models.Application)).opportunity_id == o.id
    assert o.state is models.OpportunityState.APPLIED


def test_creates_the_vacancy_when_missing(session):
    vacancy(session, title="Another role", ext="nw-2")
    add_mail(session)
    history_applications.import_applications(session)
    app = session.scalar(sa.select(models.Application))
    o = session.get(models.Opportunity, app.opportunity_id)
    assert (o.title, o.company.name, o.state) == (
        ROLE,
        "Northwind",
        models.OpportunityState.APPLIED,
    )


def test_mail_already_labelled_by_contacts_is_taken_again(session):
    # Before #15 the contact step ran over it first and called it an interview.
    email = add_mail(session)
    email.detected_event = "interview"
    session.commit()
    history_applications.import_applications(session)
    assert email.detected_event == "application_confirmation"
    assert session.scalar(sa.select(models.Application)).source is models.ApplicationSource.FDETIMES


def test_contacts_leave_the_confirmation_alone_and_it_is_imported_once(session):
    add_mail(session)
    history_applications.import_applications(session)
    history_contacts.link_and_label(session)
    again = history_applications.import_applications(session)
    assert again.detected == 0
    assert len(session.scalars(sa.select(models.Application)).all()) == 1
    assert (
        session.scalar(sa.select(models.EmailMessage)).detected_event == "application_confirmation"
    )
