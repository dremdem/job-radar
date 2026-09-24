from datetime import UTC, date, datetime, timedelta

from gmail_fakes import FakeGmailClient, message, thread
from sqlalchemy import select

from job_radar.gmail.queries import QUERIES, GmailQuery
from job_radar.gmail.sync import build_query, sync_gmail
from job_radar.models import EmailDirection, EmailMessage, SyncCursor

ALERTS = QUERIES["linkedin_alerts"].query
PROPOSALS = QUERIES["job_proposal"].query
NOW = datetime(2026, 9, 24, 12, 0, tzinfo=UTC)
T0 = datetime(2026, 9, 20, 9, 0, tzinfo=UTC)


def alert(msg_id: str, sent_at: datetime = T0) -> dict:
    return thread(
        f"t-{msg_id}",
        message(
            msg_id,
            f"t-{msg_id}",
            sent_at=sent_at,
            from_addr="LinkedIn Job Alerts <jobalerts-noreply@linkedin.com>",
            html="<table>jobs</table>",
        ),
    )


def proposal_thread() -> dict:
    return thread(
        "tp",
        message("p1", "tp", sent_at=T0, labels=("INBOX", "Label_7")),
        message("p2", "tp", sent_at=T0 + timedelta(hours=2), labels=("SENT",), from_addr="me"),
    )


def stored(session) -> dict[str, EmailMessage]:
    return {m.gmail_message_id: m for m in session.scalars(select(EmailMessage))}


def test_sync_stores_thread_messages_with_direction(session):
    client = FakeGmailClient({PROPOSALS: [proposal_thread()]})
    stats = sync_gmail(session, client, names=["job_proposal"], now=NOW)
    messages = stored(session)
    assert set(messages) == {"p1", "p2"}
    assert messages["p1"].direction is EmailDirection.IN
    assert messages["p2"].direction is EmailDirection.OUT
    assert messages["p1"].categories == ["job_proposal"]
    assert stats["job_proposal"].new == 2


def test_bodies_stored_only_for_alerts(session):
    client = FakeGmailClient({ALERTS: [alert("a1")], PROPOSALS: [proposal_thread()]})
    sync_gmail(session, client, now=NOW)
    messages = stored(session)
    assert messages["a1"].body_html == "<table>jobs</table>"
    assert messages["p1"].body_html is None
    assert messages["p1"].body_text is None
    assert messages["p1"].snippet == "Hello there"


def test_resync_is_idempotent(session):
    client = FakeGmailClient({ALERTS: [alert("a1"), alert("a2")]})
    sync_gmail(session, client, names=["linkedin_alerts"], now=NOW)
    stats = sync_gmail(session, client, names=["linkedin_alerts"], now=NOW)
    assert len(stored(session)) == 2
    assert stats["linkedin_alerts"].new == 0


def test_message_seen_by_two_queries_gets_both_categories(session):
    shared = alert("a1")
    client = FakeGmailClient({ALERTS: [shared], PROPOSALS: [shared]})
    sync_gmail(session, client, now=NOW)
    assert stored(session)["a1"].categories == ["linkedin_alerts", "job_proposal"]


def test_cursor_saved_and_used_next_time(session):
    client = FakeGmailClient({ALERTS: [alert("a1", T0), alert("a2", T0 + timedelta(days=1))]})
    sync_gmail(session, client, names=["linkedin_alerts"], now=NOW)
    cursor = session.get(SyncCursor, "gmail:linkedin_alerts")
    assert cursor.last_message_at.replace(tzinfo=UTC) == T0 + timedelta(days=1)

    sync_gmail(session, client, names=["linkedin_alerts"], now=NOW)
    expected_after = int((T0 + timedelta(days=1) - timedelta(days=1)).timestamp())
    assert client.queries[-1] == f"{ALERTS} after:{expected_after}"


def test_build_query_first_run_uses_lookback_or_everything():
    lookback = GmailQuery("x", "label:x", store_body=False, initial_lookback_days=30)
    everything = GmailQuery("y", "label:y", store_body=False, initial_lookback_days=None)
    assert build_query(lookback, cursor=None, since=None, now=NOW) == (
        f"label:x after:{int((NOW - timedelta(days=30)).timestamp())}"
    )
    assert build_query(everything, cursor=None, since=None, now=NOW) == "label:y"


def test_build_query_since_overrides_cursor():
    q = QUERIES["job_proposal"]
    assert build_query(q, cursor=T0, since=date(2026, 1, 2), now=NOW) == (
        f"{q.query} after:2026/01/02"
    )


def test_readonly_scope_only():
    from job_radar.gmail.auth import SCOPES

    assert SCOPES == ["https://www.googleapis.com/auth/gmail.readonly"]


class FailingAfter(FakeGmailClient):
    def __init__(self, threads_by_query, fail_on: str):
        super().__init__(threads_by_query)
        self.fail_on = fail_on

    def get_thread(self, thread_id):
        if thread_id == self.fail_on:
            raise RuntimeError("quota")
        return super().get_thread(thread_id)


def test_failure_keeps_progress_but_not_cursor(session):
    import pytest

    client = FailingAfter({ALERTS: [alert("a1"), alert("a2")]}, fail_on="t-a2")
    with pytest.raises(RuntimeError):
        sync_gmail(session, client, names=["linkedin_alerts"], now=NOW)
    session.rollback()
    assert set(stored(session)) == {"a1"}
    cursor = session.get(SyncCursor, "gmail:linkedin_alerts")
    assert cursor is None or cursor.last_message_at is None


def test_progress_called_per_thread(session):
    calls = []
    client = FakeGmailClient({ALERTS: [alert("a1"), alert("a2")]})
    sync_gmail(
        session,
        client,
        names=["linkedin_alerts"],
        now=NOW,
        progress=lambda name, stats: calls.append((name, stats.threads)),
    )
    assert calls == [("linkedin_alerts", 1), ("linkedin_alerts", 2)]
