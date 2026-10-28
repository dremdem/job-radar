"""Remote Python posts from "Ask HN: Who is hiring?" (#233, docs/hackernews.md).

The fixture is a trimmed copy of the October 2026 thread from the HN Search API: nine real
posts (one with a reply) and a deleted comment.
"""

import json
from pathlib import Path

from sqlalchemy import select

from job_radar.hackernews import (
    HackerNewsError,
    import_hackernews,
    latest_thread,
    parse_post,
    wants,
)
from job_radar.models import JobSnapshot, Opportunity, OpportunityState, SourceRecord, SourceType

FIXTURE = Path(__file__).parent / "fixtures" / "hackernews" / "thread.json"
THREADS = {
    "hits": [
        {"objectID": "49922568", "title": "Ask HN: Who wants to be hired? (October 2026)",
         "created_at_i": 1790870400},
        {"objectID": "49922569", "title": "Ask HN: Who is hiring? (October 2026)",
         "created_at_i": 1790870400},
        {"objectID": "49522897", "title": "Ask HN: Who is hiring? (September 2026)",
         "created_at_i": 1788192000},
    ]
}  # fmt: skip


def thread() -> dict:
    return json.loads(FIXTURE.read_text())


def post(company_start: str):
    posts = [parse_post(c) for c in thread()["children"] if c["text"]]
    return next(p for p in posts if p.header.startswith(company_start))


class FakeClient:
    def __init__(self, item: dict | Exception | None = None, threads: dict = THREADS):
        self._item = thread() if item is None else item
        self._threads = threads
        self.items: list[int] = []

    def threads(self) -> dict:
        return self._threads

    def item(self, item_id: int) -> dict:
        self.items.append(item_id)
        if isinstance(self._item, Exception):
            raise self._item
        return self._item


def test_reads_the_newest_who_is_hiring_thread_not_who_wants_to_be_hired():
    assert latest_thread(THREADS)["objectID"] == "49922569"
    assert latest_thread({"hits": THREADS["hits"][:1]}) is None


def test_header_gives_company_role_and_location():
    p = post("Enveritas")
    assert (p.company, p.title, p.location) == (
        "Enveritas (YC S18, non-profit)",
        "Backend Software Engineer",
        "Remote (Global)",
    )
    assert p.url == "https://news.ycombinator.com/item?id=49925489"
    assert "Backend Software Engineer (Python" in p.description


def test_only_the_remote_option_of_a_hybrid_or_remote_post_is_kept():
    # "San Francisco HYBRID (3 days/wk) or REMOTE (US)": the hybrid rule must not drop it.
    assert post("Paramark").location == "REMOTE (US)"
    # "or" inside brackets is not an option of its own.
    assert post("RINSE").location == "REMOTE (US or Canada)"
    # "Berlin, Germany / REMOTE (CET ±3 preferred)"
    assert post("OpenSanctions").location == "REMOTE (CET ±3 preferred)"


def test_remote_note_after_the_company_and_dash_separators():
    p = post("PrairieLearn")
    assert (p.company, p.title, p.location) == (
        "PrairieLearn",
        "Full-Stack Software Engineer",
        "Remote US",
    )


def test_links_are_not_part_of_the_location_and_the_header_is_its_own_line():
    raw = {
        "id": 1,
        "created_at_i": 1790870400,
        "text": "3Gimbals | https://www.linkedin.com/jobs/view/1 (US/Remote)<p>Python and React.",
    }
    p = parse_post(raw)
    assert p.location == "(US/Remote)"
    assert (
        p.description.splitlines()[0]
        == "3Gimbals | https://www.linkedin.com/jobs/view/1 (US/Remote)"
    )


def test_a_header_without_a_role_points_to_the_post():
    p = post("Curai")
    assert (p.title, p.location, p.has_role) == ("Curai: roles in the post", "Remote (US)", False)


def test_which_posts_are_kept():
    kept = {p.company for c in thread()["children"] if c["text"] and wants(p := parse_post(c))}
    assert kept == {"Enveritas (YC S18, non-profit)", "Sidekick Labs", "Paramark", "Curai", "RINSE"}
    assert not wants(post("Fastly"))  # on-site preferred
    assert not wants(post("Open Education"))  # hybrid
    assert not wants(post("PrairieLearn"))  # remote, but TypeScript, no Python
    assert not wants(post("OpenSanctions"))  # remote Python, but "CLOSED NOW" in the header


def test_sync_imports_kept_posts_once(session):
    client = FakeClient()
    stats = import_hackernews(session, client)
    assert client.items == [49922569]
    assert stats.thread == "Ask HN: Who is hiring? (October 2026)"
    # Nine posts: the deleted comment and the reply under Enveritas are not posts.
    assert (stats.posts, stats.new, stats.known, stats.skipped) == (9, 5, 0, 4)

    record = session.scalar(select(SourceRecord).where(SourceRecord.external_id == "49925489"))
    assert record.source_type is SourceType.HACKERNEWS
    assert record.url == "https://news.ycombinator.com/item?id=49925489"
    o = record.opportunity
    assert (o.title, o.company.name, o.location) == (
        "Backend Software Engineer",
        "Enveritas (YC S18, non-profit)",
        "Remote (Global)",
    )
    assert {"sign": "+", "text": "Worldwide / anywhere"} in o.score_reasons
    snapshot = session.scalar(select(JobSnapshot).where(JobSnapshot.source_record_id == record.id))
    assert "smallholder coffee" in snapshot.raw_text

    again = import_hackernews(session, FakeClient())
    assert (again.new, again.known) == (0, 5)
    assert session.query(Opportunity).count() == 5


def test_auto_ignore_rules_run_on_new_posts(session):
    from job_radar.auto_ignore import Rules, save_rules

    save_rules(session, Rules(countries=("Global",)))
    import_hackernews(session, FakeClient())
    o = session.scalar(
        select(Opportunity).join(SourceRecord).where(SourceRecord.external_id == "49925489")
    )
    assert o.state is OpportunityState.AUTO_IGNORE


def test_an_unreadable_api_is_reported_and_imports_nothing(session):
    stats = import_hackernews(session, FakeClient(item=HackerNewsError("HTTP 503")))
    assert stats.error == "HTTP 503"
    assert session.query(Opportunity).count() == 0

    stats = import_hackernews(session, FakeClient(threads={"hits": []}))
    assert stats.error and "Who is hiring" in stats.error


def test_sync_step_reports_counts_and_can_be_turned_off(session):
    from job_radar.config import Settings
    from job_radar.pipeline import import_hackernews as step

    lines: list[str] = []
    step(session, Settings(hackernews=True), lines.append, client=FakeClient())
    assert lines == [
        "hackernews: Ask HN: Who is hiring? (October 2026): 9 posts, 5 new, "
        "0 already stored, 4 skipped (not remote Python)"
    ]
    lines.clear()
    step(session, Settings(hackernews=False), lines.append, client=FakeClient())
    assert lines == ["hackernews: disabled (JOB_RADAR_HACKERNEWS=false)."]
