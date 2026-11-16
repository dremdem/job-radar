"""The Gmail searches that are synced.

Each ``GmailQuery`` has a name (also its sync cursor), the Gmail search string, whether
message bodies are stored and how far back the first run looks. ``QUERIES`` is the list
the CLI and the Sync button use. See docs/gmail.md.

Usage:
    import job_radar.gmail.queries as gmail_queries
"""

import dataclasses


@dataclasses.dataclass(frozen=True)
class GmailQuery:
    """One synced Gmail search: name, query, whether to store bodies, first lookback."""

    name: str
    query: str
    store_body: bool
    # None: first run imports everything
    initial_lookback_days: int | None


QUERIES: dict[str, GmailQuery] = {
    q.name: q
    for q in [
        GmailQuery(
            "linkedin_alerts",
            "from:(jobalerts-noreply@linkedin.com OR jobs-noreply@linkedin.com)",
            store_body=True,
            initial_lookback_days=30,
        ),
        GmailQuery(
            "job_proposal", "label:job-proposal", store_body=False, initial_lookback_days=None
        ),
    ]
}
