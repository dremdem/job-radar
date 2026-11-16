"""Fresh Python jobs from the Jobicy API (#233, docs/jobicy.md).

Fixtures: trimmed copies of two consecutive pages of
https://jobicy.com/api/v2/remote-jobs?count=50&tag=python fetched on 2026-10-28.
Page 1: 7 jobs from 2026-10-26 to 2026-10-28, five of them developer roles.
Page 2: 5 jobs, none a developer role, two of them from 2026-10-24.
"""

import datetime as dt
import json
import pathlib

import sqlalchemy as sa

import job_radar.jobicy as jobicy
import job_radar.models as models

FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "jobicy"
NOW = dt.datetime(2026, 10, 28, 4, 0, tzinfo=dt.UTC)


def page(n: int) -> dict:
    return json.loads((FIXTURES / f"page{n}.json").read_text())


def job(title_start: str):
    raw = next(j for n in (1, 2) for j in page(n)["jobs"] if j["jobTitle"].startswith(title_start))
    return jobicy.parse_job(raw)


class FakeClient:
    """Serves Jobicy pages in order, or raises."""

    def __init__(self, pages: list[dict | Exception] | None = None):
        self.pages = [page(1), page(2)] if pages is None else pages
        self.cursors: list[str | None] = []

    def page(self, cursor: str | None) -> dict:
        self.cursors.append(cursor)
        result = self.pages[len(self.cursors) - 1]
        if isinstance(result, Exception):
            raise result
        return result


def last_run(session, at: dt.datetime) -> None:
    session.add(models.SyncCursor(name="jobicy", last_run_at=at))
    session.commit()


def test_fields_from_the_api():
    j = job("Senior Backend Engineer")
    assert (j.title, j.company, j.location) == (
        "Senior Backend Engineer",
        "RevenueCat",
        "Remote, EMEA, LATAM, Canada, USA",
    )
    assert j.url.startswith("https://jobicy.com/jobs/")
    assert j.published_at == dt.datetime(2026, 10, 26, 10, 35, 50, tzinfo=dt.UTC)


def test_anywhere_is_worldwide():
    assert jobicy._location("Anywhere") == "Worldwide (Remote)"
    assert jobicy._location("") == "Worldwide (Remote)"
    assert jobicy._location("Canada,  USA") == "Remote, Canada, USA"


def test_title_filter():
    assert jobicy.wants(job("Staff Backend Engineer"))
    # scientist
    assert not jobicy.wants(job("Applied Data Scientist"))
    assert not jobicy.wants(job("Lead Growth Analyst"))
    assert not jobicy.wants(job("Infrastructure Security Engineer"))
    assert not jobicy.wants(job("Technical Support Engineer"))
    assert not jobicy.wants(job("Partner Sales Engineer"))


def test_first_run_reads_one_day_back(session):
    client = FakeClient()
    stats = jobicy.import_jobicy(
        session, client, now=dt.datetime(2026, 10, 27, 12, 0, tzinfo=dt.UTC), delay=0
    )
    # Since 2026-10-26 12:00: Staff Backend Engineer (new), the data scientist and the
    # analyst (skipped). Page 1 already reaches past that point, so page 2 is not asked.
    assert client.cursors == [None]
    assert (stats.pages, stats.jobs, stats.new, stats.skipped) == (1, 3, 1, 2)
    assert list(session.scalars(sa.select(models.Opportunity.title))) == ["Staff Backend Engineer"]


def test_later_runs_page_back_to_the_last_run_and_import_once(session):
    # read since 2026-10-25
    last_run(session, dt.datetime(2026, 10, 26, tzinfo=dt.UTC))
    client = FakeClient()
    stats = jobicy.import_jobicy(session, client, now=NOW, delay=0)
    assert client.cursors == [None, page(1)["nextCursor"]]
    # Page 1: 7 fresh, 5 new. Page 2: 3 fresh and skipped, 2 older and ignored; then stop.
    assert (stats.pages, stats.jobs, stats.new, stats.skipped) == (2, 10, 5, 5)
    assert session.get(models.SyncCursor, "jobicy").last_run_at.replace(tzinfo=dt.UTC) == NOW
    record = session.scalar(
        sa.select(models.SourceRecord).where(
            models.SourceRecord.external_id == page(1)["jobs"][0]["id"]
        )
    )
    assert record.source_type is models.SourceType.JOBICY
    assert record.url == page(1)["jobs"][0]["url"]

    # Same window again: everything is already stored.
    session.get(models.SyncCursor, "jobicy").last_run_at = dt.datetime(2026, 10, 26, tzinfo=dt.UTC)
    session.commit()
    again = jobicy.import_jobicy(session, FakeClient(), now=NOW, delay=0)
    assert (again.new, again.known) == (0, 5)


def test_a_failed_page_keeps_the_last_run_so_nothing_is_missed(session):
    before = dt.datetime(2026, 10, 26, tzinfo=dt.UTC)
    last_run(session, before)
    stats = jobicy.import_jobicy(
        session, FakeClient([page(1), jobicy.JobicyError("HTTP 500")]), now=NOW, delay=0
    )
    assert stats.error == "HTTP 500"
    # page 1 is stored
    assert stats.new == 5
    assert session.get(models.SyncCursor, "jobicy").last_run_at.replace(tzinfo=dt.UTC) == before


def test_sync_step_reports_counts_and_can_be_turned_off(session):
    import job_radar.config as config
    import job_radar.pipeline as pipeline

    lines: list[str] = []
    pipeline.import_jobicy(
        session, config.Settings(jobicy=False), lines.append, client=FakeClient()
    )
    assert lines == ["jobicy: disabled (JOB_RADAR_JOBICY=false)."]
    lines.clear()
    pipeline.import_jobicy(
        session,
        config.Settings(jobicy=True),
        lines.append,
        client=FakeClient([jobicy.JobicyError("HTTP 503")]),
    )
    assert lines == [
        "jobicy: 0 pages, 0 fresh jobs, 0 new, 0 already stored, 0 skipped as noise",
        "jobicy: could not read the API: HTTP 503",
    ]
