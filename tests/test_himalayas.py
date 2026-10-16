import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import select

from job_radar.himalayas import (
    HimalayasRateLimited,
    import_himalayas,
    parse_job,
    wants,
)
from job_radar.models import JobSnapshot, Opportunity, SourceRecord, SourceType, SyncCursor

FIXTURE = Path(__file__).parent / "fixtures" / "himalayas" / "search_page.json"
# The fixture was fetched around this moment (newest pubDate is ~3 h earlier).
NOW = datetime.fromtimestamp(1790845000, UTC)


def page() -> dict:
    return json.loads(FIXTURE.read_text())


def job(title_start: str) -> dict:
    return next(j for j in page()["jobs"] if j["title"].startswith(title_start))


class FakeClient:
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
    parsed = parse_job(job("Backend Software Engineer (Senior/Staff)"))
    assert parsed.title == "Backend Software Engineer (Senior/Staff)"
    assert parsed.company == "DualEntry"
    assert parsed.location == "Worldwide (Remote)"
    assert parsed.salary == "100,000-175,000 USD / year"
    assert parsed.url.startswith("https://himalayas.app/companies/dualentry/jobs/")
    assert parsed.published_at == datetime.fromtimestamp(1790834064, UTC)
    # Plain text, paragraphs kept apart, no tags.
    assert "<" not in parsed.description
    assert "About DualEntry\n" in parsed.description


def test_timezone_restrictions_do_not_limit_the_location():
    # Restricted to UTC-1..+3 only: any timezone is fine for the user (#91).
    parsed = parse_job(job("Senior Data Scraping"))
    assert parsed.location == "Worldwide (Remote)"
    assert parsed.salary is None


@pytest.mark.parametrize(
    ("title", "wanted"),
    [
        ("Remote Backend Django Engineer", True),
        ("Backend Software Engineer (Senior/Staff)", True),
        ("Senior Data Scraping", True),  # Python only in Himalayas' categories
        ("Security Architect", False),  # security role
        ("Backend Engineer", True),  # Python only in categories; the queue flags the description
        ("Business Intelligence Analyst", False),  # analyst
        ("Slot Mathematician", False),  # not a developer role
    ],
)
def test_noise_filter(title, wanted):
    assert wants(parse_job(job(title))) is wanted


def test_first_run_imports_the_last_day_only(session):
    old = {"jobs": [job("Remote Backend Django Engineer"), job("Backend Engineer")]}
    client = FakeClient({1: page(), 2: old, 3: page()})
    stats = import_himalayas(session, client, now=NOW, first_run_days=1, delay=0)

    # Page 2 is entirely older than a day: reading stops there.
    assert client.requested == [1, 2]
    titles = session.scalars(select(Opportunity.title)).all()
    # Within a day and past the filter: DualEntry only (Security, BI, Slot are noise).
    assert titles == ["Backend Software Engineer (Senior/Staff)"]
    assert stats.new == 1 and stats.skipped == 3

    record = session.scalars(select(SourceRecord)).one()
    assert record.source_type is SourceType.HIMALAYAS
    assert record.external_id == record.url
    snapshot = session.scalars(select(JobSnapshot)).one()
    assert "DualEntry" in snapshot.raw_text
    o = record.opportunity
    assert o.location == "Worldwide (Remote)" and o.salary == "100,000-175,000 USD / year"
    assert any(r["text"] == "Worldwide / anywhere" for r in o.score_reasons)

    cursor = session.get(SyncCursor, "himalayas")
    assert cursor.last_run_at.replace(tzinfo=UTC) == NOW


def test_next_run_reads_from_last_run_without_duplicates(session):
    import_himalayas(session, FakeClient({1: page()}), now=NOW, first_run_days=7, delay=0)
    first = session.scalars(select(Opportunity.title)).all()
    assert sorted(first) == [
        "Backend Engineer",
        "Backend Software Engineer (Senior/Staff)",
        "Remote Backend Django Engineer – AI & Algorithmic Systems",
        "Senior Data Scraping / Web Developer — Part-time Contract (EU)",
    ]

    stats = import_himalayas(session, FakeClient({1: page()}), now=NOW, first_run_days=7, delay=0)
    # Re-reads one day before the last run (MARGIN): only DualEntry again, already stored.
    assert stats.new == 0 and stats.known == 1
    assert session.scalars(select(Opportunity.title)).all() == first


def test_rate_limit_stops_without_moving_the_cursor(session):
    client = FakeClient({1: page(), 2: HimalayasRateLimited("429")})
    stats = import_himalayas(session, client, now=NOW, first_run_days=7, delay=0)
    assert stats.blocked
    assert stats.new == 4  # page 1 is kept
    assert session.get(SyncCursor, "himalayas") is None


def test_page_cap(session):
    client = FakeClient(dict.fromkeys(range(1, 30), page()))
    import_himalayas(session, client, now=NOW, first_run_days=30, delay=0, max_pages=4)
    assert client.requested == [1, 2, 3, 4]


def test_pipeline_step_reports(session, monkeypatch):
    from job_radar import pipeline
    from job_radar.config import Settings

    monkeypatch.setattr("job_radar.himalayas.time.sleep", lambda seconds: None)

    lines: list[str] = []
    client = FakeClient({1: page(), 2: HimalayasRateLimited("429")})
    pipeline.import_himalayas(
        session, Settings(himalayas_first_run_days=3650), lines.append, client
    )
    assert lines[0].startswith("himalayas: 1 pages, 7 fresh jobs, 4 new")
    assert "rate limited" in lines[1]


def test_country_restrictions_stay_in_the_location():
    raw = job("Senior Data Scraping") | {"locationRestrictions": ["Poland", "Germany"]}
    assert parse_job(raw).location == "Remote, Poland, Germany"
