"""Demo data for trying the review queue before any source is imported (docs/queue.md#demo).

Everything here is made up: companies end in "(demo)", links point to example.com, and the
Claude analyses are written by hand in the shape of a real answer. No model is called.
"""

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from .claude.analysis import _check_quotes
from .claude.prompt import PROMPT_VERSION
from .dedup import add_snapshot, get_or_create_company
from .models import (
    Application,
    ClaudeAnalysis,
    ClaudeMessage,
    Company,
    CompanyEvent,
    DescriptionAnalysis,
    EmailMessage,
    JobSnapshot,
    Opportunity,
    OpportunityNote,
    OpportunityState,
    Rejection,
    SourceRecord,
    SourceType,
    TelegramPost,
)
from .scoring import score_job

DEMO_SUFFIX = "(demo)"


@dataclass(frozen=True)
class Demo:
    title: str
    company: str
    location: str
    salary: str | None
    state: OpportunityState
    description: str
    claude: dict[str, Any] | None = None  # a finished analysis, as Claude would return it
    ai: dict[str, Any] = field(default_factory=dict)  # a local model finding (docs/ai.md)


ACME = """\
Acme Robotics builds fleet software for warehouse robots.

We are hiring a Senior Python Backend Engineer to own the services that plan robot routes.
This is a fully remote role: you can work from anywhere in the world.

What you will do:
- Design and run Python services with FastAPI and PostgreSQL.
- Keep our event pipeline on Kafka fast and reliable.
- Review code and mentor two mid-level engineers.

You have:
- 6+ years of backend development in Python.
- Experience with async Python and PostgreSQL in production.
- Docker and CI/CD pipelines you built yourself.

Nice to have: Kubernetes, robotics or logistics experience.
Salary: $120k-150k a year, paid as a contractor."""

GLOBEX = """\
Globex is a data company for retail chains.

As a Staff Engineer on the Data Platform team you will shape how 40 engineers ship data.
Remote, but you must be based in the US and able to work US Eastern hours.

Requirements:
- 8+ years of software engineering, at least 3 at staff level or as a tech lead.
- Strong Python and SQL.
- Production experience with Apache Spark and Airflow is required.
- You have led a migration of a data warehouse.

We move fast: expect on-call one week in four and an occasional weekend release."""

INITECH = """\
Initech is a small fintech team of 12 people.

We need a Python Developer (FastAPI) to build our public API for small businesses.
Remote within Europe and nearby time zones (CET +/- 3 hours).

Stack: Python 3.12, FastAPI, SQLAlchemy, PostgreSQL, Redis, GitHub Actions.
3+ years of Python and good written English are what we ask for."""

UMBRELLA = """\
Umbrella Labs processes card payments for online shops.

Backend Engineer, Payments.
The role is remote, and the applicant must be based in the EU for payroll reasons.

You will build ledger and payout services in Python and Go.
Experience with payments, idempotency and PostgreSQL is a strong plus."""

HOOLI = """\
Hooli is looking for a Senior Django Engineer for its London office.

Hybrid: three days a week in our London office, two from home.
Django, Django REST Framework, Celery and PostgreSQL."""

DEMO_OPPORTUNITIES = [
    Demo(
        "Senior Python Backend Engineer",
        "Acme Robotics",
        "Worldwide (Remote)",
        "$120k-150k",
        OpportunityState.NEW,
        ACME,
        claude={
            "verdict": "apply",
            "match": 86,
            "summary": (
                "A close match: senior Python backend work with FastAPI and PostgreSQL, fully "
                "remote from anywhere. The only gap is Kafka, which is named in the duties but "
                "not in the requirements."
            ),
            "location": {
                "verdict": "ok",
                "note": "Fully remote from anywhere in the world.",
                "quote": "This is a fully remote role: you can work from anywhere in the world.",
            },
            "strengths": [
                {
                    "point": "Years of Python backend work match the 6+ asked for.",
                    "quote": "6+ years of backend development in Python.",
                },
                {
                    "point": "Docker and CI/CD pipelines built by yourself are in the CV.",
                    "quote": "Docker and CI/CD pipelines you built yourself.",
                },
            ],
            "gaps": [
                {
                    "skill": "Kafka",
                    "requirement": "nice_to_have",
                    "blocker": False,
                    "note": "Not in the CV; part of the duties, learnable on the job.",
                    "quote": "Keep our event pipeline on Kafka fast and reliable.",
                }
            ],
            "red_flags": [],
            "ignore_reason": {"kind": "none", "detail": ""},
            "questions": [
                "Is the contractor agreement open to any country, and in which currency?",
                "How large is the team that owns the route planning services?",
            ],
            "cv_keywords": ["FastAPI", "PostgreSQL", "async Python", "mentoring"],
        },
    ),
    Demo(
        "Staff Engineer, Data Platform",
        "Globex",
        "Remote, USA Only",
        None,
        OpportunityState.NEW,
        GLOBEX,
        claude={
            "verdict": "maybe",
            "match": 55,
            "summary": (
                "Strong Python and SQL fit, but the role is for US residents and requires "
                "Spark and Airflow in production. Worth asking only if they hire contractors "
                "abroad."
            ),
            "location": {
                "verdict": "blocker",
                "note": "Only for people based in the US.",
                "quote": "Remote, but you must be based in the US and able to work US Eastern "
                "hours.",
            },
            "strengths": [
                {
                    "point": "Python and SQL are the core of the CV.",
                    "quote": "Strong Python and SQL.",
                }
            ],
            "gaps": [
                {
                    "skill": "Apache Spark",
                    "requirement": "must_have",
                    "blocker": True,
                    "note": "Required in production; not in the CV.",
                    "quote": "Production experience with Apache Spark and Airflow is required.",
                }
            ],
            "red_flags": [
                {
                    "point": "Regular on-call and weekend releases.",
                    "quote": "expect on-call one week in four and an occasional weekend release.",
                }
            ],
            "ignore_reason": {"kind": "location", "detail": "US residents only"},
            "questions": ["Do you hire contractors outside the US for this team?"],
            "cv_keywords": ["Python", "SQL", "data platform"],
        },
    ),
    Demo(
        "Python Developer (FastAPI)",
        "Initech",
        "Remote, CET (+/- 3 hours)",
        "€60k-75k",
        OpportunityState.NEW,
        INITECH,
        claude={
            "verdict": "maybe",
            "match": 72,
            "summary": (
                "The stack is a full match, but the role is for Europe and nearby time zones. "
                "Worth a question about where you can work from before applying."
            ),
            "location": {
                "verdict": "risk",
                "note": "Remote within Europe and CET +/- 3 hours; other places are not stated.",
                "quote": "Remote within Europe and nearby time zones (CET +/- 3 hours).",
            },
            "strengths": [
                {
                    "point": "FastAPI, SQLAlchemy and PostgreSQL are all in the CV.",
                    "quote": "Stack: Python 3.12, FastAPI, SQLAlchemy, PostgreSQL, Redis, "
                    "GitHub Actions.",
                }
            ],
            "gaps": [],
            "red_flags": [],
            "ignore_reason": {"kind": "none", "detail": ""},
            "questions": ["Can the role be done from outside Europe within CET +/- 3 hours?"],
            "cv_keywords": ["FastAPI", "SQLAlchemy", "public API"],
        },
    ),
    Demo(
        "Backend Engineer, Payments",
        "Umbrella Labs",
        "Remote, Europe",
        None,
        OpportunityState.INTERESTED,
        UMBRELLA,
        ai={
            "work_mode": "remote",
            "work_mode_quote": "The role is remote",
            "residence": ["EU"],
            "residence_quote": "the applicant must be based in the EU for payroll reasons.",
        },
    ),
    Demo(
        "Senior Django Engineer",
        "Hooli",
        "London (Hybrid)",
        "£85k",
        OpportunityState.IGNORE,
        HOOLI,
    ),
]


def _add(session: Session, demo: Demo) -> None:
    score = score_job(demo.title, demo.location)  # as a real import would give it
    opportunity = Opportunity(
        title=demo.title,
        company=get_or_create_company(session, f"{demo.company} {DEMO_SUFFIX}"),
        location=demo.location,
        salary=demo.salary,
        score=score.value,
        score_reasons=[r.as_dict() for r in score.reasons],
        state=demo.state,
    )
    slug = demo.company.lower().replace(" ", "-")
    record = SourceRecord(
        source_type=SourceType.MANUAL,
        external_id=f"demo:{slug}",
        url=f"https://example.com/jobs/{slug}",
        opportunity=opportunity,
    )
    session.add(record)
    session.flush()
    snapshot, _ = add_snapshot(session, record, demo.description)
    session.flush()
    now = datetime.now(UTC)
    if demo.ai:
        session.add(
            DescriptionAnalysis(
                snapshot_id=snapshot.id,
                opportunity_id=opportunity.id,
                model="demo",
                raw=demo.ai,
                **demo.ai,
            )
        )
    if demo.claude:
        session.add(
            ClaudeAnalysis(
                opportunity_id=opportunity.id,
                state="done",
                model="demo",
                prompt_version=PROMPT_VERSION,
                finished_at=now,  # never started: it does not count toward the daily cap
                result=_check_quotes(dict(demo.claude), demo.description),
            )
        )


def seed_demo(session: Session) -> int:
    """Add demo opportunities that are not there yet. Returns how many were added."""
    created = 0
    for demo in DEMO_OPPORTUNITIES:
        company = get_or_create_company(session, f"{demo.company} {DEMO_SUFFIX}")
        exists = session.scalar(
            select(Opportunity).where(
                Opportunity.company_id == company.id, Opportunity.title == demo.title
            )
        )
        if exists is None:
            _add(session, demo)
            created += 1
    session.commit()
    return created


def remove_demo(session: Session) -> int:
    """Delete demo companies with everything attached to them. Returns how many opportunities
    went. Real data is never touched: every delete is limited to demo companies."""
    companies = select(Company.id).where(Company.name.endswith(DEMO_SUFFIX))
    opportunities = select(Opportunity.id).where(Opportunity.company_id.in_(companies))
    records = select(SourceRecord.id).where(SourceRecord.opportunity_id.in_(opportunities))
    for model in (DescriptionAnalysis, ClaudeMessage, ClaudeAnalysis, OpportunityNote):
        session.execute(delete(model).where(model.opportunity_id.in_(opportunities)))
    for model in (CompanyEvent, Application, Rejection):  # what you logged on a demo vacancy
        session.execute(delete(model).where(model.company_id.in_(companies)))
    session.execute(delete(JobSnapshot).where(JobSnapshot.source_record_id.in_(records)))
    session.execute(delete(SourceRecord).where(SourceRecord.id.in_(records)))
    # Mail and Telegram posts are never deleted, only unlinked from demo data.
    session.execute(
        update(EmailMessage).where(EmailMessage.company_id.in_(companies)).values(company_id=None)
    )
    session.execute(
        update(TelegramPost)
        .where(TelegramPost.opportunity_id.in_(opportunities))
        .values(opportunity_id=None)
    )
    removed = session.execute(
        delete(Opportunity).where(Opportunity.company_id.in_(companies))
    ).rowcount
    session.execute(delete(Company).where(Company.name.endswith(DEMO_SUFFIX)))
    session.commit()
    return removed
