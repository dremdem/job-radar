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


def test_sync_gmail_without_token_explains_auth(tmp_path, monkeypatch):
    monkeypatch.setenv("JOB_RADAR_DATABASE_URL", f"sqlite:///{tmp_path / 'j.db'}")
    monkeypatch.setenv("JOB_RADAR_GMAIL_TOKEN", str(tmp_path / "missing.json"))
    result = runner.invoke(app, ["sync", "gmail"])
    assert result.exit_code == 1
    assert "job-radar gmail auth" in result.output


def test_sync_gmail_rejects_unknown_query(tmp_path, monkeypatch):
    monkeypatch.setenv("JOB_RADAR_DATABASE_URL", f"sqlite:///{tmp_path / 'j.db'}")
    result = runner.invoke(app, ["sync", "gmail", "--query", "nope"])
    assert result.exit_code == 2
    assert "Unknown query" in result.output


def test_import_linkedin(tmp_path, monkeypatch):
    monkeypatch.setenv("JOB_RADAR_DATABASE_URL", f"sqlite:///{tmp_path / 'j.db'}")
    result = runner.invoke(app, ["import", "linkedin"])
    assert result.exit_code == 0, result.output
    assert "0 alert emails" in result.output
