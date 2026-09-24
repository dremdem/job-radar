from sqlalchemy import func, select
from typer.testing import CliRunner

from job_radar.cli import app
from job_radar.dedup import get_or_create_company
from job_radar.demo import DEMO_SUFFIX, remove_demo, seed_demo
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
