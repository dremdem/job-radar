"""Fresh Python jobs from the Remote OK feed (#233, docs/remoteok.md).

The fixture is a trimmed copy of https://remoteok.com/api?tag=python fetched on 2026-10-27:
the terms-of-use element and nine real jobs.
"""

import datetime as dt
import json

import paths
import sqlalchemy as sa

import job_radar.models as models
import job_radar.sources.boards.remoteok as boards_remoteok

FIXTURE = paths.FIXTURES / "remoteok" / "api.json"
NOW = dt.datetime(2026, 10, 27, tzinfo=dt.UTC)


def feed() -> list[dict]:
    return json.loads(FIXTURE.read_text())


def job(position: str):
    return boards_remoteok.parse_job(
        next(j for j in feed()[1:] if j["position"].strip() == position)
    )


class FakeClient:
    """Returns the feed fixture, or raises."""

    def __init__(self, result: list | Exception | None = None):
        self.result = feed() if result is None else result

    def feed(self) -> list[dict]:
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def test_fields_from_the_feed():
    j = job("Senior Software Engineer LATAM")
    assert (j.title, j.company, j.location, j.salary) == (
        "Senior Software Engineer LATAM",
        "Wagmo",
        "Remote",
        "100,000-140,000 USD / year",
    )
    assert j.url.startswith("https://remoteOK.com/remote-jobs/")
    assert job("Principal Software Engineer").location == "Remote, Kansas"
    # 0 means not given
    assert job("APAC Python").salary is None


def test_double_encoded_letters_are_repaired():
    assert job("PYTHON DEVELOPER REMOTO").location == (
        "Remote, Madrid, Madrid, Comunidad de Madrid, España"
    )
    # correct text stays as it is
    assert boards_remoteok._fix("España") == "España"
    assert boards_remoteok._fix("Python — remote") == "Python — remote"


def test_title_filter():
    assert boards_remoteok.wants(job("Senior React Full stack Developer"))
    assert boards_remoteok.wants(job("APAC Python"))
    assert not boards_remoteok.wants(job("Risk Analyst"))
    assert not boards_remoteok.wants(job("Associate Data Scientist"))


def test_sync_imports_fresh_jobs_once(session):
    stats = boards_remoteok.import_remoteok(session, FakeClient(), now=NOW)
    # Nine jobs: two older than 30 days, two not developer roles, five new.
    assert (stats.jobs, stats.old, stats.skipped, stats.new, stats.known) == (9, 2, 2, 5, 0)
    titles = set(session.scalars(sa.select(models.Opportunity.title)))
    assert titles == {
        "Senior Software Engineer LATAM",
        "Solutions Engineer",
        "APAC Python",
        "Principal Software Engineer",
        "Senior React Full stack Developer",
    }
    record = session.scalar(
        sa.select(models.SourceRecord).where(
            models.SourceRecord.source_type == models.SourceType.REMOTEOK
        )
    )
    assert record.external_id.isdigit() and "remoteok.com" in record.url.lower()
    snapshot = session.scalar(
        sa.select(models.JobSnapshot).where(models.JobSnapshot.source_record_id == record.id)
    )
    assert snapshot.raw_text and "<p" not in snapshot.raw_text

    again = boards_remoteok.import_remoteok(session, FakeClient(), now=NOW)
    assert (again.new, again.known) == (0, 5)


def test_an_unreadable_feed_is_reported(session):
    stats = boards_remoteok.import_remoteok(
        session, FakeClient(boards_remoteok.RemoteOKError("HTTP 403")), now=NOW
    )
    assert stats.error == "HTTP 403"
    assert session.query(models.Opportunity).count() == 0


def test_sync_step_reports_counts_and_can_be_turned_off(session, monkeypatch):
    import job_radar.config as config
    import job_radar.jobs.pipeline as jobs_pipeline

    real = boards_remoteok.import_remoteok
    monkeypatch.setattr(boards_remoteok, "import_remoteok", lambda s, c: real(s, c, now=NOW))
    lines: list[str] = []
    jobs_pipeline.import_remoteok(
        session, config.Settings(remoteok=True), lines.append, client=FakeClient()
    )
    assert lines == [
        "remoteok: 9 jobs, 5 new, 0 already stored, 2 skipped as noise, 2 older than 30 days"
    ]
    lines.clear()
    jobs_pipeline.import_remoteok(
        session, config.Settings(remoteok=False), lines.append, client=FakeClient()
    )
    assert lines == ["remoteok: disabled (JOB_RADAR_REMOTEOK=false)."]
