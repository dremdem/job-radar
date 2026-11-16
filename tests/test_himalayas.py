"""Tests for job_radar.sources.boards.himalayas: parsing API jobs, the noise filter, first and next
runs and rate limits, with a fake client (docs/himalayas.md).
"""

import datetime as dt
import json
import pathlib

import pytest
import sqlalchemy as sa

import job_radar.models as models
import job_radar.sources.boards.himalayas as boards_himalayas

FIXTURE = pathlib.Path(__file__).parent / "fixtures" / "himalayas" / "search_page.json"
# The fixture was fetched around this moment (newest pubDate is ~3 h earlier).
NOW = dt.datetime.fromtimestamp(1790845000, dt.UTC)


def page() -> dict:
    return json.loads(FIXTURE.read_text())


def job(title_start: str) -> dict:
    return next(j for j in page()["jobs"] if j["title"].startswith(title_start))


class FakeClient:
    """Serves search pages by number, or raises for a page."""

    def __init__(self, pages: dict[int, dict | Exception]):
        self.pages = pages
        self.requested: list[int] = []

    def search(self, page_number: int) -> dict:
        self.requested.append(page_number)
        result = self.pages.get(page_number, {"jobs": []})
        if isinstance(result, Exception):
            raise result
        return result


def test_parse_worldwide_job():
    parsed = boards_himalayas.parse_job(job("Backend Software Engineer (Senior/Staff)"))
    assert parsed.title == "Backend Software Engineer (Senior/Staff)"
    assert parsed.company == "DualEntry"
    assert parsed.location == "Worldwide (Remote)"
    assert parsed.salary == "100,000-175,000 USD / year"
    assert parsed.url.startswith("https://himalayas.app/companies/dualentry/jobs/")
    assert parsed.published_at == dt.datetime.fromtimestamp(1790834064, dt.UTC)
    # Plain text, paragraphs kept apart, no tags.
    assert "<" not in parsed.description
    assert "About DualEntry\n" in parsed.description


def test_timezone_restrictions_do_not_limit_the_location():
    # Restricted to UTC-1..+3 only: any timezone is fine for the user (#91).
    parsed = boards_himalayas.parse_job(job("Senior Data Scraping"))
    assert parsed.location == "Worldwide (Remote)"
    assert parsed.salary is None


@pytest.mark.parametrize(
    ("title", "wanted"),
    [
        ("Remote Backend Django Engineer", True),
        ("Backend Software Engineer (Senior/Staff)", True),
        # Python only in Himalayas' categories
        ("Senior Data Scraping", True),
        # security role
        ("Security Architect", False),
        # Python only in categories; the queue flags the description
        ("Backend Engineer", True),
        # analyst
        ("Business Intelligence Analyst", False),
        # not a developer role
        ("Slot Mathematician", False),
    ],
)
def test_noise_filter(title, wanted):
    assert boards_himalayas.wants(boards_himalayas.parse_job(job(title))) is wanted


def test_first_run_imports_the_last_day_only(session):
    old = {"jobs": [job("Remote Backend Django Engineer"), job("Backend Engineer")]}
    client = FakeClient({1: page(), 2: old, 3: page()})
    stats = boards_himalayas.import_himalayas(session, client, now=NOW, first_run_days=1, delay=0)

    # Page 2 is entirely older than a day: reading stops there.
    assert client.requested == [1, 2]
    titles = session.scalars(sa.select(models.Opportunity.title)).all()
    # Within a day and past the filter: DualEntry only (Security, BI, Slot are noise).
    assert titles == ["Backend Software Engineer (Senior/Staff)"]
    assert stats.new == 1 and stats.skipped == 3

    record = session.scalars(sa.select(models.SourceRecord)).one()
    assert record.source_type is models.SourceType.HIMALAYAS
    assert record.external_id == record.url
    snapshot = session.scalars(sa.select(models.JobSnapshot)).one()
    assert "DualEntry" in snapshot.raw_text
    o = record.opportunity
    assert o.location == "Worldwide (Remote)" and o.salary == "100,000-175,000 USD / year"
    assert any(r["text"] == "Worldwide / anywhere" for r in o.score_reasons)

    cursor = session.get(models.SyncCursor, "himalayas")
    assert cursor.last_run_at.replace(tzinfo=dt.UTC) == NOW


def test_next_run_reads_from_last_run_without_duplicates(session):
    boards_himalayas.import_himalayas(
        session, FakeClient({1: page()}), now=NOW, first_run_days=7, delay=0
    )
    first = session.scalars(sa.select(models.Opportunity.title)).all()
    assert sorted(first) == [
        "Backend Engineer",
        "Backend Software Engineer (Senior/Staff)",
        "Remote Backend Django Engineer – AI & Algorithmic Systems",
        "Senior Data Scraping / Web Developer — Part-time Contract (EU)",
    ]

    stats = boards_himalayas.import_himalayas(
        session, FakeClient({1: page()}), now=NOW, first_run_days=7, delay=0
    )
    # Re-reads one day before the last run (MARGIN): only DualEntry again, already stored.
    assert stats.new == 0 and stats.known == 1
    assert session.scalars(sa.select(models.Opportunity.title)).all() == first


def test_rate_limit_stops_without_moving_the_cursor(session):
    client = FakeClient({1: page(), 2: boards_himalayas.HimalayasRateLimited("429")})
    stats = boards_himalayas.import_himalayas(session, client, now=NOW, first_run_days=7, delay=0)
    assert stats.blocked
    # page 1 is kept
    assert stats.new == 4
    assert session.get(models.SyncCursor, "himalayas") is None


def test_page_cap(session):
    client = FakeClient(dict.fromkeys(range(1, 30), page()))
    boards_himalayas.import_himalayas(
        session, client, now=NOW, first_run_days=30, delay=0, max_pages=4
    )
    assert client.requested == [1, 2, 3, 4]


def test_pipeline_step_reports(session, monkeypatch):
    import job_radar.config as config
    import job_radar.pipeline as pipeline

    monkeypatch.setattr("job_radar.sources.boards.himalayas.time.sleep", lambda seconds: None)

    lines: list[str] = []
    client = FakeClient({1: page(), 2: boards_himalayas.HimalayasRateLimited("429")})
    pipeline.import_himalayas(
        session, config.Settings(himalayas_first_run_days=3650), lines.append, client
    )
    assert lines[0].startswith("himalayas: 1 pages, 7 fresh jobs, 4 new")
    assert "rate limited" in lines[1]


def test_country_restrictions_stay_in_the_location():
    raw = job("Senior Data Scraping") | {"locationRestrictions": ["Poland", "Germany"]}
    assert boards_himalayas.parse_job(raw).location == "Remote, Poland, Germany"
