import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import func, select

from job_radar.models import JobSnapshot, Opportunity, SourceRecord, SourceType, TelegramPost
from job_radar.telegram.posts import parse_post
from job_radar.telegram.sources import Source, load_sources
from job_radar.telegram.sync import import_posts, plan_reads

POSTS = json.loads((Path(__file__).parent / "fixtures" / "telegram" / "posts.json").read_text())
NOW = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)


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
    job = parse_post(post(group, message_id)["text"])
    assert (job.title, job.company, job.location) == (title, company, location)


def test_parse_collects_links():
    job = parse_post(post("zarubezhom_jobs", 4276)["text"])
    assert "https://flexdatabases.com/" in job.links


def test_load_sources_from_example():
    sources = load_sources(Path(__file__).parent.parent / "telegram.example.yaml")
    by_name = {s.name: s for s in sources}
    assert by_name["cyprusithr"].topics == [46685, 46679]
    assert by_name["cyprusithr"].require_tags == ["#vacancy", "#вакансия"]
    assert (
        by_name["digital_nomads"].chat == 1096230280
        and by_name["digital_nomads"].parse == "graph_org_digest"
    )
    assert by_name["youritjob"].keywords.search("Senior Python Developer")
    assert not by_name["youritjob"].keywords.search("Генеральная уборка")


YOURITJOB = Source(name="youritjob", chat="youritjob")
CYPRUS = Source(
    name="cyprusithr", chat="cyprusithr", topics=[46685], require_tags=["#vacancy", "#вакансия"]
)


def count(session, model):
    return session.scalar(select(func.count()).select_from(model))


def test_import_keeps_only_python_vacancies_and_stores_every_post(session):
    stats = import_posts(session, YOURITJOB, None, POSTS["youritjob_all"], now=NOW)
    assert (stats.posts, stats.vacancies) == (8, 2)
    assert count(session, TelegramPost) == 8
    titles = {o.title for o in session.scalars(select(Opportunity))}
    assert titles == {"Junior Python Developer (Fintech)", "Junior / Junior+ разработчик (Python)"}
    statuses = dict(session.execute(select(TelegramPost.message_id, TelegramPost.status)).all())
    assert statuses[8493] == "skipped"  # the cleaning ad
    assert (
        import_posts(session, YOURITJOB, None, POSTS["youritjob_all"], now=NOW).posts == 0
    )  # no duplicates


def test_require_tags_and_keywords(session):
    stats = import_posts(session, CYPRUS, 46685, POSTS["cyprusithr_all"], now=NOW)
    titles = {o.title for o in session.scalars(select(Opportunity))}
    assert titles == {"Senior Data Engineer"}  # the Full-Stack/Mobile post has no Python
    assert stats.vacancies == 1


def test_vacancy_has_link_description_and_placeholder_company(session):
    import_posts(session, CYPRUS, 46685, [post("cyprusithr_all", 113584)], now=NOW)
    record = session.scalar(select(SourceRecord))
    assert record.source_type is SourceType.TELEGRAM
    assert record.external_id == "cyprusithr:113584"
    assert record.url == "https://t.me/cyprusithr/113584"
    o = record.opportunity
    assert o.company.name == "Unknown (cyprusithr 113584)"
    assert "Senior Data Engineer" in session.scalar(select(JobSnapshot.raw_text))


def test_private_channel_link():
    from job_radar.telegram.sync import post_url

    assert post_url(1096230280, 2251) == "https://t.me/c/1096230280/2251"
    assert post_url(-1001096230280, 2251) == "https://t.me/c/1096230280/2251"
    assert post_url("@youritjob", 5) == "https://t.me/youritjob/5"


def test_plan_reads_resumes_after_the_last_stored_post(session):
    import_posts(session, YOURITJOB, None, POSTS["youritjob_all"], now=NOW)
    plans = plan_reads(session, [YOURITJOB, CYPRUS], now=NOW)
    youritjob = next(p for p in plans if p.source.name == "youritjob")
    cyprus = next(p for p in plans if p.source.name == "cyprusithr")
    assert youritjob.after_id == max(m["id"] for m in POSTS["youritjob_all"])
    assert (cyprus.topic_id, cyprus.after_id) == (46685, None)
    assert cyprus.since == NOW - timedelta(days=14)


def fake(message_id, text):
    return {"id": message_id, "date": NOW.isoformat(), "text": text}


@pytest.mark.parametrize(
    "text",
    [
        "**Marketing Analyst**\nLimassol\nSQL, Python, Tableau",  # not a developer role
        "**Senior QA Automation Engineer**\nPython, pytest",  # QA excluded
        "**Дайджест вакансий за две недели**\nPython Developer, Go Developer",  # a digest
        "**Business Development Manager**\nPython is a plus",
    ],
)
def test_title_filters_skip_other_roles(session, text):
    assert import_posts(session, YOURITJOB, None, [fake(1, text)], now=NOW).vacancies == 0


def test_developer_role_with_python_passes(session):
    text = "**Senior Backend Engineer**\n📍 Remote\nPython, FastAPI, PostgreSQL"
    assert import_posts(session, YOURITJOB, None, [fake(2, text)], now=NOW).vacancies == 1


def test_narrative_sentence_is_not_split_into_title_and_company():
    job = parse_post("**Компания iGaming в поиске Data Analyst (junior+) с опытом в Python**")
    assert job.company is None
