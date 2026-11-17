"""Tests for Telegram posts: parsing real posts, the channel allowlist and turning
relevant posts into vacancies (docs/telegram.md).
"""

import datetime as dt
import json

import paths
import pytest
import sqlalchemy as sa

import job_radar.models as models
import job_radar.sources.telegram.posts as telegram_posts
import job_radar.sources.telegram.sources as telegram_sources
import job_radar.sources.telegram.sync as telegram_sync

POSTS = json.loads((paths.FIXTURES / "telegram" / "posts.json").read_text())
NOW = dt.datetime(2026, 10, 1, 12, 0, tzinfo=dt.UTC)


def post(group, message_id):
    return next(m for m in POSTS[group] if m["id"] == message_id)


@pytest.mark.parametrize(
    ("group", "message_id", "title", "company", "location"),
    [
        (
            "zarubezhom_jobs",
            4276,
            "EDC Data Solution Specialist",
            "Flex Databases",
            "Remote (вне РФ и РБ)",
        ),
        ("youritjob_all", 8488, "Junior Python Developer (Fintech)", "forcode", "удаленно"),
        ("youritjob_all", 8489, "Junior / Junior+ разработчик (Python)", "JoSpace", "удаленно"),
        ("cyprusithr_all", 113584, "Senior Data Engineer", None, None),
        (
            "cyprusithr_all",
            113550,
            "Senior Full-Stack & Mobile Developer (AI-Driven)",
            None,
            "100% Remote (Cyprus) | Full-Time | Fintech / Trading",
        ),
        ("twinslashjobs", 164, "Business/Data Analyst", None, "Baltics / EU | 🌍 Remote / Hybrid"),
    ],
)
def test_parse_real_posts(group, message_id, title, company, location):
    job = telegram_posts.parse_post(post(group, message_id)["text"])
    assert (job.title, job.company, job.location) == (title, company, location)


def test_parse_collects_links():
    job = telegram_posts.parse_post(post("zarubezhom_jobs", 4276)["text"])
    assert "https://flexdatabases.com/" in job.links


def test_load_sources_from_example():
    sources = telegram_sources.load_sources(paths.ROOT / "telegram.example.yaml")
    by_name = {s.name: s for s in sources}
    assert by_name["cyprusithr"].topics == [46685, 46679]
    assert by_name["cyprusithr"].require_tags == ["#vacancy", "#вакансия"]
    assert (
        by_name["digital_nomads"].chat == 1096230280
        and by_name["digital_nomads"].parse == "graph_org_digest"
    )
    assert by_name["youritjob"].keywords.search("Senior Python Developer")
    assert not by_name["youritjob"].keywords.search("Генеральная уборка")


YOURITJOB = telegram_sources.Source(name="youritjob", chat="youritjob")
CYPRUS = telegram_sources.Source(
    name="cyprusithr", chat="cyprusithr", topics=[46685], require_tags=["#vacancy", "#вакансия"]
)


def count(session, model):
    return session.scalar(sa.select(sa.func.count()).select_from(model))


def test_import_keeps_only_python_vacancies_and_stores_every_post(session):
    stats = telegram_sync.import_posts(session, YOURITJOB, None, POSTS["youritjob_all"], now=NOW)
    assert (stats.posts, stats.vacancies) == (8, 2)
    assert count(session, models.TelegramPost) == 8
    titles = {o.title for o in session.scalars(sa.select(models.Opportunity))}
    assert titles == {"Junior Python Developer (Fintech)", "Junior / Junior+ разработчик (Python)"}
    statuses = dict(
        session.execute(sa.select(models.TelegramPost.message_id, models.TelegramPost.status)).all()
    )
    # the cleaning ad
    assert statuses[8493] == "skipped"
    assert (
        telegram_sync.import_posts(session, YOURITJOB, None, POSTS["youritjob_all"], now=NOW).posts
        == 0
        # no duplicates
    )


def test_require_tags_and_keywords(session):
    stats = telegram_sync.import_posts(session, CYPRUS, 46685, POSTS["cyprusithr_all"], now=NOW)
    titles = {o.title for o in session.scalars(sa.select(models.Opportunity))}
    # the Full-Stack/Mobile post has no Python
    assert titles == {"Senior Data Engineer"}
    assert stats.vacancies == 1


def test_vacancy_has_link_description_and_placeholder_company(session):
    telegram_sync.import_posts(session, CYPRUS, 46685, [post("cyprusithr_all", 113584)], now=NOW)
    record = session.scalar(sa.select(models.SourceRecord))
    assert record.source_type is models.SourceType.TELEGRAM
    assert record.external_id == "cyprusithr:113584"
    assert record.url == "https://t.me/cyprusithr/113584"
    o = record.opportunity
    assert o.company.name == "Unknown (cyprusithr 113584)"
    assert "Senior Data Engineer" in session.scalar(sa.select(models.JobSnapshot.raw_text))


def test_private_channel_link():

    assert telegram_sync.post_url(1096230280, 2251) == "https://t.me/c/1096230280/2251"
    assert telegram_sync.post_url(-1001096230280, 2251) == "https://t.me/c/1096230280/2251"
    assert telegram_sync.post_url("@youritjob", 5) == "https://t.me/youritjob/5"


def test_plan_reads_resumes_after_the_last_stored_post(session):
    telegram_sync.import_posts(session, YOURITJOB, None, POSTS["youritjob_all"], now=NOW)
    plans = telegram_sync.plan_reads(session, [YOURITJOB, CYPRUS], now=NOW)
    youritjob = next(p for p in plans if p.source.name == "youritjob")
    cyprus = next(p for p in plans if p.source.name == "cyprusithr")
    assert youritjob.after_id == max(m["id"] for m in POSTS["youritjob_all"])
    assert (cyprus.topic_id, cyprus.after_id) == (46685, None)
    assert cyprus.since == NOW - dt.timedelta(days=14)


def fake(message_id, text):
    return {"id": message_id, "date": NOW.isoformat(), "text": text}


@pytest.mark.parametrize(
    "text",
    [
        # not a developer role
        "**Marketing Analyst**\nLimassol\nSQL, Python, Tableau",
        # QA excluded
        "**Senior QA Automation Engineer**\nPython, pytest",
        # a digest
        "**Дайджест вакансий за две недели**\nPython Developer, Go Developer",
        "**Business Development Manager**\nPython is a plus",
    ],
)
def test_title_filters_skip_other_roles(session, text):
    assert (
        telegram_sync.import_posts(session, YOURITJOB, None, [fake(1, text)], now=NOW).vacancies
        == 0
    )


def test_developer_role_with_python_passes(session):
    text = "**Senior Backend Engineer**\n📍 Remote\nPython, FastAPI, PostgreSQL"
    assert (
        telegram_sync.import_posts(session, YOURITJOB, None, [fake(2, text)], now=NOW).vacancies
        == 1
    )


def test_narrative_sentence_is_not_split_into_title_and_company():
    job = telegram_posts.parse_post(
        "**Компания iGaming в поиске Data Analyst (junior+) с опытом в Python**"
    )
    assert job.company is None
