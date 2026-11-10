"""Forward Deployed Engineer roles from FDE Times (#13, docs/fdetimes.md).

Fixtures, fetched on 2026-11-10 and trimmed:
- jobs.html: https://fdetimes.net/en/jobs/ with 15 of its 770 FDE roles (12 from
  2026-11-06..09, one from 2026-11-05, 2026-10-21 and 2023-12-22) and 8 of its related AI
  roles (all 2026-11-09).
- job.html: the page of the remote Tonic.ai role: header, description, JobPosting data.
"""

import threading
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import select

from job_radar import pipeline
from job_radar.config import Settings
from job_radar.dedup import get_or_create_company
from job_radar.fdetimes import (
    FDETimesClient,
    FDETimesError,
    PageClient,
    import_descriptions,
    import_fdetimes,
    parse_job_page,
    parse_jobs,
    parse_regions,
    wants_title,
    without_description,
)
from job_radar.models import (
    JobSnapshot,
    Opportunity,
    OpportunityState,
    SourceRecord,
    SourceType,
    SyncCursor,
)

FIXTURES = Path(__file__).parent / "fixtures" / "fdetimes"
LIST = (FIXTURES / "jobs.html").read_text()
JOB = (FIXTURES / "job.html").read_text()
NOW = datetime(2026, 11, 10, 4, 0, tzinfo=UTC)
TONIC = "tonicai-e632d472-fbd2-475a-9878-536c0eacb5e9"
CELONIS = "celonis-8198943003"


class FakeList:
    def __init__(self, result: str | Exception = LIST):
        self.result, self.calls = result, 0

    def jobs_page(self) -> str:
        self.calls += 1
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def by_slug(slug: str):
    return next(j for j in parse_jobs(LIST) if j.slug == slug)


def titles(session) -> set[str]:
    return set(session.scalars(select(Opportunity.title)))


# --- the list ---------------------------------------------------------------


def test_list_page_rows():
    jobs = parse_jobs(LIST)
    assert len(jobs) == 23
    assert [j.kind for j in jobs].count("fde") == 15
    celonis = by_slug(CELONIS)
    assert (celonis.title, celonis.company, celonis.location, celonis.region) == (
        "Forward Deployed Engineer - CPG/Retail",
        "Celonis",
        "Munich, Germany",
        "europe",
    )
    assert celonis.posted_at == datetime(2026, 11, 9, 16, 43, 48, tzinfo=UTC)
    assert celonis.url == "https://fdetimes.net/en/jobs/celonis-8198943003/"
    tonic = by_slug(TONIC)
    assert (tonic.company, tonic.location, tonic.region, tonic.kind) == (
        "Tonic.ai",
        "Remote",
        "remote",
        "fde",
    )
    # Entities are decoded; a location with a comma stays whole.
    thomson = by_slug("thomsonreuters-jreq205196")
    assert thomson.title == "Principal Software Engineer / Architect \u2013 AI \u2013 Legal FDE"
    assert thomson.location == "United States of America, Frisco, Texas"
    assert by_slug("mongodb-8360715").kind == "related"


def test_title_filter():
    assert wants_title("Forward Deployed Engineer - India")
    assert wants_title("Tencent Cloud Smart Mobility - FDE Delivery Architect")
    assert not wants_title("Manager, Forward Deployed Engineering - Munich")
    assert not wants_title("Deployment Strategist, Public Sector (Federal & SLED) - North America")
    assert not wants_title("Sr. Sales Engineer (Forward Deployed Engineer - Pre Sales)")


def test_regions_setting():
    assert parse_regions("") == ("north-america", "europe", "apac", "remote", "other")
    assert parse_regions(" Remote, apac ") == ("remote", "apac")
    with pytest.raises(ValueError, match="unknown FDE Times region 'asia'"):
        parse_regions("remote,asia")


def test_first_run_reads_seven_days_back(session):
    stats = import_fdetimes(session, FakeList(), now=NOW)
    # Since 2026-11-03 04:00: 13 FDE roles and 8 related ones. Related roles are off by
    # default; of the FDE ones two managers and a strategist are not developer roles.
    assert (stats.listed, stats.fresh, stats.new, stats.known, stats.skipped) == (23, 21, 10, 0, 11)
    assert stats.error is None
    assert "Senior Forward Deployed Engineer - Canada" in titles(session)  # 2026-11-05
    assert "Principal Forward Deployed Engineer \u2013 AI & Developer Platform" not in titles(
        session
    )
    record = session.scalar(select(SourceRecord).where(SourceRecord.external_id == CELONIS))
    assert record.source_type is SourceType.FDETIMES
    assert record.url == "https://fdetimes.net/en/jobs/celonis-8198943003/"
    o = record.opportunity
    assert (o.company.name, o.location, o.state) == (
        "Celonis",
        "Munich, Germany",
        OpportunityState.NEW,
    )
    assert o.score is not None
    assert not session.scalars(select(JobSnapshot)).all()  # descriptions come later
    assert session.get(SyncCursor, "fdetimes").last_run_at is not None


def test_a_role_is_never_imported_twice(session):
    import_fdetimes(session, FakeList(), now=NOW)
    again = import_fdetimes(session, FakeList(), now=NOW)
    # The second run reads from a week before the first: every role of the first again.
    assert (again.new, again.known, again.skipped) == (0, 10, 11)
    assert len(session.scalars(select(SourceRecord)).all()) == 10


def test_regions_and_related_roles(session):
    stats = import_fdetimes(session, FakeList(), now=NOW, regions=("remote",), related=True)
    # Remote: two FDE roles, and of the related ones only a manager (not a developer role).
    assert stats.new == 2
    assert titles(session) == {
        "Forward Deployed Engineer, Data De-Identification (Co-op)",
        "Senior Forward Deployed Engineer - Canada",
    }


def test_related_roles_when_asked(session):
    stats = import_fdetimes(session, FakeList(), now=NOW, related=True)
    assert stats.new == 10 + 6  # not the two AI Deployment Managers
    assert "Applied AI Engineer, Large Enterprise" in titles(session)


def test_a_failed_request_keeps_the_last_run(session):
    stats = import_fdetimes(session, FakeList(FDETimesError("HTTP 503")), now=NOW)
    assert stats.error == "HTTP 503" and stats.new == 0
    assert session.get(SyncCursor, "fdetimes") is None


def test_client_reports_the_http_status():
    with pytest.raises(FDETimesError, match="HTTP 503"):
        FDETimesClient(lambda url: (503, "")).jobs_page()
    asked = []
    assert FDETimesClient(lambda url: asked.append(url) or (200, LIST)).jobs_page() == LIST
    assert asked == ["https://fdetimes.net/en/jobs/"]


def test_auto_ignore_rules_run_on_import(session):
    import_fdetimes(session, FakeList(), now=NOW)
    india = session.scalar(
        select(Opportunity).where(Opportunity.title == "Forward Deployed Engineer - India")
    )
    assert india.state is OpportunityState.AUTO_IGNORE  # India is ignored by default (#229)


def test_a_copy_of_an_applied_vacancy_joins_it(session):
    applied = Opportunity(
        title="Forward Deployed Engineer - CPG/Retail",
        company=get_or_create_company(session, "Celonis"),
        state=OpportunityState.APPLIED,
    )
    session.add(applied)
    session.commit()
    import_fdetimes(session, FakeList(), now=NOW)
    same = session.scalars(
        select(Opportunity).where(Opportunity.title == "Forward Deployed Engineer - CPG/Retail")
    ).all()
    assert [o.id for o in same] == [applied.id]
    assert [r.external_id for r in applied.source_records] == [CELONIS]


# --- descriptions -----------------------------------------------------------


def test_description_from_the_job_posting_data():
    text = parse_job_page(JOB)
    assert text.startswith("Remote · Co-op, about 20 hours a week")
    assert "Python and other backend programming languages, plus SQL." in text
    assert "<p>" not in text


def test_description_from_the_page_without_the_data():
    page = JOB[: JOB.index('<script type="application/ld+json">')]
    text = parse_job_page(page)
    assert text.startswith("Remote · Co-op") and "Job description" not in text
    assert parse_job_page("<html><body>Not found</body></html>") is None


class FakePages:
    def __init__(self, answers: dict[str, tuple[int, str]]):
        self.answers, self.asked = answers, []

    def __call__(self, url: str) -> tuple[int, str]:
        slug = url.removeprefix("https://fdetimes.net/en/jobs/").rstrip("/")
        self.asked.append(slug)
        return self.answers.get(slug, (200, JOB))


def client(http_get, **extra) -> PageClient:
    waits = extra.pop("waits", [])
    return PageClient(http_get, pause=(10, 40), rand=lambda a, b: 12.0, sleep=waits.append, **extra)


def pending_slugs(session) -> list[str]:
    return [r.external_id for _, r in without_description(session)]


def test_descriptions_for_to_review_vacancies_with_a_pause(session):
    import_fdetimes(session, FakeList(), now=NOW)
    todo = pending_slugs(session)
    assert len(todo) == 9  # the India role is auto-ignored
    waits = []
    pages = FakePages({})
    stats = import_descriptions(session, client(pages, waits=waits), max_per_run=3)
    assert (stats.total, stats.requests, stats.saved, stats.errors) == (3, 3, 3, 0)
    assert pages.asked == todo[:3]  # best score first
    assert waits == [12.0, 12.0]  # between requests, not before the first
    assert len(pending_slugs(session)) == 6
    record = session.scalar(select(SourceRecord).where(SourceRecord.external_id == todo[0]))
    assert "Tonic.ai has been de-identifying" in record.snapshots[0].raw_text


def test_gone_error_and_blocked(session):
    import_fdetimes(session, FakeList(), now=NOW)
    first, second, third, fourth, *rest = pending_slugs(session)
    pages = FakePages({first: (404, ""), second: (500, ""), third: (200, "<html></html>"),
                       fourth: (429, "")})  # fmt: skip
    stats = import_descriptions(session, client(pages))
    assert pages.asked == [first, second, third, fourth]  # 429: no more requests this run
    assert (stats.requests, stats.saved, stats.gone, stats.errors) == (4, 0, 1, 2)
    assert (stats.blocked, stats.reason) == (True, "HTTP 429")
    assert stats.problems == [f"{second}: HTTP 500", f"{third}: no description"]
    gone = session.scalar(select(SourceRecord).where(SourceRecord.external_id == first))
    assert gone.opportunity.unavailable_at is not None
    assert gone.opportunity.state is OpportunityState.AUTO_IGNORE
    # The gone one is not asked again; the others are.
    assert pending_slugs(session) == [second, third, fourth, *rest]


def test_stop_before_the_next_request(session):
    import_fdetimes(session, FakeList(), now=NOW)
    stop = threading.Event()
    pages = FakePages({})
    page_client = client(pages, waits=[], stop=stop, on_wait=lambda s: stop.set())
    stats = import_descriptions(session, page_client)
    # Stop pressed during the pause after the first page: no second request.
    assert stats.stopped and stats.requests == 1 and len(pages.asked) == 1


# --- pipeline ---------------------------------------------------------------


def settings(tmp_path, database_url, **extra) -> Settings:
    base = {"database_url": database_url, "linkedin_public_search": False}
    return Settings(**(base | extra))


def test_descriptions_job_reads_fdetimes_without_linkedin(tmp_path, session, database_url):
    import_fdetimes(session, FakeList(), now=NOW)
    first = pending_slugs(session)[0]
    from job_radar.linkedin.public import PauseSettings, save_pause

    save_pause(session, PauseSettings(0, 0))
    lines, progress = [], []
    stats = pipeline.fetch_descriptions(
        session,
        settings(tmp_path, database_url, fdetimes_max_descriptions=2),
        echo=lines.append,
        on_progress=lambda s: progress.append((s.requests, s.saved)),
        page_get=FakePages({first: (404, "")}),
    )
    assert (stats.total, stats.requests, stats.saved, stats.blocked) == (2, 2, 1, False)
    assert progress[-1] == (2, 1)
    assert lines == ["descriptions: FDE Times: 2 requests, 1 saved, 1 gone"]


def test_descriptions_are_off_only_without_both_sources(tmp_path, database_url):
    assert pipeline.descriptions_on(settings(tmp_path, database_url))
    assert not pipeline.descriptions_on(settings(tmp_path, database_url, fdetimes=False))


def test_sync_step(tmp_path, session, database_url):
    lines = []
    pipeline.import_fdetimes(session, settings(tmp_path, database_url), lines.append, FakeList())
    assert lines == [
        "fdetimes: 21 fresh roles, 10 new, 0 already stored, 11 skipped (kind, region or role)"
    ]
    lines.clear()
    pipeline.import_fdetimes(
        session, settings(tmp_path, database_url), lines.append, FakeList(FDETimesError("x"))
    )
    assert lines == ["fdetimes: could not read the job list: x"]
    lines.clear()
    off = settings(tmp_path, database_url, fdetimes=False)
    pipeline.import_fdetimes(session, off, lines.append, FakeList())
    assert lines == ["fdetimes: disabled (JOB_RADAR_FDETIMES=false)."]
