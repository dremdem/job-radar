from datetime import UTC, datetime

from sqlalchemy import func, select
from typer.testing import CliRunner

from job_radar.cli import app
from job_radar.dedup import get_or_create_company
from job_radar.demo import DEMO_OPPORTUNITIES, DEMO_SUFFIX, remove_demo, seed_demo
from job_radar.models import Company, Opportunity


def count(session, model) -> int:
    return session.scalar(select(func.count()).select_from(model))


def test_seed_demo_adds_opportunities_once(session):
    created = seed_demo(session)
    assert created > 0
    assert seed_demo(session) == 0
    assert count(session, Opportunity) == created
    assert all(c.name.endswith(DEMO_SUFFIX) for c in session.scalars(select(Company)))


def test_remove_demo_keeps_real_data(session):
    real = Opportunity(title="Real role", company=get_or_create_company(session, "Real Co"))
    session.add(real)
    seed_demo(session)
    remove_demo(session)
    assert session.scalars(select(Opportunity)).all() == [real]
    assert [c.name for c in session.scalars(select(Company))] == ["Real Co"]


def test_demo_cli(engine, database_url, monkeypatch):
    monkeypatch.setenv("JOB_RADAR_DATABASE_URL", database_url)
    runner = CliRunner()
    result = runner.invoke(app, ["demo"])
    assert result.exit_code == 0, result.output
    assert "Added" in result.output
    result = runner.invoke(app, ["demo", "--remove"])
    assert result.exit_code == 0, result.output
    assert "Removed" in result.output


def test_demo_vacancies_have_descriptions_and_reasons(session):
    from job_radar.description import latest_description

    seed_demo(session)
    for o in session.scalars(select(Opportunity)):
        assert o.location and o.score is not None and o.score_reasons, o.title
        assert latest_description(o), o.title
        assert all(r.url.startswith("https://example.com/") for r in o.source_records)


def test_demo_claude_analyses_are_done_and_their_quotes_check_out(session):
    from job_radar.models import ClaudeAnalysis

    seed_demo(session)
    analyses = session.scalars(select(ClaudeAnalysis)).all()
    assert sorted(a.result["verdict"] for a in analyses) == ["apply", "maybe", "maybe"]
    for a in analyses:
        assert a.state == "done" and a.model == "demo"
        quoted = [a.result["location"], *a.result["strengths"], *a.result["gaps"]]
        quoted += a.result["red_flags"]
        assert all(item["quote_ok"] for item in quoted if item["quote"]), a.opportunity.title


def test_demo_local_ai_finding_shows_in_the_queue(client, session):
    seed_demo(session)
    o = session.scalar(select(Opportunity).where(Opportunity.title.like("%Payments%")))
    panel = client.get(f"/opportunities/{o.id}/panel").text
    assert "AI: only for EU" in panel
    assert "Claude: apply" in client.get("/?tab=all").text


def test_remove_demo_takes_what_was_attached(session):
    from job_radar.manual import add_event
    from job_radar.models import (
        Application,
        ClaudeAnalysis,
        DescriptionAnalysis,
        JobSnapshot,
        NoteKind,
        OpportunityNote,
        SourceRecord,
    )
    from job_radar.notes import add_note

    seed_demo(session)
    demo = session.scalars(select(Opportunity)).first()
    add_note(session, demo, NoteKind.NOTE, comment="hi")
    add_event(session, demo, "applied", datetime.now(UTC))
    session.commit()
    assert remove_demo(session) == len(DEMO_OPPORTUNITIES)
    for model in (
        Opportunity,
        Company,
        SourceRecord,
        JobSnapshot,
        ClaudeAnalysis,
        DescriptionAnalysis,
        OpportunityNote,
        Application,
    ):
        assert count(session, model) == 0, model


def test_demo_gives_the_background_analysis_nothing_to_send(session):
    """Auto-analyze (#264) must not spend the user's Claude on made-up vacancies."""
    from job_radar.claude.analysis import _done_today, enqueue_new_descriptions

    seed_demo(session)
    assert enqueue_new_descriptions(session) == 0
    assert _done_today(session, datetime.now(UTC)) == 0  # nor eat into the daily cap
