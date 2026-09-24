"""Builders for Gmail API payloads and a fake client, shaped like users.threads.get(format=full)."""

import base64
from datetime import datetime


def b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


def message(
    msg_id: str,
    thread_id: str,
    *,
    sent_at: datetime,
    from_addr: str = "Recruiter <jane@acme.example>",
    to_addr: str = "me@example.com",
    subject: str = "Senior Python Engineer",
    labels: tuple[str, ...] = ("INBOX",),
    html: str | None = "<p>Hello <b>there</b></p>",
    text: str | None = "Hello there",
    snippet: str = "Hello there",
) -> dict:
    parts = []
    if text is not None:
        parts.append({"mimeType": "text/plain", "body": {"data": b64(text)}})
    if html is not None:
        parts.append({"mimeType": "text/html", "body": {"data": b64(html)}})
    return {
        "id": msg_id,
        "threadId": thread_id,
        "labelIds": list(labels),
        "snippet": snippet,
        "internalDate": str(int(sent_at.timestamp() * 1000)),
        "payload": {
            "mimeType": "multipart/alternative",
            "headers": [
                {"name": "From", "value": from_addr},
                {"name": "To", "value": to_addr},
                {"name": "Subject", "value": subject},
            ],
            "body": {"size": 0},
            "parts": parts,
        },
    }


def thread(thread_id: str, *messages: dict) -> dict:
    return {"id": thread_id, "messages": list(messages)}


class FakeGmailClient:
    """Returns threads registered for a base query; records the full queries it was asked."""

    def __init__(self, threads_by_query: dict[str, list[dict]]):
        self.threads_by_query = threads_by_query
        self.queries: list[str] = []

    def list_thread_ids(self, query: str):
        self.queries.append(query)
        for base, threads in self.threads_by_query.items():
            if query == base or query.startswith(base + " "):
                yield from (t["id"] for t in threads)

    def get_thread(self, thread_id: str) -> dict:
        for threads in self.threads_by_query.values():
            for t in threads:
                if t["id"] == thread_id:
                    return t
        raise KeyError(thread_id)
