"""Tests for job_radar.demo: demo data is added once, removed without touching real
data, and its analyses pass the same checks as real ones (docs/queue.md#demo).
"""

import datetime as dt

import sqlalchemy as sa
import typer.testing as typer_testing

import job_radar.cli as cli
import job_radar.dedup as dedup
import job_radar.demo as jr_demo
import job_radar.models as models


def count(session, model) -> int:
    return session.scalar(sa.select(sa.func.count()).select_from(model))


def test_seed_demo_adds_opportunities_once(session):
    created = jr_demo.seed_demo(session)
    assert created > 0
    assert jr_demo.seed_demo(session) == 0
    assert count(session, models.Opportunity) == created
    assert all(
        c.name.endswith(jr_demo.DEMO_SUFFIX) for c in session.scalars(sa.select(models.Company))
    )


def test_remove_demo_keeps_real_data(session):
    real = models.Opportunity(
        title="Real role", company=dedup.get_or_create_company(session, "Real Co")
    )
    session.add(real)
    jr_demo.seed_demo(session)
    jr_demo.remove_demo(session)
    assert session.scalars(sa.select(models.Opportunity)).all() == [real]
    assert [c.name for c in session.scalars(sa.select(models.Company))] == ["Real Co"]


def test_demo_cli(engine, database_url, monkeypatch):
    monkeypatch.setenv("JOB_RADAR_DATABASE_URL", database_url)
    runner = typer_testing.CliRunner()
    result = runner.invoke(cli.app, ["demo"])
    assert result.exit_code == 0, result.output
    assert "Added" in result.output
    result = runner.invoke(cli.app, ["demo", "--remove"])
    assert result.exit_code == 0, result.output
    assert "Removed" in result.output


def test_demo_vacancies_have_descriptions_and_reasons(session):
    import job_radar.description as description

    jr_demo.seed_demo(session)
    for o in session.scalars(sa.select(models.Opportunity)):
        assert o.location and o.score is not None and o.score_reasons, o.title
        assert description.latest_description(o), o.title
        assert all(r.url.startswith("https://example.com/") for r in o.source_records)


def test_demo_claude_analyses_are_done_and_their_quotes_check_out(session):

    jr_demo.seed_demo(session)
    analyses = session.scalars(sa.select(models.ClaudeAnalysis)).all()
    assert sorted(a.result["verdict"] for a in analyses) == ["apply", "maybe", "maybe"]
    for a in analyses:
        assert a.state == "done" and a.model == "demo"
        quoted = [a.result["location"], *a.result["strengths"], *a.result["gaps"]]
        quoted += a.result["red_flags"]
        assert all(item["quote_ok"] for item in quoted if item["quote"]), a.opportunity.title


def test_demo_local_ai_finding_shows_in_the_queue(client, session):
    jr_demo.seed_demo(session)
    o = session.scalar(
        sa.select(models.Opportunity).where(models.Opportunity.title.like("%Payments%"))
    )
    panel = client.get(f"/opportunities/{o.id}/panel").text
    assert "AI: only for EU" in panel
    assert "Claude: apply" in client.get("/?tab=all").text


def test_remove_demo_takes_what_was_attached(session):
    import job_radar.manual as manual
    import job_radar.notes as notes

    jr_demo.seed_demo(session)
    demo = session.scalars(sa.select(models.Opportunity)).first()
    notes.add_note(session, demo, models.NoteKind.NOTE, comment="hi")
    manual.add_event(session, demo, "applied", dt.datetime.now(dt.UTC))
    session.commit()
    assert jr_demo.remove_demo(session) == len(jr_demo.DEMO_OPPORTUNITIES)
    for model in (
        models.Opportunity,
        models.Company,
        models.SourceRecord,
        models.JobSnapshot,
        models.ClaudeAnalysis,
        models.DescriptionAnalysis,
        models.OpportunityNote,
        models.Application,
    ):
        assert count(session, model) == 0, model


def test_demo_gives_the_background_analysis_nothing_to_send(session):
    """Auto-analyze (#264) must not spend the user's Claude on made-up vacancies."""
    import job_radar.claude.analysis as claude_analysis

    jr_demo.seed_demo(session)
    assert claude_analysis.enqueue_new_descriptions(session) == 0
    # nor eat into the daily cap
    assert claude_analysis._done_today(session, dt.datetime.now(dt.UTC)) == 0
