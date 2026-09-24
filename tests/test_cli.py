from typer.testing import CliRunner

from job_radar.cli import app

runner = CliRunner()


def test_db_upgrade_creates_database(tmp_path, monkeypatch):
    db_path = tmp_path / "nested" / "job_radar.db"
    monkeypatch.setenv("JOB_RADAR_DATABASE_URL", f"sqlite:///{db_path}")
    result = runner.invoke(app, ["db", "upgrade"])
    assert result.exit_code == 0, result.output
    assert db_path.exists()


def test_sync_is_not_implemented_yet():
    result = runner.invoke(app, ["sync", "linkedin_alert"])
    assert result.exit_code == 1
    assert "not implemented" in result.output
