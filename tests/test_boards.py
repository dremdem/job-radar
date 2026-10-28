"""Remotive, Working Nomads, We Work Remotely in one step (#233, docs/boards.md).

Fixtures: trimmed copies of each feed fetched on 2026-10-27/28 (real jobs).
"""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import select

from job_radar.boards import (
    BOARDS,
    BoardError,
    import_boards,
    location,
    parse_remotive,
    parse_weworkremotely,
    parse_workingnomads,
    wants,
)
from job_radar.models import Opportunity, SourceRecord, SourceType

FIXTURES = Path(__file__).parent / "fixtures" / "boards"
NOW = datetime(2026, 10, 28, 8, 0, tzinfo=UTC)


def fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def body_for(url: str) -> bytes:
    if "remotive" in url:
        return fixture("remotive.json")
    if "workingnomads" in url:
        return fixture("workingnomads.json")
    return fixture("weworkremotely.rss")  # both categories: the same jobs twice


class FakeClient:
    def __init__(self, fail: str | None = None):
        self.fail = fail
        self.urls: list[str] = []

    def get(self, url: str) -> bytes:
        self.urls.append(url)
        if self.fail and self.fail in url:
            raise BoardError("HTTP 503")
        return body_for(url)


def by_title(jobs, start):
    return next(j for j in jobs if j.title.startswith(start))


def test_locations():
    assert location("Worldwide") == "Worldwide (Remote)"
    assert location("Anywhere in the World") == "Worldwide (Remote)"
    assert location("") == "Worldwide (Remote)"
    assert location("USA Only") == "Remote, USA Only"


def test_remotive_fields():
    j = by_title(parse_remotive(fixture("remotive.json")), "Software Engineer, AI Coding")
    assert (j.id, j.company, j.location) == ("2091158", "Askable", "Worldwide (Remote)")
    assert j.url.startswith("https://remotive.com/")
    assert j.published_at == datetime(2026, 10, 17, 16, 4, 29, tzinfo=UTC)


def test_working_nomads_fields():
    j = by_title(parse_workingnomads(fixture("workingnomads.json")), "Senior Django Developer")
    assert (j.id, j.company, j.location) == ("1925616", "Proxify", "Remote, CET (+/- 3 hours)")
    assert j.published_at == datetime(2026, 10, 8, 16, 48, 51, tzinfo=UTC)  # from -04:00


def test_we_work_remotely_splits_company_and_role():
    j = by_title(parse_weworkremotely(fixture("weworkremotely.rss")), "Applied AI Engineers")
    assert (j.company, j.location) == ("Toptal", "Remote, USA Only")
    assert j.url == j.id and j.url.startswith("https://weworkremotely.com/remote-jobs/toptal-")
    assert "<p>" not in j.description and j.description


def test_noise_filter():
    jobs = parse_remotive(fixture("remotive.json")) + parse_workingnomads(
        fixture("workingnomads.json")
    )
    assert not wants(by_title(jobs, "Freelance Writer"))
    assert not wants(by_title(jobs, "Looking For Online ESL Teachers"))
    assert not wants(by_title(jobs, "Tech Lead Full-Stack Rails"))  # no Python
    assert wants(by_title(jobs, "Senior Backend Developer (Python)"))


def test_a_title_with_another_stack_and_no_python_is_skipped():
    # Lemon.io posts list Python in the boilerplate of every stack's vacancy.
    base = by_title(parse_workingnomads(fixture("workingnomads.json")), "Senior DevOps Engineer")
    assert "Python" in base.description
    for title in (
        "Senior Swift & SwiftUI Developer",
        "Senior Full-stack PHP Developer",
        "Senior Angular Developer",
        "Senior React Full-stack Developer",
        "Senior .NET Full-stack Developer",
        "Senior C++ Engineer",
    ):
        assert not wants(replace(base, title=title)), title
    assert wants(replace(base, title="Senior Python/React Developer"))
    assert wants(base)


def test_we_work_remotely_location_is_the_region_not_the_country_list():
    rss = fixture("weworkremotely.rss").replace(
        b"<country></country>", b"<country>Andorra, Albania, Thailand</country>", 1
    )
    jobs = parse_weworkremotely(rss)
    assert {j.location for j in jobs} <= {"Worldwide (Remote)", "Remote, USA Only"}


def test_sync_imports_each_board_once(session):
    client = FakeClient()
    results = import_boards(session, client, now=NOW)
    counts = {s.label: (s.jobs, s.old, s.skipped, s.new) for s in results}
    assert counts == {
        "Remotive": (4, 0, 2, 2),
        "Working Nomads": (4, 0, 1, 3),
        # Five jobs in both categories count once; one is from 2024.
        "We Work Remotely": (5, 1, 1, 3),
    }
    assert len(client.urls) == sum(len(b.urls) for b in BOARDS)
    types = list(session.scalars(select(SourceRecord.source_type)))
    assert types.count(SourceType.REMOTIVE) == 2
    assert types.count(SourceType.WORKINGNOMADS) == 3
    assert types.count(SourceType.WEWORKREMOTELY) == 3

    later = NOW + timedelta(hours=7)
    again = import_boards(session, FakeClient(), now=later)
    assert [(s.new, s.known) for s in again] == [(0, 2), (0, 3), (0, 3)]
    assert session.query(Opportunity).count() == 8


def test_each_board_is_read_at_most_every_six_hours(session):
    import_boards(session, FakeClient(), now=NOW)
    client = FakeClient()
    results = import_boards(session, client, now=NOW + timedelta(hours=5))
    assert client.urls == []
    assert all(s.waited for s in results)


def test_a_failed_board_does_not_stop_the_others_and_is_retried(session):
    results = import_boards(session, FakeClient(fail="workingnomads"), now=NOW)
    assert [s.error for s in results] == [None, "HTTP 503", None]
    assert [s.new for s in results] == [2, 0, 3]
    # Not marked as read: the next sync tries it again.
    client = FakeClient()
    retry = import_boards(session, client, now=NOW + timedelta(hours=1))
    assert [s.waited for s in retry] == [True, False, True]
    assert retry[1].new == 3


def test_sync_step_reports_each_board_and_can_be_turned_off(session):
    from job_radar.config import Settings
    from job_radar.pipeline import import_boards as step

    lines: list[str] = []
    step(session, Settings(boards=False), lines.append, client=FakeClient())
    assert lines == ["boards: disabled (JOB_RADAR_BOARDS=false)."]
    lines.clear()
    step(session, Settings(boards=True), lines.append, client=FakeClient(fail="remotive"))
    assert lines[0] == "boards: Remotive: could not read the feed: HTTP 503"
    assert lines[-1].startswith("boards: ") and lines[-1].endswith(" new")
