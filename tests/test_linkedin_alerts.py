"""Tests for job_radar.sources.linkedin.alerts: parsing alert emails of several years and
importing their jobs once (docs/linkedin-alerts.md).
"""

import datetime as dt
import pathlib
import re

import pytest
import sqlalchemy as sa

import job_radar.models as models
import job_radar.sources.linkedin.alerts as linkedin_alerts

FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "linkedin_alerts"
ALL = sorted(FIXTURES.glob("*.html"))


def fixture(name: str) -> str:
    return (FIXTURES / f"{name}.html").read_text()


def test_fixtures_contain_no_personal_data():
    for path in ALL:
        text = path.read_text()
        assert not re.search(r"midToken|midSig|licdn|@gmail|Yakovenko|Vladimir", text), path.name


@pytest.mark.parametrize("path", ALL, ids=lambda p: p.stem)
def test_every_fixture_parses(path):
    jobs = linkedin_alerts.parse_alert(path.read_text())
    assert len(jobs) == 6
    for job in jobs:
        assert job.title and job.company
        assert job.url == f"https://www.linkedin.com/jobs/view/{job.job_id}/"


def test_parse_card_with_salary_and_easy_apply():
    first = linkedin_alerts.parse_alert(fixture("2026-09-24"))[0]
    assert first.job_id == "4469326693"
    assert first.title == "Back End Developer"
    assert first.company == "asobbi"
    assert first.location == "Germany (Remote)"
    assert first.salary == "€70K-€85K / year"
    assert first.easy_apply is True


def test_parse_card_without_extras():
    flex = next(
        j for j in linkedin_alerts.parse_alert(fixture("2026-09-24")) if j.company == "The Flex"
    )
    assert flex.salary is None
    assert flex.easy_apply is False


def test_old_format_ignores_connection_badge():
    first = linkedin_alerts.parse_alert(fixture("2022-01-06"))[0]
    assert first.title == "Senior Java Software Engineer"
    assert first.company == "EPAM Anywhere"
    assert first.location == "Moscow, Moscow, Russia (Remote)"
    assert first.salary is None


def test_canonical_job_url_drops_tracking():
    url = "https://www.linkedin.com/comm/jobs/view/123/?trackingId=x&otpToken=y&midSig=z"
    assert linkedin_alerts.canonical_job_url(url) == "https://www.linkedin.com/jobs/view/123/"
    assert linkedin_alerts.canonical_job_url("https://example.com/") is None


def add_email(session, msg_id: str, html: str, sender="jobalerts-noreply@linkedin.com"):
    session.add(
        models.EmailMessage(
            gmail_message_id=msg_id,
            gmail_thread_id=msg_id,
            direction=models.EmailDirection.IN,
            sent_at=dt.datetime(2026, 9, 24, tzinfo=dt.UTC),
            from_addr=f"LinkedIn Job Alerts <{sender}>",
            to_addr="user@example.com",
            subject="alert",
            categories=["linkedin_alerts"],
            body_html=html,
        )
    )
    session.commit()


def count(session, model) -> int:
    return session.scalar(sa.select(sa.func.count()).select_from(model))


def test_import_creates_scored_opportunities_once(session):
    add_email(session, "m1", fixture("2026-09-24"))
    stats = linkedin_alerts.import_alerts(session)
    assert (stats.emails, stats.jobs, stats.new) == (1, 6, 6)
    assert linkedin_alerts.import_alerts(session).new == 0
    assert count(session, models.Opportunity) == count(session, models.SourceRecord) == 6

    opportunity = session.scalar(
        sa.select(models.Opportunity).where(models.Opportunity.title == "Sr Python Developer")
    )
    assert opportunity.company.name == "Capitole"
    assert opportunity.location == "Spain (Remote)"
    assert opportunity.easy_apply is True
    assert opportunity.score is not None
    assert opportunity.score_reasons


def test_same_job_in_two_alerts_is_one_opportunity(session):
    html = fixture("2026-09-24")
    add_email(session, "m1", html)
    add_email(session, "m2", html)
    assert linkedin_alerts.import_alerts(session).new == 6
    assert count(session, models.Opportunity) == 6


def test_non_alert_linkedin_mail_is_ignored(session):
    add_email(session, "m1", fixture("2026-09-24"), sender="jobs-noreply@linkedin.com")
    assert linkedin_alerts.import_alerts(session).emails == 0
    assert count(session, models.Opportunity) == 0
