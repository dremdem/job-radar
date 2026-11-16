"""Tests for job_radar.sources.linkedin.rejections: detecting rejection emails and recording
them on the company (docs/data-model.md).
"""

import datetime as dt
import pathlib
import re

import pytest
import sqlalchemy as sa

import job_radar.history.rejections as history_rejections
import job_radar.mail as mail
import job_radar.models as models
import job_radar.sources.linkedin.alerts as linkedin_alerts
import job_radar.sources.linkedin.rejections as linkedin_rejections

FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "rejections"


def fixture(name: str, ext: str = "html") -> str:
    return (FIXTURES / f"{name}.{ext}").read_text()


def add_email(
    session,
    msg_id,
    *,
    subject,
    html,
    text=None,
    sender="jobs-noreply@linkedin.com",
    sent_at=None,
    categories=("linkedin_alerts",),
):
    email = models.EmailMessage(
        gmail_message_id=msg_id,
        gmail_thread_id=msg_id,
        direction=models.EmailDirection.IN,
        sent_at=sent_at or dt.datetime(2026, 9, 24, tzinfo=dt.UTC),
        from_addr=f"LinkedIn <{sender}>",
        to_addr="user@example.com",
        subject=subject,
        categories=list(categories),
        body_html=html,
        body_text=text,
    )
    session.add(email)
    session.commit()
    return email


def count(session, model) -> int:
    return session.scalar(sa.select(sa.func.count()).select_from(model))


def test_fixtures_contain_no_personal_data():
    for path in sorted(FIXTURES.glob("*.*")):
        text = path.read_text()
        assert not re.search(r"midToken|midSig|licdn|@gmail|Yakovenko|Vladimir", text), path.name


def test_detect_rejection_reads_subject_and_body():
    info = linkedin_rejections.detect_rejection(
        "Your application to AI Python Engineer at ThoughtLabs Belgium",
        "Unfortunately, we will not be moving forward with your application.",
    )
    assert info is not None
    assert info.role == "AI Python Engineer"
    assert info.company == "ThoughtLabs Belgium"


def test_detect_rejection_needs_the_rejection_phrase():
    # Same subject shape, but the body is not a rejection.
    assert (
        linkedin_rejections.detect_rejection(
            "Your application to Python engineer at SCUBE",
            "Good news — your application was viewed by the recruiter.",
        )
        is None
    )


def test_detect_rejection_ignores_unrelated_subjects():
    assert (
        linkedin_rejections.detect_rejection(
            "You have a new job alert", "we will not be moving forward"
        )
        is None
    )


def test_import_records_rejection_and_links_company(session):
    email = add_email(
        session, "m1", subject="Your application to Python engineer at SCUBE", html=fixture("scube")
    )
    stats = linkedin_rejections.import_rejections(session)
    assert (stats.emails, stats.detected, stats.new) == (1, 1, 1)

    rejection = session.scalar(sa.select(models.Rejection))
    assert rejection.company.name == "SCUBE"
    assert rejection.role == "Python engineer"
    assert rejection.rejected_at.replace(tzinfo=dt.UTC) == email.sent_at
    assert rejection.email_message_id == email.id
    # The source email is annotated for provenance.
    session.refresh(email)
    assert email.detected_event == "rejection"
    assert email.company_id == rejection.company_id
    # The company is flagged for the queue.
    assert rejection.company_id in history_rejections.latest_rejections(session)


def test_import_rejections_is_idempotent(session):
    add_email(
        session, "m1", subject="Your application to Python engineer at SCUBE", html=fixture("scube")
    )
    linkedin_rejections.import_rejections(session)
    assert linkedin_rejections.import_rejections(session).new == 0
    assert count(session, models.Rejection) == 1


def test_rejection_email_does_not_create_opportunities(session):
    # The "Top jobs looking for your skills" cards must not become opportunities.
    add_email(
        session, "m1", subject="Your application to Python engineer at SCUBE", html=fixture("scube")
    )
    # ignores jobs-noreply sender
    linkedin_alerts.import_alerts(session)
    linkedin_rejections.import_rejections(session)
    assert count(session, models.Opportunity) == 0
    assert count(session, models.Rejection) == 1


QUBIKA = "Your application to Senior Python / Rust Developer at Qubika"
ORIGINDATA = "Your application to Python Developer (+ Frontend Framework Experience) at Origindata"


def test_real_email_with_stub_plain_text_is_detected(session):
    # Real LinkedIn emails: text/plain is "Your update from <company>" + footer,
    # the rejection sentence is only in the HTML part.
    assert "moving forward" not in fixture("qubika", "txt")
    add_email(session, "m1", subject=QUBIKA, html=fixture("qubika"), text=fixture("qubika", "txt"))
    assert linkedin_rejections.import_rejections(session).new == 1
    assert session.scalar(sa.select(models.Rejection)).company.name == "Qubika"


def test_did_not_select_wording_is_detected(session):
    add_email(
        session,
        "m1",
        subject=ORIGINDATA,
        html=fixture("origindata"),
        text=fixture("origindata", "txt"),
    )
    assert linkedin_rejections.import_rejections(session).new == 1


def test_have_not_selected_wording_is_detected():
    body = "Unfortunately, we have not selected your application to move forward."
    assert linkedin_rejections.detect_rejection(
        "Your application to Python Developer at DevelGuru", body
    )


class FakeFetch:
    """Returns stored message bodies by id and records the calls."""

    def __init__(self, bodies):
        self.bodies = bodies
        self.calls = []

    def __call__(self, gmail_message_id):
        self.calls.append(gmail_message_id)
        return self.bodies[gmail_message_id]


def add_bodyless(session, msg_id, subject):
    # Older application updates arrive via label:job-proposal: headers only.
    return add_email(session, msg_id, subject=subject, html=None, categories=["job_proposal"])


def test_bodyless_emails_are_fetched_once_and_body_not_stored(session):
    email = add_bodyless(session, "old1", QUBIKA)
    viewed = add_bodyless(session, "old2", "Your application to Python Developer at Acme")
    fetch = FakeFetch(
        {
            "old1": (fixture("qubika"), fixture("qubika", "txt")),
            "old2": ("<p>Your application was viewed by Acme</p>", None),
        }
    )
    stats = linkedin_rejections.import_rejections(session, fetch_body=fetch)
    assert (stats.fetched, stats.new) == (2, 1)

    session.refresh(email)
    session.refresh(viewed)
    assert email.detected_event == "rejection"
    assert viewed.detected_event == "application_update"
    # never persisted
    assert email.body_html is None and email.body_text is None

    linkedin_rejections.import_rejections(session, fetch_body=fetch)
    assert fetch.calls == ["old1", "old2"]
    assert count(session, models.Rejection) == 1


def test_bodyless_emails_wait_without_gmail(session):
    add_bodyless(session, "old1", QUBIKA)
    stats = linkedin_rejections.import_rejections(session)
    assert (stats.pending, stats.new) == (1, 0)


@pytest.mark.parametrize(
    "body",
    [
        "After careful consideration, we regret to inform you that we have decided to proceed "
        "with another candidate.",
        "After reviewing your application, we have decided to move forward with other candidates "
        "whose experience more closely matches our needs.",
        "К сожалению, мы решили не рассматривать Вашу заявку на следующем этапе процесса найма.",  # noqa: RUF001 (Russian text)
    ],
)
def test_custom_employer_wording_is_detected(body):
    assert linkedin_rejections.detect_rejection(
        "Your application to Python Developer at Acme", body
    )


def test_positive_update_is_not_a_rejection():
    body = "Congratulations! We would like to move forward with your application and invite you."
    assert (
        linkedin_rejections.detect_rejection("Your application to Python Developer at Acme", body)
        is None
    )


def test_recheck_revisits_emails_checked_before(session):
    email = add_bodyless(session, "old1", "Your application to Python Developer at Acme")
    # checked by older, narrower rules
    email.detected_event = "application_update"
    session.commit()
    fetch = FakeFetch(
        {"old1": ("<p>we regret to inform you that we chose another candidate</p>", None)}
    )

    assert linkedin_rejections.import_rejections(session, fetch_body=fetch).fetched == 0
    assert linkedin_rejections.import_rejections(session, fetch_body=fetch, recheck=True).new == 1


class GoneFetch(FakeFetch):
    """Like FakeFetch, but a missing body means the message is gone from Gmail."""

    def __call__(self, gmail_message_id):

        if self.bodies[gmail_message_id] is None:
            self.calls.append(gmail_message_id)
            raise mail.MessageGone(gmail_message_id)
        return super().__call__(gmail_message_id)


@pytest.mark.parametrize("recheck", [False, True])
def test_email_deleted_from_gmail_is_marked_and_the_rest_goes_on(session, recheck):
    gone = add_bodyless(session, "gone1", "Your application to Python Developer at Acme")
    add_bodyless(session, "old1", QUBIKA)
    fetch = GoneFetch({"gone1": None, "old1": (fixture("qubika"), fixture("qubika", "txt"))})
    stats = linkedin_rejections.import_rejections(session, fetch_body=fetch)
    assert (stats.gone, stats.new) == (1, 1)
    session.refresh(gone)
    assert gone.detected_event == "gone"

    linkedin_rejections.import_rejections(session, fetch_body=fetch, recheck=recheck)
    assert fetch.calls == ["gone1", "old1"]
