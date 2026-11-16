"""Tests for job_radar.telegram.digest: digest links, category and vacancy pages and
the import, with a fake Telegraph (docs/telegram.md#digests-digital-nomads).
"""

import copy
import datetime as dt
import json
import pathlib

import sqlalchemy as sa

import job_radar.linkedin.alerts as linkedin_alerts
import job_radar.models as models
import job_radar.telegram.digest as telegram_digest
import job_radar.telegram.sources as telegram_sources
import job_radar.telegram.sync as telegram_sync

FIX = json.loads(
    (pathlib.Path(__file__).parent / "fixtures" / "telegram" / "digest.json").read_text()
)
NOW = dt.datetime(2026, 10, 1, 12, 0, tzinfo=dt.UTC)
NOMADS = telegram_sources.Source(
    name="digital_nomads",
    chat=1096230280,
    parse="graph_org_digest",
    categories=["Back-end, Python", "AI, AI Engineering", "Big Data, Data engineering"],
)
KIWANO = "Python-razrabotchik-Backend-09-30"


def test_digest_links_only_the_chosen_categories():
    links = telegram_digest.digest_links(FIX["digest_post"]["text"], NOMADS.categories)
    paths = [path for _, path in links]
    assert "Back-end-Python-Global-10-01" in paths and "AI-AI-Engineering-Global-10-01" in paths
    assert not any("Go" in p or "PHP" in p or "Sales" in p for p in paths)


def test_parse_category_page():
    items = telegram_digest.parse_category(FIX["category_page"])
    kiwano = next(i for i in items if i.company == "KIWANO")
    assert (kiwano.title, kiwano.group, kiwano.path) == (
        "Python-разработчик (Backend)",
        "HeadHunter",
        KIWANO,
    )
    assert len(items) == 4


def test_parse_vacancy_page():
    page = telegram_digest.parse_vacancy(FIX["vacancy_page"])
    assert page.original_url == "https://hh.kz/vacancy/137922959"
    assert "Почему это 100% удалёнка" in page.description and "Speto" in page.description


class FakeTelegraph:
    """Serves Telegraph pages by path and records the calls."""

    def __init__(self, pages):
        self.pages, self.calls = pages, []

    def get_page(self, path):
        self.calls.append(path)
        return self.pages.get(path)


def pages(vacancy_pages=None):
    out = {"Back-end-Python-Global-10-01": FIX["category_page"]}
    out.update(vacancy_pages or {KIWANO: FIX["vacancy_page"]})
    return out


def digest_message():
    return FIX["digest_post"]


def count(session, model):
    return session.scalar(sa.select(sa.func.count()).select_from(model))


def test_import_creates_vacancies_with_descriptions_and_stores_the_digest(session):
    client = FakeTelegraph(pages())
    stats = telegram_digest.import_digest(session, NOMADS, [digest_message()], client, now=NOW)
    o = session.scalar(sa.select(models.Opportunity))
    assert (o.title, o.company.name, o.location) == (
        "Python-разработчик (Backend)",
        "KIWANO",
        "Remote",
    )
    record = session.scalar(
        sa.select(models.SourceRecord).where(
            models.SourceRecord.source_type == models.SourceType.TELEGRAM
        )
    )
    assert (
        record.external_id == f"telegraph:{KIWANO}" and record.url == f"https://telegra.ph/{KIWANO}"
    )
    assert "Speto" in session.scalar(sa.select(models.JobSnapshot.raw_text))
    assert stats.vacancies == 1
    assert session.scalar(sa.select(models.TelegramPost.status)) == "digest"


def test_second_run_reads_nothing_again(session):
    telegram_digest.import_digest(
        session, NOMADS, [digest_message()], FakeTelegraph(pages()), now=NOW
    )
    again = FakeTelegraph(pages())
    assert (
        telegram_digest.import_digest(session, NOMADS, [digest_message()], again, now=NOW).vacancies
        == 0
    )
    # the digest post is already stored
    assert again.calls == []


def linkedin_page(job_id):
    page = copy.deepcopy(FIX["vacancy_page"])
    for node in page["content"]:
        if isinstance(node, dict) and node.get("tag") == "a":
            node["attrs"]["href"] = f"https://www.linkedin.com/jobs/view/{job_id}/"
    return page


def test_known_linkedin_job_gets_the_description_instead_of_a_duplicate(session):
    linkedin_alerts.add_linkedin_job(
        session,
        linkedin_alerts.AlertJob("555", "Python Developer", "KIWANO", "Remote", None, False),
        models.SourceType.LINKEDIN_ALERT,
    )
    session.commit()
    stats = telegram_digest.import_digest(
        session,
        NOMADS,
        [digest_message()],
        FakeTelegraph(pages({KIWANO: linkedin_page("555")})),
        now=NOW,
    )
    assert count(session, models.Opportunity) == 1
    assert (stats.vacancies, stats.linked) == (0, 1)
    assert "Speto" in session.scalar(sa.select(models.JobSnapshot.raw_text))


def test_new_linkedin_linked_vacancy_blocks_a_later_alert_duplicate(session):
    telegram_digest.import_digest(
        session,
        NOMADS,
        [digest_message()],
        FakeTelegraph(pages({KIWANO: linkedin_page("777")})),
        now=NOW,
    )
    later = linkedin_alerts.add_linkedin_job(
        session,
        linkedin_alerts.AlertJob("777", "Python Developer", "KIWANO", None, None, False),
        models.SourceType.LINKEDIN_ALERT,
    )
    assert later is False and count(session, models.Opportunity) == 1


def test_digest_sources_are_planned(session):
    plans = telegram_sync.plan_reads(session, [NOMADS], now=NOW)
    assert len(plans) == 1 and plans[0].source.name == "digital_nomads"
