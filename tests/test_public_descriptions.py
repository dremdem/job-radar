import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import func, select

from job_radar.auto_ignore import Rules, save_rules
from job_radar.dedup import get_or_create_company, upsert_source_record
from job_radar.linkedin.public import (
    PublicSearchClient,
    import_public_descriptions,
    import_public_search,
    parse_job_posting,
)
from job_radar.models import (
    EmailDirection,
    EmailMessage,
    JobSnapshot,
    LinkedInFetch,
    Opportunity,
    OpportunityState,
    SourceType,
)

POSTING = (Path(__file__).parent / "fixtures" / "linkedin_public" / "job_posting.html").read_text()
NOW = datetime(2026, 9, 28, 12, 0, tzinfo=UTC)


def test_fixture_has_no_tracking_or_personal_data():
    assert not re.search(r"trackingId|refId|midToken|Vladimir|@gmail", POSTING)


def test_parse_job_posting():
    job = parse_job_posting(POSTING)
    assert (job.title, job.company, job.location) == (
        "Senior Back-end Engineer",
        "ScorePlay",
        "Paris, Île-de-France, France",
    )
    assert job.description.startswith("ScorePlay is the AI-powered media infrastructure for sports")
    assert parse_job_posting("<html>nothing</html>") is None


class FakeHttp:
    def __init__(self, response=(200, "u", POSTING)):
        self.response, self.urls = response, []

    def __call__(self, url):
        self.urls.append(url)
        return self.response


def client(response=(200, "u", POSTING)):
    http = FakeHttp(response)
    return PublicSearchClient(http_get=http, sleep=lambda _: None), http


def vacancy(session, job_id, score, state=OpportunityState.NEW, title="Backend Engineer"):
    o = Opportunity(
        title=title,
        company=get_or_create_company(session, f"Co {job_id}"),
        state=state,
        score=score,
    )
    session.add(o)
    session.flush()
    upsert_source_record(
        session, source_type=SourceType.LINKEDIN_ALERT, external_id=job_id, url=None, opportunity=o
    )
    session.commit()
    return o


def count(session, model):
    return session.scalar(select(func.count()).select_from(model))


def test_fetches_descriptions_for_to_review_by_score(session):
    vacancy(session, "1", score=5.0)
    vacancy(session, "2", score=9.0)
    vacancy(session, "3", score=7.0, state=OpportunityState.IGNORE)
    c, http = client()
    stats = import_public_descriptions(session, c, now=NOW, max_per_day=5)
    assert (stats.requests, stats.saved) == (2, 2)
    assert [u.rsplit("/", 1)[1] for u in http.urls] == ["2", "1"]  # highest score first, only new
    assert count(session, JobSnapshot) == 2
    assert import_public_descriptions(session, c, now=NOW, max_per_day=5).requests == 0  # done


def test_daily_limit(session):
    for i in range(4):
        vacancy(session, str(i), score=float(i))
    c, _ = client()
    assert import_public_descriptions(session, c, now=NOW, max_per_day=3).requests == 3
    assert (
        import_public_descriptions(session, c, now=NOW + timedelta(hours=1), max_per_day=3).requests
        == 0
    )
    assert (
        import_public_descriptions(session, c, now=NOW + timedelta(days=1), max_per_day=3).requests
        == 1
    )


def test_blocking_stops_descriptions_and_searches_for_the_day(session):
    vacancy(session, "1", score=5.0)
    vacancy(session, "2", score=4.0)
    c, _ = client((429, "u", ""))
    stats = import_public_descriptions(session, c, now=NOW, max_per_day=5)
    assert stats.blocked and stats.requests == 1
    assert session.scalar(select(LinkedInFetch.kind)) == "description"
    session.add(
        EmailMessage(
            gmail_message_id="a1",
            gmail_thread_id="a1",
            direction=EmailDirection.IN,
            sent_at=NOW,
            from_addr="LinkedIn Job Alerts <jobalerts-noreply@linkedin.com>",
            to_addr="u",
            subject="s",
            body_html='<a href="https://www.linkedin.com/comm/jobs/search-results/?keywords=Python">x</a>',
        )
    )
    session.commit()
    ok, _ = client()
    assert import_public_search(session, ok, now=NOW + timedelta(hours=1)).blocked


def test_description_fetches_do_not_use_the_search_budget(session):
    vacancy(session, "1", score=5.0)
    c, _ = client()
    import_public_descriptions(session, c, now=NOW, max_per_day=5)
    searches = session.scalar(
        select(func.count()).select_from(LinkedInFetch).where(LinkedInFetch.kind == "search")
    )
    assert searches == 0


def test_no_python_rule_applies_to_fetched_descriptions(session):
    save_rules(session, Rules(no_python=True))
    o = vacancy(session, "1", score=5.0)
    import_public_descriptions(session, client()[0], now=NOW, max_per_day=5)  # ScorePlay: no Python
    session.refresh(o)
    assert (o.state, o.auto_ignore_reason) == (
        OpportunityState.AUTO_IGNORE,
        "no Python in the description",
    )


def test_page_without_description_is_an_error_not_a_block(session):
    vacancy(session, "1", score=5.0)
    stats = import_public_descriptions(
        session, client((200, "u", "<html>moved</html>"))[0], now=NOW, max_per_day=5
    )
    assert (stats.blocked, stats.errors, stats.saved) == (False, 1, 0)
