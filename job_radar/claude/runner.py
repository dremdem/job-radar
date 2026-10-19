"""Run `claude -p` on the user's subscription: no tools (web search in the chat), no API key,
JSON out."""

import json
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

# Never let Claude Code fall back to paid API billing (docs/claude.md#how-it-runs).
_PAID_ENV = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")


# Worth another attempt: the next run usually works (docs/claude.md, #113).
_TRANSIENT = re.compile(
    r"error_max_structured_output_retries|error_during_execution|overloaded|rate.?limit"
    r"|\b5\d\d\b|timed? ?out|no answer in|temporarily|try again",
    re.IGNORECASE,
)


# The chat may search the web, but never open these: the project does not fetch them (#213).
# Claude Code needs both forms: a bare domain does not cover its subdomains.
WEB_TOOLS = "WebSearch,WebFetch"
BLOCKED_DOMAINS = ("linkedin.com", "lnkd.in", "licdn.com", "hh.ru", "hh.kz", "headhunter.ru")
WEB_DENIED = [f"WebFetch(domain:{p}{d})" for d in BLOCKED_DOMAINS for p in ("", "*.")]


def is_transient(message: str) -> bool:
    return bool(_TRANSIENT.search(message))


class ClaudeError(Exception):
    def __init__(self, message: str, *, transient: bool = False):
        super().__init__(message)
        self.transient = transient


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
        web: bool = False,
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
        web: bool = False,
    ) -> Reply:
        tools = [
            "--tools",
            WEB_TOOLS,
            "--allowedTools",
            WEB_TOOLS,
            "--disallowedTools",
            *WEB_DENIED,
        ]
        args = [
            *self.command,
            "-p",
            "--output-format",
            "json",
            "--model",
            model,
            *(tools if web else ["--tools", ""]),
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
            raise ClaudeError(f"no answer in {self.timeout:.0f} s", transient=True) from e
        try:
            data = json.loads(done.stdout)
        except ValueError as e:
            detail = (done.stderr or done.stdout or "").strip()[-300:]
            message = f"claude exited with {done.returncode}: {detail}"
            raise ClaudeError(message, transient=is_transient(detail)) from e
        if data.get("is_error") or done.returncode != 0:
            message = str(data.get("result") or data.get("subtype") or done.returncode)
            subtype = str(data.get("subtype") or "")
            raise ClaudeError(message, transient=is_transient(f"{subtype} {message}"))
        return Reply(
            text=str(data.get("result") or ""),
            structured=data.get("structured_output"),
            session_id=data.get("session_id"),
            cost_usd=data.get("total_cost_usd"),
        )
