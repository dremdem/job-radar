"""A thin read-only client of the Gmail API.

``GmailClient`` is the interface the sync uses (list thread ids for a query, get a
thread); ``ApiGmailClient`` implements it over the real API, and tests pass fakes.
``RateLimitedExecutor`` paces requests and waits and retries on rate-limit errors.
``body_fetcher`` reads a message body for callers that must not store it. See
docs/gmail.md.

Usage:
    import job_radar.gmail.client as gmail_client
"""

import collections.abc as cabc
import json
import time
import typing as t

import google.oauth2.credentials as oauth2_credentials
import googleapiclient.discovery as googleapiclient_discovery
import googleapiclient.errors as googleapiclient_errors

import job_radar.mail as mail

# Gmail enforces a per-user, per-minute query-cost quota. Pace requests evenly and, when the
# quota is still hit, wait for the next minute instead of backing off at random.
MIN_INTERVAL = 0.5
RATE_LIMIT_WAIT = 60.0
MAX_RATE_LIMIT_RETRIES = 5
# googleapiclient's own retries, for 5xx and connection errors
TRANSIENT_RETRIES = 2


class GmailClient(t.Protocol):
    """What the sync needs from Gmail: thread ids for a query and a thread."""

    def list_thread_ids(self, query: str) -> cabc.Iterator[str]: ...
    def get_thread(self, thread_id: str) -> dict: ...


def _is_rate_limit(error: googleapiclient_errors.HttpError) -> bool:
    if error.resp.status == 429:
        return True
    if error.resp.status != 403:
        return False
    try:
        errors = json.loads(error.content)["error"]["errors"]
    except (ValueError, KeyError, TypeError):
        return False
    return any(e.get("reason") in ("rateLimitExceeded", "userRateLimitExceeded") for e in errors)


class RateLimitedExecutor:
    """Runs API requests with a minimum interval, retrying after rate limits."""

    def __init__(
        self,
        *,
        min_interval: float = MIN_INTERVAL,
        rate_limit_wait: float = RATE_LIMIT_WAIT,
        max_rate_limit_retries: int = MAX_RATE_LIMIT_RETRIES,
        clock: cabc.Callable[[], float] = time.monotonic,
        sleep: cabc.Callable[[float], None] = time.sleep,
    ):
        self.min_interval = min_interval
        self.rate_limit_wait = rate_limit_wait
        self.max_rate_limit_retries = max_rate_limit_retries
        self._clock = clock
        self._sleep = sleep
        self._last_call: float | None = None

    def _pace(self) -> None:
        if self._last_call is not None:
            wait = self.min_interval - (self._clock() - self._last_call)
            if wait > 0:
                self._sleep(wait)
        self._last_call = self._clock()

    def __call__(self, request: t.Any) -> dict:
        for attempt in range(self.max_rate_limit_retries + 1):
            self._pace()
            try:
                return request.execute(num_retries=TRANSIENT_RETRIES)
            except googleapiclient_errors.HttpError as e:
                if not _is_rate_limit(e) or attempt == self.max_rate_limit_retries:
                    raise
                self._sleep(self.rate_limit_wait)
        raise AssertionError("unreachable")


class ApiGmailClient:
    """Thin read-only wrapper over the Gmail API for the signed-in user."""

    def __init__(
        self,
        credentials: oauth2_credentials.Credentials,
        execute: RateLimitedExecutor | None = None,
    ):
        service = googleapiclient_discovery.build(
            "gmail", "v1", credentials=credentials, cache_discovery=False
        )
        self._threads = service.users().threads()
        self._messages = service.users().messages()
        self._execute = execute or RateLimitedExecutor()

    def list_thread_ids(self, query: str) -> cabc.Iterator[str]:
        page_token = None
        while True:
            response = self._execute(
                self._threads.list(userId="me", q=query, pageToken=page_token, maxResults=500)
            )
            yield from (thread["id"] for thread in response.get("threads", []))
            page_token = response.get("nextPageToken")
            if not page_token:
                return

    def get_thread(self, thread_id: str) -> dict:
        return self._execute(self._threads.get(userId="me", id=thread_id, format="full"))

    def get_message(self, gmail_message_id: str) -> dict:
        return self._execute(self._messages.get(userId="me", id=gmail_message_id, format="full"))


class MessageSource(t.Protocol):
    """Anything that returns one Gmail message by id."""

    def get_message(self, gmail_message_id: str) -> dict: ...


def body_fetcher(client: MessageSource) -> cabc.Callable[[str], tuple[str | None, str | None]]:
    """(html, text) of a message by id, for callers that must not persist the body."""
    import job_radar.gmail.parse as gmail_parse

    def fetch(gmail_message_id: str) -> tuple[str | None, str | None]:
        try:
            message = client.get_message(gmail_message_id)
        except googleapiclient_errors.HttpError as e:
            # deleted from Gmail for good (e.g. emptied Trash)
            if e.resp.status == 404:
                raise mail.MessageGone(gmail_message_id) from e
            raise
        parsed = gmail_parse.parse_message(message)
        return parsed.body_html, parsed.body_text

    return fetch
