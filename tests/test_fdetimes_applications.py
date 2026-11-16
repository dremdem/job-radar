"""FDE Times application confirmations from mail (#15, docs/applications.md).

Made up after the real mail's shape: sender `FDE Times <account@fdetimes.net>`, subject
`Application received: <role> at <company>`, a snippet that opens with a newsletter teaser.
"""

from datetime import UTC, datetime

from sqlalchemy import select

from job_radar.applications import detect_application, import_applications
from job_radar.contacts import link_and_label
from job_radar.dedup import get_or_create_company
from job_radar.models import (
    Application,
    ApplicationReview,
    ApplicationSource,
    EmailDirection,
    EmailMessage,
    Opportunity,
    OpportunityState,
    SourceRecord,
    SourceType,
)

SENDER = "FDE Times <account@fdetimes.net>"
ROLE = "Senior Backend Engineer, Platform: Search"
SUBJECT = f"Application received: {ROLE} at Northwind"
SNIPPET = (
    "Engineering interviews now grade how you review AI-written code FDE Times Application "
    f"received · 8 November 2026 Your application for {ROLE} at Northwind is in. While"
)
AT = datetime(2026, 11, 8, 8, 0, tzinfo=UTC)


def add_mail(session, msg_id="fde1", subject=SUBJECT, from_addr=SENDER) -> EmailMessage:
    email = EmailMessage(
        gmail_message_id=msg_id,
        gmail_thread_id=f"t-{msg_id}",
        direction=EmailDirection.IN,
        sent_at=AT,
        from_addr=from_addr,
        to_addr="user@example.com",
        subject=subject,
        snippet=SNIPPET,
    )
    session.add(email)
    session.commit()
    return email


def vacancy(session, title=ROLE, company="Northwind", source=SourceType.FDETIMES, ext="nw-1"):
    o = Opportunity(title=title, company=get_or_create_company(session, company))
    session.add(SourceRecord(source_type=source, external_id=ext, url=None, opportunity=o))
    session.commit()
    return o


def test_detects_the_confirmation():
    found = detect_application(SENDER, SUBJECT, SNIPPET, None, None)
    assert (found.source, found.company, found.role) == (
        ApplicationSource.FDETIMES,
        "Northwind",
        ROLE,
    )
    # The last " at " splits: a role may contain "at" itself.
    found = detect_application(
        SENDER, "Application received: Lead at Scale at Acme AI", "", None, None
    )
    assert (found.company, found.role) == ("Acme AI", "Lead at Scale")
    assert detect_application(SENDER, "Confirm your email for FDE Times", "", None, None) is None
    assert detect_application("x <account@example.com>", SUBJECT, "", None, None) is None


def test_links_the_fdetimes_vacancy_and_marks_it_applied(session):
    o = vacancy(session)
    other = vacancy(session, source=SourceType.HIMALAYAS, ext="https://himalayas.app/x")
    email = add_mail(session)
    stats = import_applications(session)
    assert (stats.detected, stats.new) == (1, 1)
    app = session.scalar(select(Application))
    assert (app.source, app.review, app.role, app.opportunity_id) == (
        ApplicationSource.FDETIMES,
        ApplicationReview.CONFIRMED,
        ROLE,
        o.id,
    )
    assert app.applied_at.replace(tzinfo=UTC) == AT and app.email_message_id == email.id
    assert o.state is OpportunityState.APPLIED
    # The copy from another source joined the applied vacancy.
    assert session.get(Opportunity, other.id) is None
    assert {r.source_type for r in o.source_records} == {SourceType.FDETIMES, SourceType.HIMALAYAS}
    assert (email.detected_event, email.company_id) == ("application_confirmation", o.company_id)


def test_another_source_when_fdetimes_has_none(session):
    o = vacancy(session, title=ROLE.upper(), source=SourceType.HIMALAYAS, ext="h1")
    add_mail(session)
    import_applications(session)
    assert session.scalar(select(Application)).opportunity_id == o.id
    assert o.state is OpportunityState.APPLIED


def test_creates_the_vacancy_when_missing(session):
    vacancy(session, title="Another role", ext="nw-2")
    add_mail(session)
    import_applications(session)
    app = session.scalar(select(Application))
    o = session.get(Opportunity, app.opportunity_id)
    assert (o.title, o.company.name, o.state) == (ROLE, "Northwind", OpportunityState.APPLIED)


def test_mail_already_labelled_by_contacts_is_taken_again(session):
    # Before #15 the contact step ran over it first and called it an interview.
    email = add_mail(session)
    email.detected_event = "interview"
    session.commit()
    import_applications(session)
    assert email.detected_event == "application_confirmation"
    assert session.scalar(select(Application)).source is ApplicationSource.FDETIMES


def test_contacts_leave_the_confirmation_alone_and_it_is_imported_once(session):
    add_mail(session)
    import_applications(session)
    link_and_label(session)
    again = import_applications(session)
    assert again.detected == 0
    assert len(session.scalars(select(Application)).all()) == 1
    assert session.scalar(select(EmailMessage)).detected_event == "application_confirmation"
