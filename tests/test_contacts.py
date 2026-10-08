from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from job_radar.contacts import classify, company_history, link_and_label
from job_radar.dedup import get_or_create_company
from job_radar.models import (
    Application,
    ApplicationReview,
    ApplicationSource,
    Company,
    EmailDirection,
    EmailMessage,
    Rejection,
)
from job_radar.rejections import mark_rejected

T0 = datetime(2026, 5, 1, 9, 0, tzinfo=UTC)


def add_email(
    session,
    msg_id,
    *,
    from_addr,
    subject,
    snippet="",
    thread=None,
    direction=EmailDirection.IN,
    at=T0,
    company=None,
    event=None,
):
    email = EmailMessage(
        gmail_message_id=msg_id,
        gmail_thread_id=thread or f"t-{msg_id}",
        direction=direction,
        sent_at=at,
        from_addr=from_addr,
        to_addr="user@example.com",
        subject=subject,
        snippet=snippet,
        company_id=company.id if company else None,
        company_match="exact" if company else None,
        detected_event=event,
    )
    session.add(email)
    session.commit()
    return email


@pytest.mark.parametrize(
    ("subject", "snippet", "label"),
    [
        ("Meeting invite: Northwind | Senior Backend interview", "", "interview"),
        ("Приглашение на собеседование", "", "interview"),
        ("Re: Northwind | How to prepare for your technical task", "", "interview"),
        (
            "Abacum Inc Application Update",
            "Unfortunately, we will not be moving forward",
            "rejection",
        ),
        ("Работодатель не готов пригласить вас на собеседование", "", "rejection"),
        ("Re: Application Status", "Thanks, have a nice day", "other"),
    ],
)
def test_classify(subject, snippet, label):
    assert classify(subject, snippet) == label


def test_thread_propagates_company(session):
    acme = get_or_create_company(session, "Northwind")
    add_email(
        session,
        "a",
        from_addr="Anna <anna@northwind.com>",
        subject="Northwind | Ready to Move Next?",
        thread="thr",
        company=acme,
        event="application_confirmation",
    )
    reply = add_email(
        session,
        "b",
        from_addr="me <user@example.com>",
        subject="Re: Northwind | Ready to Move Next?",
        thread="thr",
        direction=EmailDirection.OUT,
        at=T0 + timedelta(hours=1),
    )
    link_and_label(session)
    session.refresh(reply)
    assert (reply.company_id, reply.company_match) == (acme.id, "exact")


def test_domains_are_learned_but_not_free_mail_or_ats(session):
    acme = get_or_create_company(session, "Northwind")
    add_email(
        session, "a", from_addr="anna@northwind.com", subject="x", company=acme, event="other"
    )
    add_email(session, "g", from_addr="friend@gmail.com", subject="y", company=acme, event="other")
    add_email(session, "w", from_addr="n@ashbyhq.com", subject="z", company=acme, event="other")
    other = add_email(session, "b", from_addr="Bob <bob@northwind.com>", subject="Hello")
    stranger = add_email(session, "c", from_addr="someone@gmail.com", subject="Hi")
    link_and_label(session)
    for email in (other, stranger):
        session.refresh(email)
    session.refresh(acme)
    assert acme.domains == ["northwind.com"]
    assert other.company_id == acme.id
    assert stranger.company_id is None


def test_probable_match_by_name_in_subject(session):
    get_or_create_company(session, "Northwind")
    get_or_create_company(session, "Rain")
    long_name = add_email(
        session, "a", from_addr="x@mailer.example", subject="Your Northwind offer"
    )
    short_name = add_email(session, "b", from_addr="y@mailer.example", subject="Rain forecast")
    link_and_label(session)
    session.refresh(long_name)
    session.refresh(short_name)
    assert long_name.company_match == "probable"
    assert short_name.company_id is None


def test_ats_rejection_from_snippet_is_recorded(session):
    email = add_email(
        session,
        "r",
        from_addr="Abacum Inc <no-reply@ashbyhq.com>",
        subject="Abacum Inc Application Update",
        snippet="Hi Alex, Unfortunately, we will not be moving forward with your application.",
    )
    stats = link_and_label(session)
    session.refresh(email)
    assert stats.rejections == 1
    assert email.detected_event == "rejection"
    rejection = session.scalar(select(Rejection))
    assert rejection.company.name == "Abacum Inc"
    assert link_and_label(session).rejections == 0
    assert session.scalar(select(func.count()).select_from(Rejection)) == 1


def test_recruiter_outreach_is_the_first_inbound_message(session):
    acme = get_or_create_company(session, "Acme Robotics")
    first = add_email(
        session,
        "a",
        from_addr="Jane <jane@acmerobotics.com>",
        subject="Senior Python Engineer role at Acme Robotics",
        thread="thr",
        company=acme,
    )
    later = add_email(
        session,
        "b",
        from_addr="Jane <jane@acmerobotics.com>",
        subject="Re: Senior Python Engineer role at Acme Robotics",
        thread="thr",
        at=T0 + timedelta(days=1),
        company=acme,
    )
    link_and_label(session)
    session.refresh(first)
    session.refresh(later)
    assert (first.detected_event, later.detected_event) == ("recruiter_outreach", "other")


def test_company_history_merges_without_duplicates(session):
    acme = get_or_create_company(session, "Capitole")
    confirmation = add_email(
        session,
        "c",
        from_addr="LinkedIn <jobs-noreply@linkedin.com>",
        subject="Alex, your application was sent to Capitole",
        company=acme,
        event="application_confirmation",
        thread="thr-app",
    )
    session.add(
        Application(
            company_id=acme.id,
            applied_at=T0,
            role="Sr Python Developer",
            source=ApplicationSource.LINKEDIN,
            review=ApplicationReview.CONFIRMED,
            email_message_id=confirmation.id,
        )
    )
    mark_rejected(
        session, company_id=acme.id, rejected_at=T0 + timedelta(days=7), role="Sr Python Developer"
    )
    add_email(
        session,
        "i",
        from_addr="hr@capitole.com",
        subject="Interview invitation",
        company=acme,
        event="interview",
        at=T0 + timedelta(days=3),
    )
    session.commit()

    events = company_history(session, acme.id)
    assert [e.kind for e in events] == ["rejection", "interview", "applied"]
    assert events[2].text == "Sr Python Developer"
    assert events[2].gmail_url == "https://mail.google.com/mail/u/0/#all/thr-app"


def test_company_history_empty(session):
    assert company_history(session, get_or_create_company(session, "Nobody").id) == []


def test_links_made_by_other_modules_count_as_exact(session):
    # Applications/rejections set company_id without company_match.
    acme = get_or_create_company(session, "Northwind")
    source = add_email(
        session,
        "a",
        from_addr="anna@northwind.com",
        subject="x",
        thread="thr",
        event="application_confirmation",
    )
    source.company_id = acme.id
    session.commit()
    reply = add_email(
        session,
        "b",
        from_addr="me <user@example.com>",
        subject="Re: x",
        thread="thr",
        direction=EmailDirection.OUT,
        at=T0 + timedelta(hours=1),
    )
    other = add_email(session, "c", from_addr="bob@northwind.com", subject="Hello")
    stats = link_and_label(session)
    for e in (source, reply, other):
        session.refresh(e)
    assert source.company_match == "exact"
    assert (reply.company_id, other.company_id) == (acme.id, acme.id)
    assert (stats.by_thread, stats.by_domain) == (1, 1)


def test_linkedin_application_viewed_is_linked_exactly(session):
    email = add_email(
        session,
        "v",
        from_addr="LinkedIn <jobs-noreply@linkedin.com>",
        subject="Your application was viewed by genieX",
    )
    link_and_label(session)
    session.refresh(email)
    assert email.company_match == "exact"
    assert email.detected_event == "viewed"


def test_name_match_confirmed_by_sender_domain_is_exact_and_learns_domain(session):
    engine = get_or_create_company(session, "Northwind")
    named = add_email(
        session,
        "a",
        from_addr="Anna - Northwind <anna@northwind.com>",
        subject="Northwind | Ready to Move Next?",
    )
    later = add_email(session, "b", from_addr="hr@northwind.com", subject="Offer details")
    elsewhere = add_email(
        session, "c", from_addr="news@mailer.example", subject="Northwind raises a round"
    )
    link_and_label(session)
    for e in (named, later, elsewhere):
        session.refresh(e)
    session.refresh(engine)
    assert named.company_match == "exact"
    assert engine.domains == ["northwind.com"]
    assert (later.company_id, later.company_match) == (engine.id, "exact")
    assert elsewhere.company_match == "probable"


def test_hibob_is_an_ats_and_wrong_domain_links_are_undone(session):
    # #178: hibob.com was learned as eCarsTrade's domain, so every HiBob mail went there.
    ecars = get_or_create_company(session, "eCarsTrade")
    ecars.domains = ["hibob.com"]
    bob = '"No-reply (via Bob)" <no-reply@hibob.com>'
    own = add_email(
        session,
        "own",
        from_addr=bob,
        subject="Your application was received",
        company=ecars,
        event="other",
    )
    pure = add_email(
        session,
        "p",
        from_addr=bob,
        subject="Your application at Pure App was received",
        company=ecars,
        event="other",
    )
    zepz = add_email(
        session,
        "z",
        from_addr=bob,
        subject="You're All Set! Your Zepz Application for \u200cBackend Software Engineer"
        " (Python)\u200c is In!",
        company=ecars,
        event="other",
    )
    zepz2 = add_email(
        session,
        "z2",
        from_addr=bob,
        subject="Update on Your Application for the \u200cBackend Software Engineer"
        " (Python) Role at Zepz\u200c",
        company=ecars,
        event="other",
    )
    altery = add_email(
        session,
        "a",
        from_addr='"Polina P. (via Bob)" <notifications@hibob.com>',
        subject="Interview for the ‌‌‌Agentic Product Engineer‌‌‌‌ at Altery‌",
    )
    link_and_label(session)
    session.refresh(ecars)
    assert "hibob.com" not in (ecars.domains or [])
    names = {}
    for e in (own, pure, zepz, zepz2, altery):
        session.refresh(e)
        company = session.get(Company, e.company_id) if e.company_id else None
        names[e.gmail_message_id] = company.name if company else None
    assert names == {"own": "eCarsTrade", "p": "Pure App", "z": "Zepz", "z2": "Zepz", "a": None}
