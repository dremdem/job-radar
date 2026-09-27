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
    ):
        assert command in out


def test_jr_rejects_unknown_command():
    result = subprocess.run([str(JR), "nonsense"], capture_output=True, text=True)
    assert result.returncode != 0 and "Unknown command" in result.stderr
