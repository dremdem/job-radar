"""Tests for the ``job-radar`` command line: database upgrade, sync and its error
messages, import commands (docs/usage.md).
"""

import typer.testing as typer_testing

import job_radar.cli as cli

runner = typer_testing.CliRunner()


def test_db_upgrade_creates_database(tmp_path, monkeypatch):
    db_path = tmp_path / "nested" / "job_radar.db"
    monkeypatch.setenv("JOB_RADAR_DATABASE_URL", f"sqlite:///{db_path}")
    result = runner.invoke(cli.app, ["db", "upgrade"])
    assert result.exit_code == 0, result.output
    assert db_path.exists()


def test_sync_is_not_implemented_yet():
    result = runner.invoke(cli.app, ["sync", "linkedin_alert"])
    assert result.exit_code == 1
    assert "not implemented" in result.output


def test_sync_gmail_without_token_explains_auth(tmp_path, monkeypatch):
    monkeypatch.setenv("JOB_RADAR_DATABASE_URL", f"sqlite:///{tmp_path / 'j.db'}")
    monkeypatch.setenv("JOB_RADAR_GMAIL_TOKEN", str(tmp_path / "missing.json"))
    # set up half way: the client file, no token yet
    client = tmp_path / "client.json"
    client.write_text("{}")
    monkeypatch.setenv("JOB_RADAR_GMAIL_CLIENT_SECRET", str(client))
    monkeypatch.setenv("JOB_RADAR_HIMALAYAS", "false")
    # no network in tests
    monkeypatch.setenv("JOB_RADAR_FDETIMES", "false")
    monkeypatch.setenv("JOB_RADAR_ATS", "false")
    monkeypatch.setenv("JOB_RADAR_AI_ANALYSIS", "false")
    result = runner.invoke(cli.app, ["sync", "gmail"])
    assert result.exit_code == 1
    assert "job-radar gmail auth" in result.output
    # The other steps still ran.
    assert "rejections:" in result.output and "1 step failed: gmail" in result.output


def test_sync_on_a_fresh_clone_succeeds_without_gmail(tmp_path, monkeypatch):
    # Neither the OAuth client file nor the token: Gmail is off, not a failure (#8).
    monkeypatch.setenv("JOB_RADAR_DATABASE_URL", f"sqlite:///{tmp_path / 'j.db'}")
    monkeypatch.setenv("JOB_RADAR_GMAIL_TOKEN", str(tmp_path / "missing.json"))
    monkeypatch.setenv("JOB_RADAR_GMAIL_CLIENT_SECRET", str(tmp_path / "missing-client.json"))
    for source in (
        "HIMALAYAS",
        "HACKERNEWS",
        "REMOTEOK",
        "JOBICY",
        "FDETIMES",
        "BOARDS",
        "ATS",
        "AI_ANALYSIS",
    ):
        # no network in tests
        monkeypatch.setenv(f"JOB_RADAR_{source}", "false")
    result = runner.invoke(cli.app, ["sync", "gmail"])
    assert result.exit_code == 0, result.output
    assert "step failed" not in result.output


def test_sync_gmail_rejects_unknown_query(tmp_path, monkeypatch):
    monkeypatch.setenv("JOB_RADAR_DATABASE_URL", f"sqlite:///{tmp_path / 'j.db'}")
    result = runner.invoke(cli.app, ["sync", "gmail", "--query", "nope"])
    assert result.exit_code == 2
    assert "Unknown query" in result.output


def test_import_linkedin(tmp_path, monkeypatch):
    monkeypatch.setenv("JOB_RADAR_DATABASE_URL", f"sqlite:///{tmp_path / 'j.db'}")
    result = runner.invoke(cli.app, ["import", "linkedin"])
    assert result.exit_code == 0, result.output
    assert "0 alert emails" in result.output


def test_import_rejections_without_gmail_reports_pending(tmp_path, monkeypatch):
    import datetime as dt

    import job_radar.db as db
    import job_radar.models as models

    url = f"sqlite:///{tmp_path / 'j.db'}"
    monkeypatch.setenv("JOB_RADAR_DATABASE_URL", url)
    monkeypatch.setenv("JOB_RADAR_GMAIL_TOKEN", str(tmp_path / "missing.json"))
    db.upgrade_database(url)
    with db.make_session_factory(db.make_engine(url))() as session:
        session.add(
            models.EmailMessage(
                gmail_message_id="old1",
                gmail_thread_id="old1",
                direction=models.EmailDirection.IN,
                sent_at=dt.datetime(2026, 1, 1, tzinfo=dt.UTC),
                from_addr="LinkedIn <jobs-noreply@linkedin.com>",
                to_addr="user@example.com",
                subject="Your application to Python Developer at Acme",
            )
        )
        session.commit()
    result = runner.invoke(cli.app, ["import", "rejections"])
    assert result.exit_code == 0, result.output
    assert "1 without body" in result.output
    assert "job-radar gmail auth" in result.output


def test_import_applications(tmp_path, monkeypatch):
    monkeypatch.setenv("JOB_RADAR_DATABASE_URL", f"sqlite:///{tmp_path / 'j.db'}")
    monkeypatch.setenv("JOB_RADAR_GMAIL_TOKEN", str(tmp_path / "missing.json"))
    result = runner.invoke(cli.app, ["import", "applications"])
    assert result.exit_code == 0, result.output
    assert "applications: 0 found, 0 new" in result.output


def test_import_contacts(tmp_path, monkeypatch):
    monkeypatch.setenv("JOB_RADAR_DATABASE_URL", f"sqlite:///{tmp_path / 'j.db'}")
    result = runner.invoke(cli.app, ["import", "contacts"])
    assert result.exit_code == 0, result.output
    assert "contacts: 0 linked" in result.output


def test_import_linkedin_search_is_off_by_default(tmp_path, monkeypatch):
    monkeypatch.setenv("JOB_RADAR_DATABASE_URL", f"sqlite:///{tmp_path / 'j.db'}")
    monkeypatch.delenv("JOB_RADAR_LINKEDIN_PUBLIC_SEARCH", raising=False)
    result = runner.invoke(cli.app, ["import", "linkedin-search"])
    assert result.exit_code == 0, result.output
    assert "disabled" in result.output
    assert "JOB_RADAR_LINKEDIN_PUBLIC_SEARCH=true" in result.output


def test_import_linkedin_search_enabled_without_alerts(tmp_path, monkeypatch):
    monkeypatch.setenv("JOB_RADAR_DATABASE_URL", f"sqlite:///{tmp_path / 'j.db'}")
    monkeypatch.setenv("JOB_RADAR_LINKEDIN_PUBLIC_SEARCH", "true")
    result = runner.invoke(cli.app, ["import", "linkedin-search"])
    assert result.exit_code == 0, result.output
    assert "linkedin-search: 0 searches, 0 requests" in result.output


def test_import_descriptions_is_off_without_linkedin_and_fdetimes(tmp_path, monkeypatch):
    monkeypatch.setenv("JOB_RADAR_DATABASE_URL", f"sqlite:///{tmp_path / 'j.db'}")
    monkeypatch.delenv("JOB_RADAR_LINKEDIN_PUBLIC_SEARCH", raising=False)
    monkeypatch.setenv("JOB_RADAR_FDETIMES", "false")
    result = runner.invoke(cli.app, ["import", "descriptions"])
    assert result.exit_code == 0 and "disabled" in result.output


def test_import_descriptions_reads_fdetimes_by_default(tmp_path, monkeypatch):
    # FDE Times is on by default (docs/fdetimes.md); an empty database asks for nothing.
    monkeypatch.setenv("JOB_RADAR_DATABASE_URL", f"sqlite:///{tmp_path / 'j.db'}")
    monkeypatch.delenv("JOB_RADAR_LINKEDIN_PUBLIC_SEARCH", raising=False)
    monkeypatch.delenv("JOB_RADAR_FDETIMES", raising=False)
    result = runner.invoke(cli.app, ["import", "descriptions"])
    assert result.exit_code == 0, result.output
    assert "descriptions: 0 requests, 0 saved" in result.output


def test_sync_telegram_needs_configuration(tmp_path, monkeypatch):
    monkeypatch.setenv("JOB_RADAR_DATABASE_URL", f"sqlite:///{tmp_path / 'j.db'}")
    monkeypatch.delenv("JOB_RADAR_TELEGRAM_MCP_COMMAND", raising=False)
    result = runner.invoke(cli.app, ["sync", "telegram"])
    assert result.exit_code == 0 and "telegram: not configured" in result.output


def test_sync_himalayas_can_be_disabled(tmp_path, monkeypatch):
    monkeypatch.setenv("JOB_RADAR_DATABASE_URL", f"sqlite:///{tmp_path / 'j.db'}")
    monkeypatch.setenv("JOB_RADAR_HIMALAYAS", "false")
    result = runner.invoke(cli.app, ["sync", "himalayas"])
    assert result.exit_code == 0 and "himalayas: disabled" in result.output


def test_ats_commands(tmp_path, monkeypatch):
    monkeypatch.setenv("JOB_RADAR_DATABASE_URL", f"sqlite:///{tmp_path / 'j.db'}")
    result = runner.invoke(cli.app, ["ats", "confirm", "Globex", "lever", "globex"])
    assert result.exit_code == 1 and "no company 'Globex'" in result.output
    for command in (["ats", "boards"], ["ats", "candidates"], ["ats", "run"]):
        result = runner.invoke(cli.app, command)
        assert result.exit_code == 0, result.output
    assert "ats: 0 vacancies checked" in result.output
