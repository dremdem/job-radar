from datetime import UTC, datetime

from job_radar.dedup import get_or_create_company
from job_radar.models import EmailDirection, EmailMessage, Opportunity, Rejection, RejectionSource
from job_radar.rejections import (
    REJECTION_PENALTY,
    clear_manual_rejection,
    effective_score,
    latest_rejections,
    mark_rejected,
    toggle_manual_rejection,
)


def _opportunity(session, company_name="Acme", title="Python Engineer", score=None) -> Opportunity:
    opportunity = Opportunity(
        title=title, company=get_or_create_company(session, company_name), score=score
    )
    session.add(opportunity)
    session.commit()
    return opportunity


def _email(session, gmail_message_id="m1") -> EmailMessage:
    email = EmailMessage(
        gmail_message_id=gmail_message_id,
        gmail_thread_id="t1",
        direction=EmailDirection.IN,
        sent_at=datetime(2026, 1, 1, tzinfo=UTC),
        from_addr="jobs-noreply@linkedin.com",
        to_addr="me@example.com",
    )
    session.add(email)
    session.commit()
    return email


def test_effective_score_penalises_rejected():
    assert effective_score(10.0, rejected=False) == 10.0
    assert effective_score(10.0, rejected=True) == 10.0 - REJECTION_PENALTY
    # Never below zero.
    assert effective_score(2.0, rejected=True) == 0.0
    # No score stays no score.
    assert effective_score(None, rejected=True) is None


def test_mark_rejected_flags_the_company(session):
    o = _opportunity(session)
    mark_rejected(session, company_id=o.company_id, opportunity_id=o.id)
    session.commit()
    assert set(latest_rejections(session)) == {o.company_id}


def test_mark_rejected_is_idempotent_per_email(session):
    o = _opportunity(session)
    email = _email(session)
    first = mark_rejected(
        session, company_id=o.company_id, source=RejectionSource.GMAIL, email_message_id=email.id
    )
    second = mark_rejected(
        session, company_id=o.company_id, source=RejectionSource.GMAIL, email_message_id=email.id
    )
    session.commit()
    assert first.id == second.id
    assert session.query(Rejection).count() == 1


def test_latest_rejections_returns_most_recent(session):
    o = _opportunity(session)
    older = _email(session, "m1")
    newer = _email(session, "m2")
    mark_rejected(
        session,
        company_id=o.company_id,
        source=RejectionSource.GMAIL,
        email_message_id=older.id,
        rejected_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    mark_rejected(
        session,
        company_id=o.company_id,
        source=RejectionSource.GMAIL,
        email_message_id=newer.id,
        rejected_at=datetime(2026, 6, 1, tzinfo=UTC),
    )
    session.commit()
    # SQLite returns tz-naive datetimes; the newer (June) rejection wins.
    assert latest_rejections(session)[o.company_id].replace(tzinfo=UTC) == datetime(
        2026, 6, 1, tzinfo=UTC
    )


def test_toggle_manual_rejection_flips(session):
    o = _opportunity(session)
    assert toggle_manual_rejection(session, o) is True
    session.commit()
    assert o.company_id in latest_rejections(session)
    assert toggle_manual_rejection(session, o) is False
    session.commit()
    assert o.company_id not in latest_rejections(session)


def test_clear_manual_keeps_auto(session):
    o = _opportunity(session)
    email = _email(session)
    mark_rejected(session, company_id=o.company_id)  # manual
    mark_rejected(
        session, company_id=o.company_id, source=RejectionSource.GMAIL, email_message_id=email.id
    )
    session.commit()
    clear_manual_rejection(session, o.company_id)
    session.commit()
    # The auto-detected one still flags the company.
    assert o.company_id in latest_rejections(session)
    assert session.query(Rejection).count() == 1
