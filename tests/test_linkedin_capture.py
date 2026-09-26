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
