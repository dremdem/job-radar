"""Remotive, Working Nomads, We Work Remotely in one step (#233, docs/boards.md).

Fixtures: trimmed copies of each feed fetched on 2026-10-27/28 (real jobs).
"""

import dataclasses
import datetime as dt
import pathlib

import pytest
import sqlalchemy as sa

import job_radar.boards as boards
import job_radar.models as models

FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "boards"
NOW = dt.datetime(2026, 10, 28, 8, 0, tzinfo=dt.UTC)


def fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def body_for(url: str) -> bytes:
    if "remotive" in url:
        return fixture("remotive.json")
    if "workingnomads" in url:
        return fixture("workingnomads.json")
    # both categories: the same jobs twice
    return fixture("weworkremotely.rss")


class FakeClient:
    """Serves the board fixtures by URL; can fail for one URL."""

    def __init__(self, fail: str | None = None):
        self.fail = fail
        self.urls: list[str] = []

    def get(self, url: str) -> bytes:
        self.urls.append(url)
        if self.fail and self.fail in url:
            raise boards.BoardError("HTTP 503")
        return body_for(url)


def by_title(jobs, start):
    return next(j for j in jobs if j.title.startswith(start))


def test_locations():
    assert boards.location("Worldwide") == "Worldwide (Remote)"
    assert boards.location("Anywhere in the World") == "Worldwide (Remote)"
    assert boards.location("") == "Worldwide (Remote)"
    assert boards.location("USA Only") == "Remote, USA Only"


def test_remotive_fields():
    j = by_title(boards.parse_remotive(fixture("remotive.json")), "Software Engineer, AI Coding")
    assert (j.id, j.company, j.location) == ("2091158", "Askable", "Worldwide (Remote)")
    assert j.url.startswith("https://remotive.com/")
    assert j.published_at == dt.datetime(2026, 10, 17, 16, 4, 29, tzinfo=dt.UTC)


def test_working_nomads_fields():
    j = by_title(
        boards.parse_workingnomads(fixture("workingnomads.json")), "Senior Django Developer"
    )
    assert (j.id, j.company, j.location) == ("1925616", "Proxify", "Remote, CET (+/- 3 hours)")
    # from -04:00
    assert j.published_at == dt.datetime(2026, 10, 8, 16, 48, 51, tzinfo=dt.UTC)


def test_we_work_remotely_splits_company_and_role():
    j = by_title(boards.parse_weworkremotely(fixture("weworkremotely.rss")), "Applied AI Engineers")
    assert (j.company, j.location) == ("Toptal", "Remote, USA Only")
    assert j.url == j.id and j.url.startswith("https://weworkremotely.com/remote-jobs/toptal-")
    assert "<p>" not in j.description and j.description


def test_noise_filter():
    jobs = boards.parse_remotive(fixture("remotive.json")) + boards.parse_workingnomads(
        fixture("workingnomads.json")
    )
    assert not boards.wants(by_title(jobs, "Freelance Writer"))
    assert not boards.wants(by_title(jobs, "Looking For Online ESL Teachers"))
    # no Python
    assert not boards.wants(by_title(jobs, "Tech Lead Full-Stack Rails"))
    assert boards.wants(by_title(jobs, "Senior Backend Developer (Python)"))


def test_a_title_with_another_stack_and_no_python_is_skipped():
    # Lemon.io posts list Python in the boilerplate of every stack's vacancy.
    base = by_title(
        boards.parse_workingnomads(fixture("workingnomads.json")), "Senior DevOps Engineer"
    )
    assert "Python" in base.description
    for title in (
        "Senior Swift & SwiftUI Developer",
        "Senior Full-stack PHP Developer",
        "Senior Angular Developer",
        "Senior React Full-stack Developer",
        "Senior .NET Full-stack Developer",
        "Senior C++ Engineer",
    ):
        assert not boards.wants(dataclasses.replace(base, title=title)), title
    assert boards.wants(dataclasses.replace(base, title="Senior Python/React Developer"))
    assert boards.wants(base)


def test_we_work_remotely_location_is_the_region_not_the_country_list():
    rss = fixture("weworkremotely.rss").replace(
        b"<country></country>", b"<country>Andorra, Albania, Thailand</country>", 1
    )
    jobs = boards.parse_weworkremotely(rss)
    assert {j.location for j in jobs} <= {"Worldwide (Remote)", "Remote, USA Only"}


def test_sync_imports_each_board_once(session):
    client = FakeClient()
    results = boards.import_boards(session, client, now=NOW)
    counts = {s.label: (s.jobs, s.old, s.skipped, s.new) for s in results}
    assert counts == {
        "Remotive": (4, 0, 2, 2),
        "Working Nomads": (4, 0, 1, 3),
        # Five jobs in both categories count once; one is from 2024.
        "We Work Remotely": (5, 1, 1, 3),
    }
    assert len(client.urls) == sum(len(b.urls) for b in boards.BOARDS)
    types = list(session.scalars(sa.select(models.SourceRecord.source_type)))
    assert types.count(models.SourceType.REMOTIVE) == 2
    assert types.count(models.SourceType.WORKINGNOMADS) == 3
    assert types.count(models.SourceType.WEWORKREMOTELY) == 3

    later = NOW + dt.timedelta(hours=7)
    again = boards.import_boards(session, FakeClient(), now=later)
    assert [(s.new, s.known) for s in again] == [(0, 2), (0, 3), (0, 3)]
    assert session.query(models.Opportunity).count() == 8


def test_each_board_is_read_at_most_every_six_hours(session):
    boards.import_boards(session, FakeClient(), now=NOW)
    client = FakeClient()
    results = boards.import_boards(session, client, now=NOW + dt.timedelta(hours=5))
    assert client.urls == []
    assert all(s.waited for s in results)


def test_a_failed_board_does_not_stop_the_others_and_is_retried(session):
    results = boards.import_boards(session, FakeClient(fail="workingnomads"), now=NOW)
    assert [s.error for s in results] == [None, "HTTP 503", None]
    assert [s.new for s in results] == [2, 0, 3]
    # Not marked as read: the next sync tries it again.
    client = FakeClient()
    retry = boards.import_boards(session, client, now=NOW + dt.timedelta(hours=1))
    assert [s.waited for s in retry] == [True, False, True]
    assert retry[1].new == 3


def test_sync_step_reports_each_board_and_can_be_turned_off(session):
    import job_radar.config as config
    import job_radar.pipeline as pipeline

    lines: list[str] = []
    pipeline.import_boards(
        session, config.Settings(boards=False), lines.append, client=FakeClient()
    )
    assert lines == ["boards: disabled (JOB_RADAR_BOARDS=false)."]
    lines.clear()
    pipeline.import_boards(
        session, config.Settings(boards=True), lines.append, client=FakeClient(fail="remotive")
    )
    assert lines[0] == "boards: Remotive: could not read the feed: HTTP 503"
    assert lines[-1].startswith("boards: ") and lines[-1].endswith(" new")


def test_weworkremotely_feed_with_entities_is_refused():
    """A feed that defines XML entities (billion laughs) is an error, not parsed."""
    bomb = (
        b'<?xml version="1.0"?><!DOCTYPE r [<!ENTITY a "aaaaaaaaaa">'
        b'<!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;&a;&a;">]><rss><channel><item>'
        b"<title>&b;</title></item></channel></rss>"
    )
    with pytest.raises(boards.BoardError):
        boards.parse_weworkremotely(bomb)
