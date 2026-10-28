"""Fresh Python jobs from the Remote OK feed (#233, docs/remoteok.md).

The fixture is a trimmed copy of https://remoteok.com/api?tag=python fetched on 2026-10-27:
the terms-of-use element and nine real jobs.
"""

import json
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select

from job_radar.models import JobSnapshot, Opportunity, SourceRecord, SourceType
from job_radar.remoteok import RemoteOKError, _fix, import_remoteok, parse_job, wants

FIXTURE = Path(__file__).parent / "fixtures" / "remoteok" / "api.json"
NOW = datetime(2026, 10, 27, tzinfo=UTC)


def feed() -> list[dict]:
    return json.loads(FIXTURE.read_text())


def job(position: str):
    return parse_job(next(j for j in feed()[1:] if j["position"].strip() == position))


class FakeClient:
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
    assert job("APAC Python").salary is None  # 0 means not given


def test_double_encoded_letters_are_repaired():
    assert job("PYTHON DEVELOPER REMOTO").location == (
        "Remote, Madrid, Madrid, Comunidad de Madrid, España"
    )
    assert _fix("España") == "España"  # correct text stays as it is
    assert _fix("Python — remote") == "Python — remote"


def test_title_filter():
    assert wants(job("Senior React Full stack Developer"))
    assert wants(job("APAC Python"))
    assert not wants(job("Risk Analyst"))
    assert not wants(job("Associate Data Scientist"))


def test_sync_imports_fresh_jobs_once(session):
    stats = import_remoteok(session, FakeClient(), now=NOW)
    # Nine jobs: two older than 30 days, two not developer roles, five new.
    assert (stats.jobs, stats.old, stats.skipped, stats.new, stats.known) == (9, 2, 2, 5, 0)
    titles = set(session.scalars(select(Opportunity.title)))
    assert titles == {
        "Senior Software Engineer LATAM",
        "Solutions Engineer",
        "APAC Python",
        "Principal Software Engineer",
        "Senior React Full stack Developer",
    }
    record = session.scalar(
        select(SourceRecord).where(SourceRecord.source_type == SourceType.REMOTEOK)
    )
    assert record.external_id.isdigit() and "remoteok.com" in record.url.lower()
    snapshot = session.scalar(select(JobSnapshot).where(JobSnapshot.source_record_id == record.id))
    assert snapshot.raw_text and "<p" not in snapshot.raw_text

    again = import_remoteok(session, FakeClient(), now=NOW)
    assert (again.new, again.known) == (0, 5)


def test_an_unreadable_feed_is_reported(session):
    stats = import_remoteok(session, FakeClient(RemoteOKError("HTTP 403")), now=NOW)
    assert stats.error == "HTTP 403"
    assert session.query(Opportunity).count() == 0


def test_sync_step_reports_counts_and_can_be_turned_off(session, monkeypatch):
    from job_radar import remoteok
    from job_radar.config import Settings
    from job_radar.pipeline import import_remoteok as step

    real = remoteok.import_remoteok
    monkeypatch.setattr(remoteok, "import_remoteok", lambda s, c: real(s, c, now=NOW))
    lines: list[str] = []
    step(session, Settings(remoteok=True), lines.append, client=FakeClient())
    assert lines == [
        "remoteok: 9 jobs, 5 new, 0 already stored, 2 skipped as noise, 2 older than 30 days"
    ]
    lines.clear()
    step(session, Settings(remoteok=False), lines.append, client=FakeClient())
    assert lines == ["remoteok: disabled (JOB_RADAR_REMOTEOK=false)."]
