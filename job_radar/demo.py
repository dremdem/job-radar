"""Demo data for trying the review queue before any source is imported."""

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from .dedup import get_or_create_company
from .models import Company, Opportunity, OpportunityState

DEMO_SUFFIX = "(demo)"

# (title, company, score, state)
DEMO_OPPORTUNITIES = [
    ("Senior Python Backend Engineer", "Acme Robotics", 8.5, OpportunityState.NEW),
    ("Staff Engineer, Data Platform", "Globex", 7.0, OpportunityState.NEW),
    ("Python Developer (FastAPI)", "Initech", None, OpportunityState.NEW),
    ("Backend Engineer, Payments", "Umbrella Labs", 6.0, OpportunityState.INTERESTED),
    ("Senior Django Engineer", "Hooli", 3.5, OpportunityState.IGNORE),
]


def seed_demo(session: Session) -> int:
    """Add demo opportunities that are not there yet. Returns how many were added."""
    created = 0
    for title, company_name, score, state in DEMO_OPPORTUNITIES:
        company = get_or_create_company(session, f"{company_name} {DEMO_SUFFIX}")
        exists = session.scalar(
            select(Opportunity).where(
                Opportunity.company_id == company.id, Opportunity.title == title
            )
        )
        if exists is None:
            session.add(Opportunity(title=title, company=company, score=score, state=state))
            created += 1
    session.commit()
    return created


def remove_demo(session: Session) -> int:
    """Delete demo companies and their opportunities. Returns how many opportunities went."""
    company_ids = select(Company.id).where(Company.name.endswith(DEMO_SUFFIX))
    removed = session.execute(
        delete(Opportunity).where(Opportunity.company_id.in_(company_ids))
    ).rowcount
    session.execute(delete(Company).where(Company.name.endswith(DEMO_SUFFIX)))
    session.commit()
    return removed
