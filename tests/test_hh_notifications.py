from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import select

from job_radar.hh.notifications import import_hh_notifications, parse_notice
from job_radar.models import (
    Application,
    ApplicationSource,
    Company,
    EmailDirection,
    EmailMessage,
    Opportunity,
    OpportunityState,
    Rejection,
    SourceRecord,
    SourceType,
)

FIXTURES = Path(__file__).parent / "fixtures" / "hh"


def fixture(name: str) -> tuple[str, str]:
    return (FIXTURES / f"{name}.subject").read_text(), (FIXTURES / f"{name}.html").read_text()


@pytest.mark.parametrize(
    ("name", "kind", "title", "company", "vacancy_id"),
    [
        (
            "rejection_new",
            "rejection",
            "Backend Python Developer (Middle / Middle+)",
            "ANCOR",
            "137434460",
        ),
        ("rejection_old", "rejection", "Python Developer Lead", "Sapiens solutions", None),
        ("closed", "closed", "Senior Python developer (E-commerce)", "GRI", "128490343"),
        ("message", "message", "Python-разработчик", "LIAN", "130050272"),
    ],
)
def test_parse_notice(name, kind, title, company, vacancy_id):
    subject, html = fixture(name)
    notice = parse_notice(subject, html)
    assert (notice.kind, notice.title, notice.company) == (kind, title, company)
    if vacancy_id:
        assert notice.vacancy_id == vacancy_id
    else:
        assert notice.vacancy_id and notice.vacancy_id.isdigit()


def test_digest_and_unknown_mail_are_not_notices():
    subject, html = fixture("digest")
    assert parse_notice(subject, html) is None
    assert parse_notice("Спасибо за ваше резюме!", "<p>Ваш отклик зарегистрирован</p>") is None


def add_mail(session, name: str, gmail_id: str, at: datetime) -> EmailMessage:
    subject, _ = fixture(name)
    email = EmailMessage(
        gmail_message_id=gmail_id,
        gmail_thread_id=gmail_id,
        direction=EmailDirection.IN,
        sent_at=at,
        from_addr='"hh.ru" <noreply@hh.ru>',
        to_addr="user@example.com",
        subject=subject,
        categories=["job_proposal"],
    )
    session.add(email)
    session.commit()
    return email


BODIES = {f"g-{n}": fixture(n)[1] for n in ("rejection_new", "closed", "message", "digest")}


def fetch(gmail_id: str):
    return BODIES[gmail_id], None


def test_import_rejection_creates_vacancy_application_and_rejection(session):
    at = datetime(2026, 10, 2, 8, 9, tzinfo=UTC)
    email = add_mail(session, "rejection_new", "g-rejection_new", at)
    stats = import_hh_notifications(session, fetch)
    assert (stats.notices, stats.rejections, stats.fetched) == (1, 1, 1)

    record = session.scalars(select(SourceRecord)).one()
    assert record.source_type is SourceType.HH and record.external_id == "137434460"
    assert record.url == "https://hh.ru/vacancy/137434460"
    o = record.opportunity
    assert o.title == "Backend Python Developer (Middle / Middle+)"
    assert o.company.name == "ANCOR" and o.state is OpportunityState.APPLIED

    app = session.scalars(select(Application)).one()
    assert app.source is ApplicationSource.HH and app.opportunity_id == o.id
    assert app.applied_at.replace(tzinfo=UTC) == at and "date unknown" in app.notes
    rejection = session.scalars(select(Rejection)).one()
    assert rejection.email_message_id == email.id and rejection.opportunity_id == o.id

    session.refresh(email)
    assert email.company_id == o.company_id and email.detected_event == "rejection"
    # No body is stored.
    assert email.body_html is None and email.body_text is None


def test_import_is_idempotent_and_reuses_the_vacancy(session):
    add_mail(session, "rejection_new", "g-rejection_new", datetime(2026, 10, 2, tzinfo=UTC))
    import_hh_notifications(session, fetch)
    stats = import_hh_notifications(session, fetch)
    assert stats.notices == 0 and stats.fetched == 0
    assert session.query(Opportunity).count() == 1
    assert session.query(Rejection).count() == 1


def test_message_links_mail_without_rejection(session):
    email = add_mail(session, "message", "g-message", datetime(2026, 1, 30, tzinfo=UTC))
    import_hh_notifications(session, fetch)
    session.refresh(email)
    assert email.detected_event == "recruiter_outreach"
    assert session.get(Company, email.company_id).name == "LIAN"
    assert session.query(Rejection).count() == 0
    assert session.query(Opportunity).count() == 0


def test_digest_is_marked_and_skipped(session):
    email = add_mail(session, "digest", "g-digest", datetime(2026, 10, 2, tzinfo=UTC))
    stats = import_hh_notifications(session, fetch)
    session.refresh(email)
    assert stats.notices == 0 and email.detected_event == "hh_digest"


def test_without_gmail_the_mail_waits(session):
    email = add_mail(session, "rejection_new", "g-rejection_new", datetime(2026, 10, 2, tzinfo=UTC))
    stats = import_hh_notifications(session, None)
    session.refresh(email)
    assert stats.pending == 1 and email.detected_event is None


def test_earlier_rejection_mark_is_completed(session):
    # Marked as a rejection by the generic detector before, without a company.
    email = add_mail(session, "rejection_new", "g-rejection_new", datetime(2026, 10, 2, tzinfo=UTC))
    email.detected_event = "rejection"
    session.commit()
    stats = import_hh_notifications(session, fetch)
    assert stats.rejections == 1
    session.refresh(email)
    assert session.get(Company, email.company_id).name == "ANCOR"


def known_vacancy(session, title="Backend Python Developer (Middle / Middle+)", company="ANCOR"):
    from job_radar.dedup import get_or_create_company, upsert_source_record

    o = Opportunity(title=title, company=get_or_create_company(session, company))
    session.add(o)
    session.flush()
    upsert_source_record(
        session, source_type=SourceType.TELEGRAM, external_id="tg-1", url=None, opportunity=o
    )
    session.commit()
    return o


def test_notice_attaches_to_the_same_vacancy_from_another_source(session):
    known = known_vacancy(session, title="backend python developer  (middle / middle+)")
    add_mail(session, "rejection_new", "g-rejection_new", datetime(2026, 10, 2, tzinfo=UTC))
    import_hh_notifications(session, fetch)
    assert session.scalars(select(Opportunity)).all() == [known]
    assert {r.source_type for r in known.source_records} == {SourceType.TELEGRAM, SourceType.HH}
    assert known.state is OpportunityState.APPLIED
    assert session.scalars(select(Rejection)).one().opportunity_id == known.id


def test_notice_does_not_attach_to_another_hh_vacancy_or_when_ambiguous(session):
    from job_radar.dedup import upsert_source_record

    other_hh = known_vacancy(session)
    other_hh.source_records[0].source_type = SourceType.HH  # a different hh.ru posting
    session.commit()
    add_mail(session, "rejection_new", "g-rejection_new", datetime(2026, 10, 2, tzinfo=UTC))
    import_hh_notifications(session, fetch)
    assert len(session.scalars(select(Opportunity)).all()) == 2

    # Two Telegram candidates: ambiguous, so the importer creates its own vacancy.
    from job_radar.hh.notifications import same_vacancy

    a = known_vacancy(session, title="Go Developer", company="Globex")
    b = Opportunity(title="Go developer", company_id=a.company_id)
    session.add(b)
    session.flush()
    upsert_source_record(
        session, source_type=SourceType.TELEGRAM, external_id="tg-2", url=None, opportunity=b
    )
    assert same_vacancy(session, a.company_id, "GO DEVELOPER") is None


def test_merge_hh_duplicates(session):
    from job_radar.dedup import upsert_source_record
    from job_radar.hh.notifications import merge_hh_duplicates
    from job_radar.models import ClaudeAnalysis, ClaudeMessage, NoteKind, OpportunityNote

    known = known_vacancy(session)
    known.state = OpportunityState.NEW
    dup = Opportunity(title=known.title, company_id=known.company_id)
    dup.state = OpportunityState.APPLIED
    session.add(dup)
    session.flush()
    upsert_source_record(
        session, source_type=SourceType.HH, external_id="137434460", url=None, opportunity=dup
    )
    session.add(
        OpportunityNote(
            opportunity_id=dup.id, company_id=dup.company_id, kind=NoteKind.NOTE, comment="hh"
        )
    )
    analysis = ClaudeAnalysis(opportunity_id=dup.id, model="sonnet", prompt_version="2")
    session.add(analysis)
    session.flush()
    session.add(ClaudeMessage(opportunity_id=dup.id, analysis_id=analysis.id, role="user"))
    session.commit()

    assert merge_hh_duplicates(session, apply=False) == [(known.id, dup.id)]
    assert len(session.scalars(select(Opportunity)).all()) == 2  # dry run
    assert merge_hh_duplicates(session, apply=True) == [(known.id, dup.id)]
    session.expire_all()
    assert session.scalars(select(Opportunity)).all() == [known]
    assert {r.source_type for r in known.source_records} == {SourceType.TELEGRAM, SourceType.HH}
    assert known.state is OpportunityState.APPLIED
    assert session.scalars(select(OpportunityNote)).one().opportunity_id == known.id
    assert session.scalars(select(ClaudeMessage)).one().opportunity_id == known.id
    assert merge_hh_duplicates(session, apply=True) == []
