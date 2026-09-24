import json
import time
from collections.abc import Callable, Iterator
from typing import Any, Protocol

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

# Gmail enforces a per-user, per-minute query-cost quota. Pace requests evenly and, when the
# quota is still hit, wait for the next minute instead of backing off at random.
MIN_INTERVAL = 0.5
RATE_LIMIT_WAIT = 60.0
MAX_RATE_LIMIT_RETRIES = 5
TRANSIENT_RETRIES = 2  # googleapiclient's own retries, for 5xx and connection errors


class GmailClient(Protocol):
    def list_thread_ids(self, query: str) -> Iterator[str]: ...
    def get_thread(self, thread_id: str) -> dict: ...


def _is_rate_limit(error: HttpError) -> bool:
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
    def __init__(
        self,
        *,
        min_interval: float = MIN_INTERVAL,
        rate_limit_wait: float = RATE_LIMIT_WAIT,
        max_rate_limit_retries: int = MAX_RATE_LIMIT_RETRIES,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
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

    def __call__(self, request: Any) -> dict:
        for attempt in range(self.max_rate_limit_retries + 1):
            self._pace()
            try:
                return request.execute(num_retries=TRANSIENT_RETRIES)
            except HttpError as e:
                if not _is_rate_limit(e) or attempt == self.max_rate_limit_retries:
                    raise
                self._sleep(self.rate_limit_wait)
        raise AssertionError("unreachable")


class ApiGmailClient:
    """Thin read-only wrapper over the Gmail API for the signed-in user."""

    def __init__(self, credentials: Credentials, execute: RateLimitedExecutor | None = None):
        service = build("gmail", "v1", credentials=credentials, cache_discovery=False)
        self._threads = service.users().threads()
        self._execute = execute or RateLimitedExecutor()

    def list_thread_ids(self, query: str) -> Iterator[str]:
        page_token = None
        while True:
            response = self._execute(
                self._threads.list(userId="me", q=query, pageToken=page_token, maxResults=500)
            )
            yield from (t["id"] for t in response.get("threads", []))
            page_token = response.get("nextPageToken")
            if not page_token:
                return

    def get_thread(self, thread_id: str) -> dict:
        return self._execute(self._threads.get(userId="me", id=thread_id, format="full"))
