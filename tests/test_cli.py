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


def test_import_rejections_without_gmail_reports_pending(tmp_path, monkeypatch):
    from datetime import UTC, datetime

    from job_radar.db import make_engine, make_session_factory, upgrade_database
    from job_radar.models import EmailDirection, EmailMessage

    url = f"sqlite:///{tmp_path / 'j.db'}"
    monkeypatch.setenv("JOB_RADAR_DATABASE_URL", url)
    monkeypatch.setenv("JOB_RADAR_GMAIL_TOKEN", str(tmp_path / "missing.json"))
    upgrade_database(url)
    with make_session_factory(make_engine(url))() as session:
        session.add(
            EmailMessage(
                gmail_message_id="old1",
                gmail_thread_id="old1",
                direction=EmailDirection.IN,
                sent_at=datetime(2026, 1, 1, tzinfo=UTC),
                from_addr="LinkedIn <jobs-noreply@linkedin.com>",
                to_addr="user@example.com",
                subject="Your application to Python Developer at Acme",
            )
        )
        session.commit()
    result = runner.invoke(app, ["import", "rejections"])
    assert result.exit_code == 0, result.output
    assert "1 without body" in result.output
    assert "job-radar gmail auth" in result.output


def test_import_applications(tmp_path, monkeypatch):
    monkeypatch.setenv("JOB_RADAR_DATABASE_URL", f"sqlite:///{tmp_path / 'j.db'}")
    monkeypatch.setenv("JOB_RADAR_GMAIL_TOKEN", str(tmp_path / "missing.json"))
    result = runner.invoke(app, ["import", "applications"])
    assert result.exit_code == 0, result.output
    assert "applications: 0 found, 0 new" in result.output


def test_import_contacts(tmp_path, monkeypatch):
    monkeypatch.setenv("JOB_RADAR_DATABASE_URL", f"sqlite:///{tmp_path / 'j.db'}")
    result = runner.invoke(app, ["import", "contacts"])
    assert result.exit_code == 0, result.output
    assert "contacts: 0 linked" in result.output
