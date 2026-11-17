"""Tests for public LinkedIn job pages: parsing a posting and the opt-in description
run with its daily limit and stops (docs/linkedin-public-search.md).
"""

import datetime as dt
import re

import paths
import sqlalchemy as sa

import job_radar.dedup as dedup
import job_radar.models as models
import job_radar.review.auto_ignore as review_auto_ignore
import job_radar.sources.linkedin.public as linkedin_public

POSTING = (paths.FIXTURES / "linkedin_public" / "job_posting.html").read_text()
NOW = dt.datetime(2026, 9, 28, 12, 0, tzinfo=dt.UTC)


def test_fixture_has_no_tracking_or_personal_data():
    assert not re.search(r"trackingId|refId|midToken|Vladimir|@gmail", POSTING)


def test_parse_job_posting():
    job = linkedin_public.parse_job_posting(POSTING)
    assert (job.title, job.company, job.location) == (
        "Senior Back-end Engineer",
        "ScorePlay",
        "Paris, Île-de-France, France",
    )
    assert job.description.startswith("ScorePlay is the AI-powered media infrastructure for sports")
    assert linkedin_public.parse_job_posting("<html>nothing</html>") is None


class FakeHttp:
    """Answers every request with the same status and page."""

    def __init__(self, response=(200, "u", POSTING)):
        self.response, self.urls = response, []

    def __call__(self, url):
        self.urls.append(url)
        return self.response


def client(response=(200, "u", POSTING)):
    http = FakeHttp(response)
    return linkedin_public.PublicSearchClient(http_get=http, sleep=lambda _: None), http


def vacancy(session, job_id, score, state=models.OpportunityState.NEW, title="Backend Engineer"):
    o = models.Opportunity(
        title=title,
        company=dedup.get_or_create_company(session, f"Co {job_id}"),
        state=state,
        score=score,
    )
    session.add(o)
    session.flush()
    dedup.upsert_source_record(
        session,
        source_type=models.SourceType.LINKEDIN_ALERT,
        external_id=job_id,
        url=None,
        opportunity=o,
    )
    session.commit()
    return o


def count(session, model):
    return session.scalar(sa.select(sa.func.count()).select_from(model))


def test_fetches_descriptions_for_to_review_by_score(session):
    vacancy(session, "1", score=5.0)
    vacancy(session, "2", score=9.0)
    vacancy(session, "3", score=7.0, state=models.OpportunityState.IGNORE)
    c, http = client()
    stats = linkedin_public.import_public_descriptions(session, c, now=NOW, max_per_day=5)
    assert (stats.requests, stats.saved) == (2, 2)
    # highest score first, only new
    assert [u.rsplit("/", 1)[1] for u in http.urls] == ["2", "1"]
    assert count(session, models.JobSnapshot) == 2
    # done
    assert (
        linkedin_public.import_public_descriptions(session, c, now=NOW, max_per_day=5).requests == 0
    )


def test_daily_limit(session):
    for i in range(4):
        vacancy(session, str(i), score=float(i))
    c, _ = client()
    assert (
        linkedin_public.import_public_descriptions(session, c, now=NOW, max_per_day=3).requests == 3
    )
    assert (
        linkedin_public.import_public_descriptions(
            session, c, now=NOW + dt.timedelta(hours=1), max_per_day=3
        ).requests
        == 0
    )
    assert (
        linkedin_public.import_public_descriptions(
            session, c, now=NOW + dt.timedelta(days=1), max_per_day=3
        ).requests
        == 1
    )


def test_blocking_stops_descriptions_and_searches_for_the_day(session):
    vacancy(session, "1", score=5.0)
    vacancy(session, "2", score=4.0)
    c, _ = client((429, "u", ""))
    stats = linkedin_public.import_public_descriptions(session, c, now=NOW, max_per_day=5)
    assert stats.blocked and stats.requests == 1
    assert session.scalar(sa.select(models.LinkedInFetch.kind)) == "description"
    session.add(
        models.EmailMessage(
            gmail_message_id="a1",
            gmail_thread_id="a1",
            direction=models.EmailDirection.IN,
            sent_at=NOW,
            from_addr="LinkedIn Job Alerts <jobalerts-noreply@linkedin.com>",
            to_addr="u",
            subject="s",
            body_html='<a href="https://www.linkedin.com/comm/jobs/search-results/?keywords=Python">x</a>',
        )
    )
    session.commit()
    ok, _ = client()
    assert linkedin_public.import_public_search(
        session, ok, now=NOW + dt.timedelta(hours=1)
    ).blocked


def test_description_fetches_do_not_use_the_search_budget(session):
    vacancy(session, "1", score=5.0)
    c, _ = client()
    linkedin_public.import_public_descriptions(session, c, now=NOW, max_per_day=5)
    searches = session.scalar(
        sa.select(sa.func.count())
        .select_from(models.LinkedInFetch)
        .where(models.LinkedInFetch.kind == "search")
    )
    assert searches == 0


def test_no_python_rule_applies_to_fetched_descriptions(session):
    review_auto_ignore.save_rules(session, review_auto_ignore.Rules(no_python=True))
    o = vacancy(session, "1", score=5.0)
    # ScorePlay: no Python
    linkedin_public.import_public_descriptions(session, client()[0], now=NOW, max_per_day=5)
    session.refresh(o)
    assert (o.state, o.auto_ignore_reason) == (
        models.OpportunityState.AUTO_IGNORE,
        "no Python in the description",
    )


def test_page_without_description_is_an_error_not_a_block(session):
    vacancy(session, "1", score=5.0)
    stats = linkedin_public.import_public_descriptions(
        session, client((200, "u", "<html>moved</html>"))[0], now=NOW, max_per_day=5
    )
    assert (stats.blocked, stats.errors, stats.saved) == (False, 1, 0)
