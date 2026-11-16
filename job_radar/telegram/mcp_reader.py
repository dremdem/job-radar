"""Read Telegram posts through the existing telegram-mcp server.

The server is started as a subprocess and spoken to over MCP stdio. Read-only by design:
only ``read_messages`` is ever called. ``fetch_posts`` runs the reads planned by
``telegram.sync``, and ``unfence`` strips the code fences around a tool answer. See
docs/telegram.md.

Usage:
    import job_radar.telegram.mcp_reader as telegram_mcp_reader
"""

import datetime as dt
import json
import re
import shlex
import typing as t

import mcp
import mcp.client.stdio as client_stdio

import job_radar.telegram.sync as telegram_sync

READ_TOOL = "read_messages"
ALLOWED_TOOLS = frozenset({READ_TOOL})
PAGE = 50
MAX_PER_READ = 500
# telegram-mcp wraps untrusted text: "[UNTRUSTED MESSAGE - ...]\n<text>\n[END ...]".
_FENCE = re.compile(r"^\[[A-Z ]+- [A-Z ,]+\]\n|\n\[END [A-Z ]+\]$")


class TelegramReadError(RuntimeError):
    """telegram-mcp could not be started or answered with an error."""

    pass


def unfence(value: t.Any) -> t.Any:
    return _FENCE.sub("", value) if isinstance(value, str) else value


async def _call(session: mcp.ClientSession, tool: str, args: dict[str, t.Any]) -> str:
    # never send, mark read, join, ...
    if tool not in ALLOWED_TOOLS:
        raise TelegramReadError(f"tool {tool!r} is not allowed")
    result = await session.call_tool(tool, args)
    return result.content[0].text if result.content else ""


async def _read(session: mcp.ClientSession, plan: telegram_sync.ReadPlan) -> list[dict[str, t.Any]]:
    rows: list[dict[str, t.Any]] = []
    offset: str | None = None
    limit = PAGE
    while len(rows) < MAX_PER_READ:
        args: dict[str, t.Any] = {"chat_id": plan.source.chat, "limit": limit}
        if offset:
            args["offset_date"] = offset
        if plan.topic_id is not None:
            args["topic_id"] = plan.topic_id
        text = await _call(session, READ_TOOL, args)
        if not text.lstrip().startswith("["):
            # a page with something odd in it: retry smaller
            if limit > 5:
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
        if plan.since is not None and dt.datetime.fromisoformat(oldest["date"]) < plan.since:
            break
        offset = oldest["date"]
    return [
        m
        for m in rows
        if (plan.after_id is None or m["id"] > plan.after_id)
        and (plan.since is None or dt.datetime.fromisoformat(m["date"]) >= plan.since)
    ]


async def fetch_posts(
    command: str, plans: list[telegram_sync.ReadPlan]
) -> list[tuple[telegram_sync.ReadPlan, list[dict[str, t.Any]] | Exception]]:
    """New posts for every plan, in one telegram-mcp session; a failing channel does not stop
    the others."""
    parts = shlex.split(command)
    params = mcp.StdioServerParameters(command=parts[0], args=parts[1:])
    results: list[tuple[telegram_sync.ReadPlan, list[dict[str, t.Any]] | Exception]] = []
    async with (
        client_stdio.stdio_client(params) as (read, write),
        mcp.ClientSession(read, write) as session,
    ):
        await session.initialize()
        for plan in plans:
            try:
                results.append((plan, await _read(session, plan)))
            except Exception as e:
                # reported per channel by the caller
                results.append((plan, e))
    return results
