"""Rejection emails from fixtures, stored the way the Gmail sync stores them.

Shared by the tests of several modules; see test_linkedin_rejections.py for how it is used.
"""

import datetime as dt

import paths

import job_radar.models as models

FIXTURES = paths.FIXTURES / "rejections"


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
