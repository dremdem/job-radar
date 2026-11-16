"""Tests for job_radar.sources.gmail.client: request pacing, rate-limit retries and reading
message bodies, with fake requests and a fake clock (docs/gmail.md).
"""

import googleapiclient.errors as googleapiclient_errors
import httplib2
import pytest

import job_radar.sources.gmail.client as gmail_client


def http_error(status: int, reason: str = "") -> googleapiclient_errors.HttpError:
    content = f'{{"error": {{"errors": [{{"reason": "{reason}"}}]}}}}'.encode()
    return googleapiclient_errors.HttpError(httplib2.Response({"status": status}), content)


class FakeRequest:
    """A Gmail API request that returns or raises the given outcomes in order."""

    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.calls = 0

    def execute(self, num_retries: int = 0):
        self.calls += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class FakeClock:
    """A clock that only moves when the code sleeps."""

    def __init__(self):
        self.now = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def executor(clock: FakeClock, **kwargs) -> gmail_client.RateLimitedExecutor:
    return gmail_client.RateLimitedExecutor(
        min_interval=0.5, rate_limit_wait=60, clock=clock.monotonic, sleep=clock.sleep, **kwargs
    )


def test_paces_consecutive_calls():
    clock = FakeClock()
    run = executor(clock)
    run(FakeRequest({"a": 1}))
    run(FakeRequest({"a": 2}))
    assert clock.sleeps == [0.5]


@pytest.mark.parametrize("error", [http_error(429), http_error(403, "rateLimitExceeded")])
def test_waits_out_rate_limit_then_succeeds(error):
    clock = FakeClock()
    request = FakeRequest(error, {"ok": True})
    assert executor(clock)(request) == {"ok": True}
    assert request.calls == 2
    assert 60 in clock.sleeps


def test_gives_up_after_max_retries():
    clock = FakeClock()
    request = FakeRequest(*[http_error(429)] * 3)
    with pytest.raises(googleapiclient_errors.HttpError):
        executor(clock, max_rate_limit_retries=2)(request)
    assert request.calls == 3


def test_other_errors_are_not_retried():
    clock = FakeClock()
    request = FakeRequest(http_error(404))
    with pytest.raises(googleapiclient_errors.HttpError):
        executor(clock)(request)
    assert request.calls == 1


def test_body_fetcher_returns_html_and_text():
    import datetime as dt

    import gmail_fakes

    class Client:
        def get_message(self, gmail_message_id):
            return gmail_fakes.message(
                gmail_message_id, "t", sent_at=dt.datetime(2026, 1, 1, tzinfo=dt.UTC)
            )

    assert gmail_client.body_fetcher(Client())("m1") == ("<p>Hello <b>there</b></p>", "Hello there")


def test_body_fetcher_turns_404_into_message_gone():
    import job_radar.mail as mail

    class Client:
        def get_message(self, gmail_message_id):
            raise http_error(404, "notFound")

    with pytest.raises(mail.MessageGone):
        gmail_client.body_fetcher(Client())("m1")


def test_body_fetcher_lets_other_errors_through():

    class Client:
        def get_message(self, gmail_message_id):
            raise http_error(401, "authError")

    with pytest.raises(googleapiclient_errors.HttpError):
        gmail_client.body_fetcher(Client())("m1")
