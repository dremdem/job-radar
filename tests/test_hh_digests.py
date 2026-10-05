from datetime import UTC, date, datetime
from pathlib import Path

from sqlalchemy import select

from job_radar.hh.digests import import_hh_digests, parse_digest
from job_radar.models import EmailDirection, EmailMessage, Opportunity, SourceRecord, SourceType

DIGEST = (Path(__file__).parent / "fixtures" / "hh" / "digest.html").read_text()


def test_parse_digest():
    items = parse_digest(DIGEST)
    assert len(items) == 8
    first = items[0]
    assert first.title == "Computer science IGCSE / International A-Level / SAT / IB"
    assert first.vacancy_id == "138023113"
    assert first.company == "EdEra School" and first.city == "Москва"
    assert first.salary == "от 3 000 до 5 000 ₽ за час"
    ml = next(i for i in items if i.title == "ML Engineer (Senior)")
    assert ml.company == "Agile Soft" and ml.city == "Алматы" and ml.salary is None
    # Metro stations after the city are not part of it.
    assert all(not (i.city or "").startswith("м.") for i in items)


def add_digest(session, gmail_id: str, at: datetime, event="hh_digest") -> EmailMessage:
    email = EmailMessage(
        gmail_message_id=gmail_id,
        gmail_thread_id=gmail_id,
        direction=EmailDirection.IN,
        sent_at=at,
        from_addr='"hh.ru" <noreply@hh.ru>',
        to_addr="user@example.com",
        subject="Вакансии по подписке: Python",
        categories=["job_proposal"],
        detected_event=event,
    )
    session.add(email)
    session.commit()
    return email


def fetch(gmail_id):
    return DIGEST, None


SINCE = date(2026, 10, 5)


def test_import_only_developer_roles_from_new_digests(session):
    old = add_digest(session, "old", datetime(2026, 10, 3, tzinfo=UTC))
    new = add_digest(session, "new", datetime(2026, 10, 5, 9, tzinfo=UTC))
    stats = import_hh_digests(session, fetch, since=SINCE)
    assert (stats.digests, stats.fetched) == (1, 1)
    titles = sorted(session.scalars(select(Opportunity.title)))
    assert "ML Engineer (Senior)" in titles and "Backend-разработчик (Python/JS)" in titles
    assert not any("IGCSE" in t or "Mathematician" in t or "Автор" in t for t in titles)
    assert stats.vacancies == len(titles) and stats.skipped == 8 - len(titles)

    record = session.scalars(
        select(SourceRecord).where(SourceRecord.external_id == "138030671")
    ).one()
    assert record.source_type is SourceType.HH and record.url == "https://hh.ru/vacancy/138030671"
    assert record.opportunity.location == "Remote (Алматы)"
    assert record.opportunity.company.name == "Agile Soft"
    session.refresh(old)
    session.refresh(new)
    assert old.detected_event == "hh_digest" and new.detected_event == "hh_digest_done"


def test_import_is_idempotent_and_shares_ids_with_notices(session):
    add_digest(session, "a", datetime(2026, 10, 5, tzinfo=UTC))
    add_digest(session, "b", datetime(2026, 10, 6, tzinfo=UTC))  # the same vacancies again
    import_hh_digests(session, fetch, since=SINCE)
    count = session.query(Opportunity).count()
    assert session.query(SourceRecord).count() == count  # one record per vacancy
    assert import_hh_digests(session, fetch, since=SINCE).digests == 0


def test_without_a_date_or_gmail_nothing_is_imported(session):
    add_digest(session, "a", datetime(2026, 10, 5, tzinfo=UTC))
    assert import_hh_digests(session, fetch, since=None).digests == 0
    stats = import_hh_digests(session, None, since=SINCE)
    assert stats.pending == 1 and session.query(Opportunity).count() == 0


def test_pipeline_reports_digests(session, tmp_path):
    from job_radar import pipeline
    from job_radar.config import Settings

    lines: list[str] = []
    pipeline.import_hh(session, Settings(), lines.append, use_gmail=False)
    assert lines[-1].startswith("hh digests: off")
    lines.clear()
    settings = Settings(hh_digests_since=SINCE)
    pipeline.import_hh(session, settings, lines.append, use_gmail=False)
    assert lines[-1] == (
        "hh digests: 0 digests, 0 new vacancies, 0 already known, 0 skipped as noise"
    )


def test_last_vacancy_does_not_take_the_footer():
    last = parse_digest(DIGEST)[-1]
    assert "HeadHunter" not in last.company and "2000" not in (last.city or "")


def test_noise_titles_are_skipped():
    from job_radar.hh.digests import wanted

    assert wanted("Ведущий Python разработчик")
    assert wanted("Senior ML-инженер")
    noise = [
        "Инженер по информационной безопасности AI",
        "Tech Lead/Senior CV Engineer (офис)",
        "Преподаватель программирования",
        "Security Engineer",
    ]
    for title in noise:
        assert not wanted(title), title
