import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import func, select

from job_radar.applications import (
    application_status,
    confirm_all,
    detect_application,
    dismiss,
    import_applications,
)
from job_radar.dedup import get_or_create_company, upsert_source_record
from job_radar.models import (
    Application,
    ApplicationReview,
    ApplicationSource,
    EmailDirection,
    EmailMessage,
    Opportunity,
    SourceType,
)
from job_radar.rejections import mark_rejected

FIXTURES = Path(__file__).parent / "fixtures" / "applications"
LINKEDIN = "LinkedIn <jobs-noreply@linkedin.com>"
SENT_SUBJECT = "Alex, your application was sent to VySystems"
T0 = datetime(2026, 9, 23, 10, 0, tzinfo=UTC)


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text()


def test_fixtures_contain_no_personal_data():
    for path in FIXTURES.iterdir():
        assert not re.search(r"midToken|midSig|licdn|@gmail|Yakovenko|Vladimir", path.read_text())


def test_detect_linkedin_confirmation_with_body():
    found = detect_application(
        LINKEDIN, SENT_SUBJECT, "", fixture("linkedin_sent.html"), fixture("linkedin_sent.txt")
    )
    assert found.source is ApplicationSource.LINKEDIN
    assert (found.company, found.role, found.linkedin_job_id) == (
        "VySystems",
        "Python Developer",
        "4470446635",
    )
    assert found.needs_body is False


def test_detect_linkedin_confirmation_without_body_asks_for_it():
    found = detect_application(LINKEDIN, SENT_SUBJECT, "", None, None)
    assert (found.company, found.role, found.needs_body) == ("VySystems", None, True)


@pytest.mark.parametrize(
    ("from_addr", "subject", "snippet", "company", "role"),
    [
        (
            "Hugging Face <noreply@candidates.workablemail.com>",
            "Thanks for applying to Hugging Face",
            "Hi Alex, Thank you for applying to the Senior Open-Source Python Engineer, "
            "ML Developer Tools - EMEA Remote opportunity at Hugging Face.",
            "Hugging Face",
            "Senior Open-Source Python Engineer, ML Developer Tools - EMEA Remote",
        ),
        (
            "no-reply@us.greenhouse-mail.io",
            "Thank you for applying to Zone & Co!",
            "Hi ALEX, Thank you for applying to AI Software Engineer at Zone! We've received it.",
            "Zone & Co",
            "AI Software Engineer",
        ),
        (
            "no-reply@us.greenhouse-mail.io",
            "We received your application — Software Engineer, Backend at Seesaw",
            "",
            "Seesaw",
            "Software Engineer, Backend",
        ),
        (
            "Lingaro <no-reply@hire.lever.co>",
            "Lingaro - ALEX, we received your application!",
            "",
            "Lingaro",
            None,
        ),
        (
            '"Base360 & The Flex hiring team" <no-reply@ashbyhq.com>',
            "Your application has been received",
            "Dear Alex, Thank you for submitting your application for the position of the "
            "Senior Software Engineer position.",
            "Base360 & The Flex",
            "Senior Software Engineer",
        ),
        (
            "SPD Technology <noreply@candidates.workablemail.com>",
            "Senior Python Developer - SPD Technology",
            "Hi Alex, thank you for applying for the Senior Python Developer role.",
            "SPD Technology",
            "Senior Python Developer",
        ),
        (
            "<noreply@northwind.teamtailor-mail.com>",
            "Application received",
            "Thanks for your application.",
            "northwind",
            None,
        ),
    ],
)
def test_detect_ats_confirmation(from_addr, subject, snippet, company, role):
    found = detect_application(from_addr, subject, snippet, None, None)
    assert found is not None
    assert found.source is ApplicationSource.ATS
    assert (found.company, found.role) == (company, role)


@pytest.mark.parametrize(
    ("from_addr", "subject", "snippet"),
    [
        ("CI&T <no-reply@hire.lever.co>", "[CI&T] - Personal Data Privacy", "In compliance with"),
        (
            "Deltia <no-reply@ashbyhq.com>",
            "Deltia Application Update",
            "Unfortunately, we will not",
        ),
        ("Jane <jane@acme.example>", "Thanks for applying to Acme", "Thank you for applying"),
    ],
)
def test_not_an_application_confirmation(from_addr, subject, snippet):
    assert detect_application(from_addr, subject, snippet, None, None) is None


def add_email(session, msg_id, *, from_addr, subject, snippet="", html=None, text=None, at=T0):
    email = EmailMessage(
        gmail_message_id=msg_id,
        gmail_thread_id=f"t-{msg_id}",
        direction=EmailDirection.IN,
        sent_at=at,
        from_addr=from_addr,
        to_addr="user@example.com",
        subject=subject,
        snippet=snippet,
        body_html=html,
        body_text=text,
    )
    session.add(email)
    session.commit()
    return email


def add_linkedin(session, msg_id="li1", at=T0):
    return add_email(
        session,
        msg_id,
        from_addr=LINKEDIN,
        subject=SENT_SUBJECT,
        html=fixture("linkedin_sent.html"),
        text=fixture("linkedin_sent.txt"),
        at=at,
    )


def count(session) -> int:
    return session.scalar(select(func.count()).select_from(Application))


def test_import_is_idempotent_and_marks_emails(session):
    email = add_linkedin(session)
    stats = import_applications(session)
    assert (stats.detected, stats.new) == (1, 1)
    assert import_applications(session).new == 0
    app = session.scalar(select(Application))
    assert app.company.name == "VySystems"
    assert app.role == "Python Developer"
    assert app.review is ApplicationReview.CONFIRMED
    assert app.applied_at.replace(tzinfo=UTC) == T0
    session.refresh(email)
    assert email.detected_event == "application_confirmation"
    assert email.company_id == app.company_id


def test_ats_confirmation_is_a_suggestion(session):
    add_email(
        session,
        "ats1",
        from_addr="Lingaro <no-reply@hire.lever.co>",
        subject="Lingaro - ALEX, we received your application!",
    )
    import_applications(session)
    assert session.scalar(select(Application)).review is ApplicationReview.SUGGESTED


def test_linkedin_and_ats_mail_for_one_application_make_one_record(session):
    add_linkedin(session)
    add_email(
        session,
        "ats1",
        from_addr="VySystems <noreply@candidates.workablemail.com>",
        subject="Thanks for applying to VySystems",
        at=T0 + timedelta(days=1),
    )
    import_applications(session)
    assert count(session) == 1


def test_applications_far_apart_are_separate(session):
    add_linkedin(session, "li1", at=T0)
    add_linkedin(session, "li2", at=T0 + timedelta(days=60))
    import_applications(session)
    assert count(session) == 2


def test_dismissed_suggestion_is_not_recreated(session):
    add_email(
        session,
        "ats1",
        from_addr="Lingaro <no-reply@hire.lever.co>",
        subject="Lingaro - ALEX, we received your application!",
    )
    import_applications(session)
    dismiss(session, session.scalar(select(Application)).id)
    import_applications(session)
    assert count(session) == 1
    assert session.scalar(select(Application)).review is ApplicationReview.DISMISSED


def test_confirm_all(session):
    for i, company in enumerate(["Lingaro", "Provectus"]):
        add_email(
            session,
            f"ats{i}",
            from_addr=f"{company} <no-reply@hire.lever.co>",
            subject=f"{company} - ALEX, we received your application!",
        )
    import_applications(session)
    assert confirm_all(session) == 2
    reviews = {a.review for a in session.scalars(select(Application))}
    assert reviews == {ApplicationReview.CONFIRMED}


def test_linkedin_job_id_links_known_opportunity(session):
    opportunity = Opportunity(
        title="Python Developer", company=get_or_create_company(session, "VySystems")
    )
    session.add(opportunity)
    upsert_source_record(
        session,
        source_type=SourceType.LINKEDIN_ALERT,
        external_id="4470446635",
        url="https://www.linkedin.com/jobs/view/4470446635/",
        opportunity=opportunity,
    )
    session.commit()
    add_linkedin(session)
    import_applications(session)
    app = session.scalar(select(Application))
    assert app.opportunity_id == opportunity.id
    assert app.linkedin_job_id == "4470446635"


class FakeFetch:
    def __init__(self, bodies):
        self.bodies, self.calls = bodies, []

    def __call__(self, gmail_message_id):
        self.calls.append(gmail_message_id)
        return self.bodies[gmail_message_id]


def test_bodyless_linkedin_confirmation_fetches_role_once(session):
    email = add_email(session, "old1", from_addr=LINKEDIN, subject=SENT_SUBJECT)
    fetch = FakeFetch({"old1": (fixture("linkedin_sent.html"), fixture("linkedin_sent.txt"))})
    stats = import_applications(session, fetch_body=fetch)
    assert (stats.fetched, stats.new) == (1, 1)
    assert session.scalar(select(Application)).role == "Python Developer"
    session.refresh(email)
    assert email.body_html is None and email.body_text is None
    import_applications(session, fetch_body=fetch)
    assert fetch.calls == ["old1"]


def test_bodyless_linkedin_confirmation_without_gmail_still_records_company(session):
    add_email(session, "old1", from_addr=LINKEDIN, subject=SENT_SUBJECT)
    stats = import_applications(session)
    assert (stats.new, stats.pending) == (1, 1)
    app = session.scalar(select(Application))
    assert (app.company.name, app.role) == ("VySystems", None)


def test_status_rejected_only_by_later_rejection(session):
    add_linkedin(session)
    import_applications(session)
    app = session.scalar(select(Application))
    assert application_status(session, app) == "applied"
    mark_rejected(session, company_id=app.company_id, rejected_at=T0 - timedelta(days=90))
    assert application_status(session, app) == "applied"
    mark_rejected(session, company_id=app.company_id, rejected_at=T0 + timedelta(days=5))
    assert application_status(session, app) == "rejected"


def test_linkedin_fact_confirms_an_earlier_ats_suggestion(session):
    add_email(
        session,
        "ats1",
        from_addr="VySystems <noreply@candidates.workablemail.com>",
        subject="Thanks for applying to VySystems",
        at=T0 - timedelta(hours=2),
    )
    import_applications(session)
    add_linkedin(session)
    import_applications(session)
    app = session.scalar(select(Application))
    assert count(session) == 1
    assert app.review is ApplicationReview.CONFIRMED
    assert app.role == "Python Developer"


def test_linkedin_does_not_override_a_dismissal(session):
    add_email(
        session,
        "ats1",
        from_addr="VySystems <noreply@candidates.workablemail.com>",
        subject="Thanks for applying to VySystems",
    )
    import_applications(session)
    dismiss(session, session.scalar(select(Application)).id)
    add_linkedin(session)
    import_applications(session)
    assert session.scalar(select(Application)).review is ApplicationReview.DISMISSED


@pytest.mark.parametrize(
    ("from_addr", "subject", "snippet", "company", "role"),
    [
        # Recruiter's name in front of the company.
        (
            "Jane - Northwind <no-reply@northwind.teamtailor-mail.com>",
            "We have received your application!",
            "",
            "Northwind",
            None,
        ),
        (
            "WLG Team \u2013 WLG <no-reply@wlg.teamtailor-mail.com>",
            "We have received your application!",
            "",
            "WLG",
            None,
        ),
        # Service words around the company.
        (
            "Intetics No Reply <no-reply@hire.lever.co>",
            "Application received",
            "",
            "Intetics",
            None,
        ),
        (
            "Careers @ Hugging Face <noreply@candidates.workablemail.com>",
            "Application received",
            "",
            "Hugging Face",
            None,
        ),
        # "your application to <role>" names the role, not the company.
        (
            "Acme Corp <no-reply@ashbyhq.com>",
            "Thank you for your application to 1209970 Software Developer C#",
            "",
            "Acme Corp",
            None,
        ),
        # A role must not run past the end of a sentence.
        (
            "Mozilla <no-reply@us.greenhouse-mail.io>",
            "Thank you for applying to Mozilla!",
            "Thank you for applying to Mozilla! In the spirit of transparency, a valued part.",
            "Mozilla",
            None,
        ),
        (
            "Semrush <no-reply@ashbyhq.com>",
            "Your application has been received",
            "Thank you for applying for the opportunity of our Senior Backend Developer role.",
            "Semrush",
            "Senior Backend Developer",
        ),
    ],
)
def test_detect_ats_names_are_cleaned(from_addr, subject, snippet, company, role):
    found = detect_application(from_addr, subject, snippet, None, None)
    assert (found.company, found.role) == (company, role)


@pytest.mark.parametrize(
    "subject",
    [
        "Abacum Inc Application Update",
        "Information about your Nethermind application",
        "Follow up on your interest in Python Developer",
        "Update on your application",
    ],
)
def test_status_updates_are_not_new_applications(subject):
    snippet = "Thank you for applying. After careful review of your background"
    assert detect_application("Acme <no-reply@ashbyhq.com>", subject, snippet, None, None) is None


@pytest.mark.parametrize(
    ("from_addr", "subject", "company"),
    [
        (
            "Nagarro <no-reply@hire.lever.co>",
            "Alex, thank you for applying to join Nagarro",
            "Nagarro",
        ),
        ("Paychex @ icims <no-reply@icims.com>", "Application received", "Paychex"),
        ("Workday@intive <intive@myworkday.com>", "Thank You For Applying!", "intive"),
    ],
)
def test_ats_name_wrappers_are_removed(from_addr, subject, company):
    assert detect_application(from_addr, subject, "", None, None).company == company
