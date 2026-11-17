"""A stored email for the company history tests.

Shared by the tests of several modules; see test_contacts.py for how it is used.
"""

import datetime as dt

import job_radar.models as models

T0 = dt.datetime(2026, 5, 1, 9, 0, tzinfo=dt.UTC)


def add_email(
    session,
    msg_id,
    *,
    from_addr,
    subject,
    snippet="",
    thread=None,
    direction=models.EmailDirection.IN,
    at=T0,
    company=None,
    event=None,
):
    email = models.EmailMessage(
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
