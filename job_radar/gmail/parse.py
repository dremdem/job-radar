"""Turn a Gmail API message into plain fields.

``parse_message`` takes a message from ``users.messages`` or ``threads.get`` in the full
format and returns a ``ParsedMessage``: ids, labels, direction (received or sent),
date, addresses, subject, snippet and the HTML and text bodies. See docs/gmail.md.

Usage:
    import job_radar.gmail.parse as gmail_parse
"""

import base64
import dataclasses
import datetime as dt

import job_radar.models as models


@dataclasses.dataclass(frozen=True)
class ParsedMessage:
    """The fields of one Gmail message that the app stores."""

    gmail_message_id: str
    gmail_thread_id: str
    label_ids: list[str]
    direction: models.EmailDirection
    sent_at: dt.datetime
    from_addr: str
    to_addr: str
    subject: str
    snippet: str
    body_html: str | None
    body_text: str | None


def _decode(data: str) -> str:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode("utf-8", "replace")


def _find_body(part: dict, mime_type: str) -> str | None:
    if (
        part.get("mimeType") == mime_type
        and not part.get("filename")
        and "data" in part.get("body", {})
    ):
        return _decode(part["body"]["data"])
    for child in part.get("parts", []):
        if (found := _find_body(child, mime_type)) is not None:
            return found
    return None


def parse_message(raw: dict) -> ParsedMessage:
    """Turn a users.messages / threads.get(format=full) message into plain fields."""
    payload = raw.get("payload", {})
    headers = {h["name"].lower(): h["value"] for h in payload.get("headers", [])}
    labels = raw.get("labelIds", [])
    return ParsedMessage(
        gmail_message_id=raw["id"],
        gmail_thread_id=raw["threadId"],
        label_ids=labels,
        direction=models.EmailDirection.OUT if "SENT" in labels else models.EmailDirection.IN,
        sent_at=dt.datetime.fromtimestamp(int(raw["internalDate"]) / 1000, dt.UTC),
        from_addr=headers.get("from", ""),
        to_addr=headers.get("to", ""),
        subject=headers.get("subject", ""),
        snippet=raw.get("snippet", ""),
        body_html=_find_body(payload, "text/html"),
        body_text=_find_body(payload, "text/plain"),
    )
