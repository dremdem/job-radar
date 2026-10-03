"""Run `claude -p` on the user's subscription: no tools, no API key, JSON out."""

import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

# Never let Claude Code fall back to paid API billing (docs/claude.md#how-it-runs).
_PAID_ENV = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")


class ClaudeError(Exception):
    pass


@dataclass(frozen=True)
class Reply:
    text: str
    structured: dict[str, Any] | None
    session_id: str | None
    cost_usd: float | None


class Runner(Protocol):
    def run(
        self,
        prompt: str,
        *,
        system: str,
        model: str,
        schema: dict[str, Any] | None = None,
        session_id: str | None = None,
        resume: str | None = None,
    ) -> Reply: ...


class ClaudeCli:
    def __init__(self, command: str, workdir: Path, timeout: float = 600):
        self.command = command.split()
        self.workdir = workdir
        self.timeout = timeout

    def run(
        self,
        prompt: str,
        *,
        system: str,
        model: str,
        schema: dict[str, Any] | None = None,
        session_id: str | None = None,
        resume: str | None = None,
    ) -> Reply:
        args = [
            *self.command,
            "-p",
            "--output-format",
            "json",
            "--model",
            model,
            "--tools",
            "",
            "--strict-mcp-config",
            "--setting-sources",
            "",
            "--system-prompt",
            system,
        ]
        if schema is not None:
            args += ["--json-schema", json.dumps(schema)]
        if session_id:
            args += ["--session-id", session_id]
        if resume:
            args += ["--resume", resume]
        env = {k: v for k, v in os.environ.items() if k not in _PAID_ENV}
        self.workdir.mkdir(parents=True, exist_ok=True)
        try:
            done = subprocess.run(
                args,
                input=prompt,
                capture_output=True,
                text=True,
                cwd=self.workdir,
                env=env,
                timeout=self.timeout,
            )
        except FileNotFoundError as e:
            raise ClaudeError(
                f"`{self.command[0]}` not found: install and log in to Claude Code"
            ) from e
        except subprocess.TimeoutExpired as e:
            raise ClaudeError(f"no answer in {self.timeout:.0f} s") from e
        try:
            data = json.loads(done.stdout)
        except ValueError as e:
            detail = (done.stderr or done.stdout or "").strip()[-300:]
            raise ClaudeError(f"claude exited with {done.returncode}: {detail}") from e
        if data.get("is_error") or done.returncode != 0:
            raise ClaudeError(str(data.get("result") or data.get("subtype") or done.returncode))
        return Reply(
            text=str(data.get("result") or ""),
            structured=data.get("structured_output"),
            session_id=data.get("session_id"),
            cost_usd=data.get("total_cost_usd"),
        )
