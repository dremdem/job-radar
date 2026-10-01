"""Read posts through the existing telegram-mcp server (MCP over stdio). Read-only by design:
only `read_messages` is ever called. See docs/telegram.md."""

import json
import re
import shlex
from datetime import datetime
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from .sync import ReadPlan

READ_TOOL = "read_messages"
ALLOWED_TOOLS = frozenset({READ_TOOL})
PAGE = 50
MAX_PER_READ = 500
# telegram-mcp wraps untrusted text: "[UNTRUSTED MESSAGE - ...]\n<text>\n[END ...]".
_FENCE = re.compile(r"^\[[A-Z ]+- [A-Z ,]+\]\n|\n\[END [A-Z ]+\]$")


class TelegramReadError(RuntimeError):
    pass


def unfence(value: Any) -> Any:
    return _FENCE.sub("", value) if isinstance(value, str) else value


async def _call(session: ClientSession, tool: str, args: dict[str, Any]) -> str:
    if tool not in ALLOWED_TOOLS:  # never send, mark read, join, ...
        raise TelegramReadError(f"tool {tool!r} is not allowed")
    result = await session.call_tool(tool, args)
    return result.content[0].text if result.content else ""


async def _read(session: ClientSession, plan: ReadPlan) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    offset: str | None = None
    limit = PAGE
    while len(rows) < MAX_PER_READ:
        args: dict[str, Any] = {"chat_id": plan.source.chat, "limit": limit}
        if offset:
            args["offset_date"] = offset
        if plan.topic_id is not None:
            args["topic_id"] = plan.topic_id
        text = await _call(session, READ_TOOL, args)
        if not text.lstrip().startswith("["):
            if limit > 5:  # a page with something odd in it: retry smaller
                limit //= 2
                continue
            raise TelegramReadError(f"{plan.source.name}: {text[:200]}")
        limit = PAGE
        batch = [{k: unfence(v) for k, v in m.items()} for m in json.loads(text)]
        batch = [m for m in batch if not rows or m["id"] < rows[-1]["id"]]
        if not batch:
            break
        rows += batch
        oldest = batch[-1]
        if plan.after_id is not None and oldest["id"] <= plan.after_id:
            break
        if plan.since is not None and datetime.fromisoformat(oldest["date"]) < plan.since:
            break
        offset = oldest["date"]
    return [
        m
        for m in rows
        if (plan.after_id is None or m["id"] > plan.after_id)
        and (plan.since is None or datetime.fromisoformat(m["date"]) >= plan.since)
    ]


async def fetch_posts(
    command: str, plans: list[ReadPlan]
) -> list[tuple[ReadPlan, list[dict[str, Any]] | Exception]]:
    """New posts for every plan, in one telegram-mcp session; a failing channel does not stop
    the others."""
    parts = shlex.split(command)
    params = StdioServerParameters(command=parts[0], args=parts[1:])
    results: list[tuple[ReadPlan, list[dict[str, Any]] | Exception]] = []
    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        for plan in plans:
            try:
                results.append((plan, await _read(session, plan)))
            except Exception as e:  # reported per channel by the caller
                results.append((plan, e))
    return results
