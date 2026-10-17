import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import func, select

from job_radar.linkedin.alerts import import_alerts
from job_radar.linkedin.public import (
    Blocked,
    PublicSearchClient,
    Search,
    import_public_search,
    parse_search_page,
    search_from_alert,
)
from job_radar.models import (
    EmailDirection,
    EmailMessage,
    LinkedInFetch,
    Opportunity,
    SourceRecord,
    SourceType,
)
from job_radar.scoring import score_job

FIXTURE = (Path(__file__).parent / "fixtures" / "linkedin_public" / "search_page.html").read_text()
NOW = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)
ALERT_LINK = (
    "https://www.linkedin.com/comm/jobs/search-results/?keywords=Python%20Developer%20Remote"
    "&f_TPR=a{ts}-&geoId=100506914&origin=SEMANTIC_SEARCH_JOB_ALERT_EMAIL"
    "&otpToken=SECRET&midToken=SECRET&trk=x"
)


def alert_html(ts: int, job_id: str = "4470085725") -> str:
    return (
        f'<a href="{ALERT_LINK.format(ts=ts)}">Your job alert for Python Developer Remote</a>'
        f'<table><tr><td><a href="https://www.linkedin.com/comm/jobs/view/{job_id}/?x=1">'
        "Food Traceability Python Developer (Backend)</a>"
        "<p>Mosano · Porto, Portugal (Remote)</p></td></tr></table>"
    )


def test_fixture_has_no_tracking_or_personal_data():
    assert not re.search(r"trackingId|refId|midToken|Vladimir|@gmail", FIXTURE)


def test_parse_search_page():
    jobs = parse_search_page(FIXTURE)
    assert len(jobs) == 10
    first = jobs[0]
    assert first.job_id == "4470085725"
    assert first.title == "Food Traceability Python Developer (Backend)"
    assert first.company == "Mosano"
    assert first.location == "Porto, Porto, Portugal"
    assert first.easy_apply is None
    assert first.url == "https://www.linkedin.com/jobs/view/4470085725/"


def test_search_from_alert_takes_only_search_parameters():
    ts = int((NOW - timedelta(hours=5)).timestamp())
    search = search_from_alert(alert_html(ts), now=NOW)
    assert search == Search(
        keywords="Python Developer Remote", geo_id="100506914", time_range="r18000"
    )
    url = search.url(start=10)
    assert "SECRET" not in url and "trk" not in url
    assert url.startswith("https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search?")
    assert "start=10" in url and "f_TPR=r18000" in url


@pytest.mark.parametrize(("hours", "expected"), [(0.1, "r3600"), (24 * 30, "r604800")])
def test_time_range_is_clamped(hours, expected):
    ts = int((NOW - timedelta(hours=hours)).timestamp())
    assert search_from_alert(alert_html(ts), now=NOW).time_range == expected


def test_alert_without_search_link():
    assert search_from_alert("<p>no links</p>", now=NOW) is None


class FakeHttp:
    """Returns (status, final_url, body) per start offset."""

    def __init__(self, pages):
        self.pages = pages
        self.urls = []

    def __call__(self, url):
        self.urls.append(url)
        start = int(re.search(r"start=(\d+)", url).group(1))
        return self.pages.get(start, (200, url, ""))


def page_with_ids(offset: int) -> str:
    # Same cards with different job ids, to make several distinct pages.
    return re.sub(r"(\d{10})", lambda m: str(int(m.group(1)) + offset), FIXTURE)


def client(pages, sleeps=None):
    return PublicSearchClient(
        http_get=FakeHttp(pages),
        sleep=(sleeps.append if sleeps is not None else lambda _: None),
        pause=(3, 3),
    )


@pytest.mark.parametrize(
    "response",
    [
        (429, "u", ""),
        (999, "u", ""),
        (200, "https://www.linkedin.com/authwall?trk=x", "<html>sign in</html>"),
        (200, "u", "<html>Please complete this security challenge</html>"),
    ],
)
def test_client_detects_blocking(response):
    with pytest.raises(Blocked):
        client({0: response}).fetch(Search("k", "1", "r3600"), start=0)


def test_client_pauses_between_requests():
    sleeps = []
    c = client({0: (200, "u", FIXTURE), 10: (200, "u", FIXTURE)}, sleeps)
    c.fetch(Search("k", "1", "r3600"), start=0)
    c.fetch(Search("k", "1", "r3600"), start=10)
    assert sleeps == [3]


def test_pause_is_random_within_the_range():
    sleeps, ranges = [], []

    def rand(low, high):
        ranges.append((low, high))
        return 17.5

    c = PublicSearchClient(
        http_get=FakeHttp({0: (200, "u", FIXTURE), 10: (200, "u", FIXTURE)}),
        sleep=sleeps.append,
        pause=(10, 40),
        rand=rand,
    )
    c.fetch(Search("k", "1", "r3600"), start=0)
    c.fetch(Search("k", "1", "r3600"), start=10)
    assert (ranges, sleeps) == ([(10, 40)], [17.5])


def add_alert(session, msg_id, sent_at, html):
    email = EmailMessage(
        gmail_message_id=msg_id,
        gmail_thread_id=msg_id,
        direction=EmailDirection.IN,
        sent_at=sent_at,
        from_addr="LinkedIn Job Alerts <jobalerts-noreply@linkedin.com>",
        to_addr="user@example.com",
        subject="alert",
        categories=["linkedin_alerts"],
        body_html=html,
    )
    session.add(email)
    session.commit()
    return email


PAGES = {0: (200, "u", FIXTURE), 10: (200, "u", page_with_ids(1)), 20: (200, "u", page_with_ids(2))}


def count(session, model) -> int:
    return session.scalar(select(func.count()).select_from(model))


def test_import_takes_first_25_once_per_alert(session):
    add_alert(
        session,
        "a1",
        NOW - timedelta(hours=2),
        alert_html(int((NOW - timedelta(hours=26)).timestamp())),
    )
    c = client(dict(PAGES))
    stats = import_public_search(session, c, now=NOW, max_searches=5, max_jobs=25)
    assert (stats.searches, stats.requests, stats.new) == (1, 3, 25)
    assert count(session, Opportunity) == 25
    assert import_public_search(session, c, now=NOW, max_searches=5, max_jobs=25).searches == 0
    assert count(session, LinkedInFetch) == 3


def test_search_jobs_have_no_remote_penalty_and_unknown_easy_apply(session):
    add_alert(session, "a1", NOW - timedelta(hours=2), alert_html(int(NOW.timestamp()) - 7200))
    import_public_search(session, client(dict(PAGES)), now=NOW, max_searches=5, max_jobs=25)
    o = session.scalar(select(Opportunity).where(Opportunity.title.like("Food Traceability%")))
    assert o.easy_apply is None
    assert all(r["text"] != "No remote marker in location" for r in o.score_reasons)


def test_job_seen_in_alert_and_search_is_one_opportunity(session):
    add_alert(session, "a1", NOW - timedelta(hours=2), alert_html(int(NOW.timestamp()) - 7200))
    import_alerts(session)
    import_public_search(session, client(dict(PAGES)), now=NOW, max_searches=5, max_jobs=25)
    records = session.scalars(
        select(SourceRecord).where(SourceRecord.external_id == "4470085725")
    ).all()
    assert [r.source_type for r in records] == [SourceType.LINKEDIN_ALERT]
    assert count(session, Opportunity) == 25


def test_daily_search_limit(session):
    for i in range(3):
        add_alert(
            session, f"a{i}", NOW - timedelta(hours=i + 1), alert_html(int(NOW.timestamp()) - 7200)
        )
    stats = import_public_search(session, client(dict(PAGES)), now=NOW, max_searches=2, max_jobs=25)
    assert stats.searches == 2
    later = import_public_search(
        session, client(dict(PAGES)), now=NOW + timedelta(hours=1), max_searches=2, max_jobs=25
    )
    assert later.searches == 0


def test_old_alerts_are_not_searched(session):
    add_alert(session, "old", NOW - timedelta(days=4), alert_html(int(NOW.timestamp()) - 7200))
    assert (
        import_public_search(
            session, client(dict(PAGES)), now=NOW, max_searches=5, max_jobs=25
        ).searches
        == 0
    )


def test_blocked_stops_until_next_day(session):
    add_alert(session, "a1", NOW - timedelta(hours=1), alert_html(int(NOW.timestamp()) - 7200))
    add_alert(session, "a2", NOW - timedelta(hours=2), alert_html(int(NOW.timestamp()) - 7200))
    stats = import_public_search(
        session, client({0: (429, "u", "")}), now=NOW, max_searches=5, max_jobs=25
    )
    assert stats.blocked and stats.requests == 1
    assert session.scalar(select(LinkedInFetch.status)) == "blocked"
    again = import_public_search(
        session, client(dict(PAGES)), now=NOW + timedelta(hours=2), max_searches=5, max_jobs=25
    )
    assert again.blocked and again.requests == 0
    tomorrow = import_public_search(
        session, client(dict(PAGES)), now=NOW + timedelta(days=1), max_searches=5, max_jobs=25
    )
    assert tomorrow.requests > 0


def test_score_without_known_work_format():
    known = score_job("Python Developer", "Porto, Portugal")
    unknown = score_job("Python Developer", "Porto, Portugal", remote_known=False)
    assert "No remote marker in location" in [r.text for r in known.reasons]
    texts = [r.text for r in unknown.reasons]
    assert "No remote marker in location" not in texts
    assert "Work format not shown for Porto, Portugal: check remote and Thailand" in texts
