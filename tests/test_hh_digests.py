"""Tests for job_radar.sources.hh.digests: parsing hh.ru subscription digests and importing
their developer vacancies once (docs/hh.md#digests).
"""

import datetime as dt
import pathlib

import sqlalchemy as sa

import job_radar.models as models
import job_radar.sources.hh.digests as hh_digests

DIGEST = (pathlib.Path(__file__).parent / "fixtures" / "hh" / "digest.html").read_text()


def test_parse_digest():
    items = hh_digests.parse_digest(DIGEST)
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


def add_digest(session, gmail_id: str, at: dt.datetime, event="hh_digest") -> models.EmailMessage:
    email = models.EmailMessage(
        gmail_message_id=gmail_id,
        gmail_thread_id=gmail_id,
        direction=models.EmailDirection.IN,
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


SINCE = dt.date(2026, 10, 5)


def test_import_only_developer_roles_from_new_digests(session):
    old = add_digest(session, "old", dt.datetime(2026, 10, 3, tzinfo=dt.UTC))
    new = add_digest(session, "new", dt.datetime(2026, 10, 5, 9, tzinfo=dt.UTC))
    stats = hh_digests.import_hh_digests(session, fetch, since=SINCE)
    assert (stats.digests, stats.fetched) == (1, 1)
    titles = sorted(session.scalars(sa.select(models.Opportunity.title)))
    assert "ML Engineer (Senior)" in titles and "Backend-разработчик (Python/JS)" in titles
    assert not any("IGCSE" in t or "Mathematician" in t or "Автор" in t for t in titles)
    assert stats.vacancies == len(titles) and stats.skipped == 8 - len(titles)

    record = session.scalars(
        sa.select(models.SourceRecord).where(models.SourceRecord.external_id == "138030671")
    ).one()
    assert (
        record.source_type is models.SourceType.HH
        and record.url == "https://hh.ru/vacancy/138030671"
    )
    assert record.opportunity.location == "Remote (Алматы)"
    assert record.opportunity.company.name == "Agile Soft"
    session.refresh(old)
    session.refresh(new)
    assert old.detected_event == "hh_digest" and new.detected_event == "hh_digest_done"


def test_import_is_idempotent_and_shares_ids_with_notices(session):
    add_digest(session, "a", dt.datetime(2026, 10, 5, tzinfo=dt.UTC))
    # the same vacancies again
    add_digest(session, "b", dt.datetime(2026, 10, 6, tzinfo=dt.UTC))
    hh_digests.import_hh_digests(session, fetch, since=SINCE)
    count = session.query(models.Opportunity).count()
    # one record per vacancy
    assert session.query(models.SourceRecord).count() == count
    assert hh_digests.import_hh_digests(session, fetch, since=SINCE).digests == 0


def test_without_a_date_or_gmail_nothing_is_imported(session):
    add_digest(session, "a", dt.datetime(2026, 10, 5, tzinfo=dt.UTC))
    assert hh_digests.import_hh_digests(session, fetch, since=None).digests == 0
    stats = hh_digests.import_hh_digests(session, None, since=SINCE)
    assert stats.pending == 1 and session.query(models.Opportunity).count() == 0


def test_pipeline_reports_digests(session, tmp_path):
    import job_radar.config as config
    import job_radar.pipeline as pipeline

    lines: list[str] = []
    pipeline.import_hh(session, config.Settings(), lines.append, use_gmail=False)
    assert lines[-1].startswith("hh digests: off")
    lines.clear()
    settings = config.Settings(hh_digests_since=SINCE)
    pipeline.import_hh(session, settings, lines.append, use_gmail=False)
    assert lines[-1] == (
        "hh digests: 0 digests, 0 new vacancies, 0 already known, 0 skipped as noise"
    )


def test_last_vacancy_does_not_take_the_footer():
    last = hh_digests.parse_digest(DIGEST)[-1]
    assert "HeadHunter" not in last.company and "2000" not in (last.city or "")


def test_noise_titles_are_skipped():

    assert hh_digests.wanted("Ведущий Python разработчик")
    assert hh_digests.wanted("Senior ML-инженер")
    noise = [
        "Инженер по информационной безопасности AI",
        "Tech Lead/Senior CV Engineer (офис)",
        "Преподаватель программирования",
        "Security Engineer",
    ]
    for title in noise:
        assert not hh_digests.wanted(title), title


def test_digest_deleted_from_gmail_is_marked_and_the_rest_goes_on(session):
    import job_radar.mail as mail

    calls = []

    def fetch_some(gmail_id):
        calls.append(gmail_id)
        if gmail_id == "d-gone":
            raise mail.MessageGone(gmail_id)
        return fetch(gmail_id)

    gone = add_digest(session, "d-gone", dt.datetime(2026, 10, 6, tzinfo=dt.UTC))
    add_digest(session, "d-ok", dt.datetime(2026, 10, 7, tzinfo=dt.UTC))
    stats = hh_digests.import_hh_digests(session, fetch_some, since=SINCE)
    assert (stats.gone, stats.digests) == (1, 1)
    session.refresh(gone)
    assert gone.detected_event == "gone"

    hh_digests.import_hh_digests(session, fetch_some, since=SINCE)
    assert calls == ["d-gone", "d-ok"]
