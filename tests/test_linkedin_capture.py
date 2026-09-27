from sqlalchemy import func, select

from job_radar.linkedin.alerts import AlertJob, add_linkedin_job
from job_radar.linkedin.capture import import_capture, parse_card_text
from job_radar.models import JobSnapshot, Opportunity, SourceRecord, SourceType

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
    job = parse_card_text("4471000001", PFIZER)
    assert (job.title, job.company, job.location) == (
        "Staff Software Engineer (AI Backend Services)",
        "Pfizer",
        "Sofia",
    )
    assert job.easy_apply is False and job.salary is None


def test_parse_card_easy_apply_and_noise():
    job = parse_card_text("1", FUSE)
    assert (job.company, job.location, job.easy_apply) == (
        "Fuse Energy",
        "Switzerland (Remote)",
        True,
    )
    job = parse_card_text("2", GLOBALLOGIC)
    assert (job.title, job.company, job.location) == (
        "Middle Python Engineer with AI Expertise IRC305232",
        "GlobalLogic",
        "Kyiv (Remote)",
    )


def test_parse_card_salary():
    job = parse_card_text("3", SALARY)
    assert job.salary == "£69.3K/yr - £84.9K/yr"
    assert job.location == "United Kingdom (Remote)"


def test_unreadable_card_is_skipped():
    assert parse_card_text("4", "") is None
    assert parse_card_text("5", "Promoted\n1 day ago") is None


def count(session, model) -> int:
    return session.scalar(select(func.count()).select_from(model))


def payload(cards, detail=None):
    return {
        "source_url": "https://www.linkedin.com/jobs/search-results/",
        "cards": cards,
        "detail": detail,
    }


def test_import_capture_creates_new_and_skips_known(session):
    add_linkedin_job(
        session,
        AlertJob("1", "Senior/Lead Backend Engineer (Remote)", "Fuse Energy", None, None, True),
        SourceType.LINKEDIN_ALERT,
    )
    stats = import_capture(
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
    pfizer = session.scalar(select(Opportunity).where(Opportunity.title.like("Staff Software%")))
    assert pfizer.company.name == "Pfizer"
    record = pfizer.source_records[0]
    assert record.source_type is SourceType.LINKEDIN_CAPTURE
    assert record.url == "https://www.linkedin.com/jobs/view/4471000001/"
    assert import_capture(session, payload([{"job_id": "4471000001", "text": PFIZER}])).new == 0


def test_detail_saves_description_for_new_or_known_job(session):
    stats = import_capture(
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
    snapshot = session.scalar(select(JobSnapshot))
    assert "EV charging" in snapshot.raw_text
    # Sending the same page again does not duplicate the description.
    import_capture(
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
    assert count(session, JobSnapshot) == 1
    assert count(session, SourceRecord) == 1


def test_detail_for_a_card_on_the_same_page(session):
    stats = import_capture(
        session,
        payload(
            [{"job_id": "4471000001", "text": PFIZER}],
            {"job_id": "4471000001", "description": "Full text"},
        ),
    )
    assert (stats.new, stats.descriptions) == (1, 1)


# Real card texts from LinkedIn's semantic search page (/jobs/search-results/), sanitized.
SEMANTIC = __import__("json").loads(
    (
        __import__("pathlib").Path(__file__).parent
        / "fixtures"
        / "linkedin_capture"
        / "semantic_search.json"
    ).read_text()
)


def test_semantic_page_all_cards_parse():
    jobs = [parse_card_text(c["job_id"], c["text"]) for c in SEMANTIC["cards"]]
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
    by_id = {c["job_id"]: parse_card_text(c["job_id"], c["text"]) for c in SEMANTIC["cards"]}
    infused = by_id["4469742675"]
    assert (infused.company, infused.location) == ("Infused Solutions", "London (Hybrid)")
    assert infused.salary == "65K GBP/yr - 75K GBP/yr" and infused.easy_apply is True
    avenga = by_id["4471680406"]  # "1 connection works here", "Saved"
    assert (avenga.company, avenga.location) == ("Avenga", "Bratislava (Remote)")


def test_semantic_page_import_with_detail_from_page_title(session):
    stats = import_capture(session, payload(SEMANTIC["cards"], SEMANTIC["detail"]))
    # The open vacancy's own card is covered by the detail, not counted twice.
    assert (stats.cards, stats.new, stats.skipped, stats.descriptions) == (24, 25, 0, 1)
    assert stats.vacancy == "Empat — Python Developer: new, description saved"
    record = session.scalar(select(SourceRecord).where(SourceRecord.external_id == "4471482895"))
    assert record.opportunity.location == "Ukraine (Remote)"  # from its own card
    assert "conversational AI agents" in record.snapshots[0].raw_text


def test_detail_title_and_company_from_page_title(session):
    stats = import_capture(
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
    o = session.scalar(select(Opportunity))
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
    stats = import_capture(session, VACANCY_PAGE)
    assert (stats.cards, stats.skipped, stats.new, stats.descriptions) == (0, 0, 1, 1)
    assert stats.vacancy == "ScorePlay — Senior Back-end Engineer: new, description saved"
    again = import_capture(session, VACANCY_PAGE)
    assert (again.cards, again.skipped, again.new) == (0, 0, 0)
    assert again.vacancy == "ScorePlay — Senior Back-end Engineer: description already saved"


def test_vacancy_page_without_description(session):
    page = {**VACANCY_PAGE, "detail": {**VACANCY_PAGE["detail"], "description": ""}}
    assert import_capture(session, page).vacancy.endswith("no description found on the page")
