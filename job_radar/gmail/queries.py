from dataclasses import dataclass


@dataclass(frozen=True)
class GmailQuery:
    name: str
    query: str
    store_body: bool
    initial_lookback_days: int | None  # None: first run imports everything


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
