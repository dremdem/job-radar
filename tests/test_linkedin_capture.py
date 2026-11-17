"""Tests for job_radar.sources.linkedin.capture: search cards and vacancy pages sent by the
bookmarklet (docs/linkedin-capture.md).
"""

import paths
import sqlalchemy as sa

import job_radar.models as models
import job_radar.review.auto_ignore as review_auto_ignore
import job_radar.sources.linkedin.alerts as linkedin_alerts
import job_radar.sources.linkedin.capture as linkedin_capture

# Card texts as LinkedIn's logged-in search page shows them (innerText), from the user's screen.
PFIZER = """Staff Software Engineer (AI Backend Services)
Staff Software Engineer (AI Backend Services) with verification
Pfizer
Sofia
Be an early applicant
·
1 day ago"""
FUSE = """Senior/Lead Backend Engineer (Remote)
Senior/Lead Backend Engineer (Remote) with verification
Fuse Energy
Switzerland (Remote)
Actively reviewing applicants
1 day ago
·
Easy Apply"""
GLOBALLOGIC = """Promoted
Middle Python Engineer with AI Expertise IRC305232
GlobalLogic
Kyiv (Remote)
2 connections work here
Be an early applicant · 1 day ago"""
SALARY = """Senior Backend Engineer (Python)
ev.energy
United Kingdom (Remote)
£69.3K/yr - £84.9K/yr
Viewed
Promoted by hirer"""


def test_parse_card_pfizer():
    job = linkedin_capture.parse_card_text("4471000001", PFIZER)
    assert (job.title, job.company, job.location) == (
        "Staff Software Engineer (AI Backend Services)",
        "Pfizer",
        "Sofia",
    )
    assert job.easy_apply is False and job.salary is None


def test_parse_card_easy_apply_and_noise():
    job = linkedin_capture.parse_card_text("1", FUSE)
    assert (job.company, job.location, job.easy_apply) == (
        "Fuse Energy",
        "Switzerland (Remote)",
        True,
    )
    job = linkedin_capture.parse_card_text("2", GLOBALLOGIC)
    assert (job.title, job.company, job.location) == (
        "Middle Python Engineer with AI Expertise IRC305232",
        "GlobalLogic",
        "Kyiv (Remote)",
    )


def test_parse_card_salary():
    job = linkedin_capture.parse_card_text("3", SALARY)
    assert job.salary == "£69.3K/yr - £84.9K/yr"
    assert job.location == "United Kingdom (Remote)"


def test_unreadable_card_is_skipped():
    assert linkedin_capture.parse_card_text("4", "") is None
    assert linkedin_capture.parse_card_text("5", "Promoted\n1 day ago") is None


def count(session, model) -> int:
    return session.scalar(sa.select(sa.func.count()).select_from(model))


def payload(cards, detail=None):
    return {
        "source_url": "https://www.linkedin.com/jobs/search-results/",
        "cards": cards,
        "detail": detail,
    }


def test_import_capture_creates_new_and_skips_known(session):
    linkedin_alerts.add_linkedin_job(
        session,
        linkedin_alerts.AlertJob(
            "1", "Senior/Lead Backend Engineer (Remote)", "Fuse Energy", None, None, True
        ),
        models.SourceType.LINKEDIN_ALERT,
    )
    stats = linkedin_capture.import_capture(
        session,
        payload(
            [
                {"job_id": "4471000001", "text": PFIZER},
                {"job_id": "1", "text": FUSE},
                {"job_id": "9", "text": ""},
            ]
        ),
    )
    assert (stats.cards, stats.new, stats.known, stats.skipped) == (3, 1, 1, 1)
    pfizer = session.scalar(
        sa.select(models.Opportunity).where(models.Opportunity.title.like("Staff Software%"))
    )
    assert pfizer.company.name == "Pfizer"
    record = pfizer.source_records[0]
    assert record.source_type is models.SourceType.LINKEDIN_CAPTURE
    assert record.url == "https://www.linkedin.com/jobs/view/4471000001/"
    assert (
        linkedin_capture.import_capture(
            session, payload([{"job_id": "4471000001", "text": PFIZER}])
        ).new
        == 0
    )


def test_detail_saves_description_for_new_or_known_job(session):
    stats = linkedin_capture.import_capture(
        session,
        payload(
            [],
            {
                "job_id": "77",
                "title": "Senior Backend Engineer (Python)",
                "company": "ev.energy",
                "location": "United Kingdom",
                "description": "About the job\nWe build EV charging software in Python.",
            },
        ),
    )
    assert (stats.new, stats.descriptions) == (1, 1)
    snapshot = session.scalar(sa.select(models.JobSnapshot))
    assert "EV charging" in snapshot.raw_text
    # Sending the same page again does not duplicate the description.
    linkedin_capture.import_capture(
        session,
        payload(
            [],
            {
                "job_id": "77",
                "title": "x",
                "company": "y",
                "description": "About the job\nWe build EV charging software in Python.",
            },
        ),
    )
    assert count(session, models.JobSnapshot) == 1
    assert count(session, models.SourceRecord) == 1


def test_detail_for_a_card_on_the_same_page(session):
    stats = linkedin_capture.import_capture(
        session,
        payload(
            [{"job_id": "4471000001", "text": PFIZER}],
            {"job_id": "4471000001", "description": "Full text"},
        ),
    )
    assert (stats.new, stats.descriptions) == (1, 1)


# Real card texts from LinkedIn's semantic search page (/jobs/search-results/), sanitized.
SEMANTIC = __import__("json").loads(
    (paths.FIXTURES / "linkedin_capture" / "semantic_search.json").read_text()
)


def test_semantic_page_all_cards_parse():
    jobs = [linkedin_capture.parse_card_text(c["job_id"], c["text"]) for c in SEMANTIC["cards"]]
    assert len(jobs) == 25 and all(jobs)
    first = jobs[0]
    assert (first.title, first.company, first.location) == (
        "Python Developer",
        "Empat",
        "Ukraine (Remote)",
    )
    assert all("Verified job" not in j.title for j in jobs)
    assert all(j.location and not j.location.lower().startswith("posted") for j in jobs)


def test_semantic_page_salary_easy_apply_and_badges():
    by_id = {
        c["job_id"]: linkedin_capture.parse_card_text(c["job_id"], c["text"])
        for c in SEMANTIC["cards"]
    }
    infused = by_id["4469742675"]
    assert (infused.company, infused.location) == ("Infused Solutions", "London (Hybrid)")
    assert infused.salary == "65K GBP/yr - 75K GBP/yr" and infused.easy_apply is True
    # "1 connection works here", "Saved"
    avenga = by_id["4471680406"]
    assert (avenga.company, avenga.location) == ("Avenga", "Bratislava (Remote)")


def test_semantic_page_import_with_detail_from_page_title(session):
    stats = linkedin_capture.import_capture(session, payload(SEMANTIC["cards"], SEMANTIC["detail"]))
    # The open vacancy's own card is covered by the detail, not counted twice.
    assert (stats.cards, stats.new, stats.skipped, stats.descriptions) == (24, 25, 0, 1)
    assert stats.vacancy == "Empat — Python Developer: new, description saved"
    record = session.scalar(
        sa.select(models.SourceRecord).where(models.SourceRecord.external_id == "4471482895")
    )
    # from its own card
    assert record.opportunity.location == "Ukraine (Remote)"
    assert "conversational AI agents" in record.snapshots[0].raw_text


def test_detail_title_and_company_from_page_title(session):
    stats = linkedin_capture.import_capture(
        session,
        payload(
            [],
            {
                "job_id": "99",
                "page_title": "Senior Backend Engineer (Python) | ev.energy | LinkedIn",
                "description": "Text",
            },
        ),
    )
    assert stats.new == 1
    o = session.scalar(sa.select(models.Opportunity))
    assert (o.title, o.company.name) == ("Senior Backend Engineer (Python)", "ev.energy")


VACANCY_PAGE = {
    # What the bookmarklet sent from https://www.linkedin.com/jobs/view/4329057092/ (shortened).
    "source_url": "https://www.linkedin.com/jobs/view/4329057092/",
    "cards": [{"job_id": "4329057092", "text": "75K EUR/yr - 105K EUR/yr"}],
    "detail": {
        "job_id": "4329057092",
        "page_title": "Senior Back-end Engineer | ScorePlay | LinkedIn",
        "title": "",
        "company": "",
        "description": "About the job\n\nScorePlay is the AI media infrastructure for sports.",
    },
}


def test_vacancy_page_is_not_reported_unreadable(session):
    stats = linkedin_capture.import_capture(session, VACANCY_PAGE)
    assert (stats.cards, stats.skipped, stats.new, stats.descriptions) == (0, 0, 1, 1)
    assert stats.vacancy == "ScorePlay — Senior Back-end Engineer: new, description saved"
    again = linkedin_capture.import_capture(session, VACANCY_PAGE)
    assert (again.cards, again.skipped, again.new) == (0, 0, 0)
    assert again.vacancy == "ScorePlay — Senior Back-end Engineer: description already saved"


def test_vacancy_page_without_description(session):
    page = {**VACANCY_PAGE, "detail": {**VACANCY_PAGE["detail"], "description": ""}}
    assert linkedin_capture.import_capture(session, page).vacancy.endswith(
        "no description found on the page"
    )


def not_accepting_payload(flag):
    detail = {"job_id": "77", "page_title": "Engineer | Acme | LinkedIn", "description": "Python"}
    return {"source_url": "u", "cards": [], "detail": detail | {"not_accepting": flag}}


def captured(session):
    return session.scalar(sa.select(models.Opportunity))


def test_not_accepting_page_keeps_the_description_and_is_auto_ignored(session):
    stats = linkedin_capture.import_capture(session, not_accepting_payload(True))
    o = captured(session)
    assert stats.descriptions == 1 and "not accepting" in stats.vacancy
    assert o.not_accepting_at is not None
    assert (o.state, o.auto_ignore_reason) == (
        models.OpportunityState.AUTO_IGNORE,
        review_auto_ignore.NOT_ACCEPTING_REASON,
    )


def test_not_accepting_stays_in_review_when_the_rule_is_off(session):
    review_auto_ignore.save_rules(session, review_auto_ignore.Rules(unavailable=False))
    linkedin_capture.import_capture(session, not_accepting_payload(True))
    o = captured(session)
    assert o.not_accepting_at is not None
    assert (o.state, o.auto_ignore_reason) == (models.OpportunityState.NEW, None)


def test_a_page_without_the_line_is_not_marked(session):
    linkedin_capture.import_capture(session, not_accepting_payload(False))
    o = captured(session)
    assert o.not_accepting_at is None and o.state is models.OpportunityState.NEW


def test_clearing_undoes_only_this_rule(session):
    linkedin_capture.import_capture(session, not_accepting_payload(True))
    o = captured(session)
    review_auto_ignore.clear_unavailable(o)
    assert (o.not_accepting_at, o.state, o.auto_ignore_reason) == (
        None,
        models.OpportunityState.NEW,
        None,
    )


def test_card_text_cleanup_is_linear_on_long_runs_of_spaces():
    # CodeQL py/polynomial-redos (#11): these regexes were quadratic on runs of whitespace.
    job = linkedin_capture.parse_card_text(
        "1", "Python Developer   (Verified job)  \nAcme\nRemote\n·  Easy Apply "
    )
    assert job is not None and job.title == "Python Developer" and job.easy_apply
    other = linkedin_capture.parse_card_text("2", "Python Developer\nAcme\nRemote\nnot easy apply")
    assert other is not None and other.easy_apply is False
    spaces = " " * 50_000
    # returns promptly
    linkedin_capture.parse_card_text("3", f"Python Developer{spaces}x\nAcme\n{spaces}\n")
