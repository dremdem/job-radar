"""Tests for job_radar.rejections: the rejection penalty at display time, idempotent
records and the manual toggle (docs/data-model.md).
"""

import datetime as dt

import job_radar.dedup as dedup
import job_radar.models as models
import job_radar.rejections as rejections


def _opportunity(
    session, company_name="Acme", title="Python Engineer", score=None
) -> models.Opportunity:
    opportunity = models.Opportunity(
        title=title, company=dedup.get_or_create_company(session, company_name), score=score
    )
    session.add(opportunity)
    session.commit()
    return opportunity


def _email(session, gmail_message_id="m1") -> models.EmailMessage:
    email = models.EmailMessage(
        gmail_message_id=gmail_message_id,
        gmail_thread_id="t1",
        direction=models.EmailDirection.IN,
        sent_at=dt.datetime(2026, 1, 1, tzinfo=dt.UTC),
        from_addr="jobs-noreply@linkedin.com",
        to_addr="me@example.com",
    )
    session.add(email)
    session.commit()
    return email


def test_effective_score_penalises_rejected():
    assert rejections.effective_score(10.0, rejected=False) == 10.0
    assert rejections.effective_score(10.0, rejected=True) == 10.0 - rejections.REJECTION_PENALTY
    # Never below zero.
    assert rejections.effective_score(2.0, rejected=True) == 0.0
    # No score stays no score.
    assert rejections.effective_score(None, rejected=True) is None


def test_mark_rejected_flags_the_company(session):
    o = _opportunity(session)
    rejections.mark_rejected(session, company_id=o.company_id, opportunity_id=o.id)
    session.commit()
    assert set(rejections.latest_rejections(session)) == {o.company_id}


def test_mark_rejected_is_idempotent_per_email(session):
    o = _opportunity(session)
    email = _email(session)
    first = rejections.mark_rejected(
        session,
        company_id=o.company_id,
        source=models.RejectionSource.GMAIL,
        email_message_id=email.id,
    )
    second = rejections.mark_rejected(
        session,
        company_id=o.company_id,
        source=models.RejectionSource.GMAIL,
        email_message_id=email.id,
    )
    session.commit()
    assert first.id == second.id
    assert session.query(models.Rejection).count() == 1


def test_latest_rejections_returns_most_recent(session):
    o = _opportunity(session)
    older = _email(session, "m1")
    newer = _email(session, "m2")
    rejections.mark_rejected(
        session,
        company_id=o.company_id,
        source=models.RejectionSource.GMAIL,
        email_message_id=older.id,
        rejected_at=dt.datetime(2026, 1, 1, tzinfo=dt.UTC),
    )
    rejections.mark_rejected(
        session,
        company_id=o.company_id,
        source=models.RejectionSource.GMAIL,
        email_message_id=newer.id,
        rejected_at=dt.datetime(2026, 6, 1, tzinfo=dt.UTC),
    )
    session.commit()
    # SQLite returns tz-naive datetimes; the newer (June) rejection wins.
    assert rejections.latest_rejections(session)[o.company_id].replace(
        tzinfo=dt.UTC
    ) == dt.datetime(2026, 6, 1, tzinfo=dt.UTC)


def test_toggle_manual_rejection_flips(session):
    o = _opportunity(session)
    assert rejections.toggle_manual_rejection(session, o) is True
    session.commit()
    assert o.company_id in rejections.latest_rejections(session)
    assert rejections.toggle_manual_rejection(session, o) is False
    session.commit()
    assert o.company_id not in rejections.latest_rejections(session)


def test_clear_manual_keeps_auto(session):
    o = _opportunity(session)
    email = _email(session)
    # manual
    rejections.mark_rejected(session, company_id=o.company_id)
    rejections.mark_rejected(
        session,
        company_id=o.company_id,
        source=models.RejectionSource.GMAIL,
        email_message_id=email.id,
    )
    session.commit()
    rejections.clear_manual_rejection(session, o.company_id)
    session.commit()
    # The auto-detected one still flags the company.
    assert o.company_id in rejections.latest_rejections(session)
    assert session.query(models.Rejection).count() == 1
