import subprocess
from pathlib import Path

JR = Path(__file__).parent.parent / "jr"


def test_jr_is_valid_bash_and_executable():
    assert JR.exists() and JR.stat().st_mode & 0o111
    subprocess.run(["bash", "-n", str(JR)], check=True)


def test_jr_help_lists_commands():
    out = subprocess.run([str(JR), "help"], capture_output=True, text=True, check=True).stdout
    for command in (
        "start",
        "stop",
        "restart",
        "status",
        "logs",
        "sync",
        "auth",
        "update",
        "check",
        "autostart",
        "unit",
    ):
        assert command in out


def test_jr_rejects_unknown_command():
    result = subprocess.run([str(JR), "nonsense"], capture_output=True, text=True)
    assert result.returncode != 0 and "Unknown command" in result.stderr


def test_jr_unit_describes_the_service():
    out = subprocess.run([str(JR), "unit"], capture_output=True, text=True, check=True).stdout
    root = str(JR.parent.resolve())
    assert "[Service]" in out and "[Install]" in out and "WantedBy=default.target" in out
    assert f"WorkingDirectory={root}" in out
    assert "job-radar serve --host 127.0.0.1 --port 8000" in out
    assert "Restart=on-failure" in out
    assert f"append:{root}/data/server.log" in out
    assert "Environment=PATH=" in out


def test_jr_autostart_needs_on_or_off():
    result = subprocess.run([str(JR), "autostart", "maybe"], capture_output=True, text=True)
    assert result.returncode != 0 and "on|off" in result.stderr
