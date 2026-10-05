"""An hh.ru vacancy page sent by the bookmarklet: description and work format (#136).

The bookmarklet reads only the page the user opened; nothing is fetched from hh.ru.
See docs/hh.md#descriptions-the-bookmarklet-136.
"""

import re
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..dedup import add_snapshot, get_or_create_company
from ..models import Opportunity, SourceRecord, SourceType
from ..scoring import score_job
from .notifications import VACANCY_URL

MAX_TEXT = 20000


def location_for(work_format: str, city: str) -> str:
    """Remote wins when it is one of the formats ("удалённо или гибрид")."""
    city = city.strip()
    text = work_format.casefold()
    if "удал" in text:
        return f"Remote ({city})" if city else "Remote"
    if "гибрид" in text:
        return f"Hybrid, {city}" if city else "Hybrid"
    if "на месте" in text:
        return f"On-site, {city}" if city else "On-site"
    return city or ""


def _city(address: str) -> str:
    return re.split(r"\s*,\s*", address.strip())[0] if address.strip() else ""


def import_hh_capture(session: Session, payload: dict[str, Any], stats) -> None:
    from ..auto_ignore import apply_rules

    detail = payload.get("detail") or {}
    vacancy_id = str(detail.get("job_id") or "").strip()
    title = " ".join(str(detail.get("title") or "").split())
    company = " ".join(str(detail.get("company") or "").split())
    if not vacancy_id.isdigit() or not title:
        stats.vacancy = "hh.ru page not recognised (no vacancy id or title on the page)"
        return
    location = location_for(
        str(detail.get("work_format") or ""), _city(str(detail.get("address") or ""))
    )
    salary = " ".join(str(detail.get("salary") or "").split()) or None
    record = session.scalar(
        select(SourceRecord).where(
            SourceRecord.source_type == SourceType.HH, SourceRecord.external_id == vacancy_id
        )
    )
    outcome = []
    if record is None:
        score = score_job(title, location or None)
        opportunity = Opportunity(
            title=title[:255],
            company=get_or_create_company(session, company or f"Unknown (hh {vacancy_id})"),
            location=location or None,
            salary=salary,
            score=score.value,
            score_reasons=[r.as_dict() for r in score.reasons],
        )
        record = SourceRecord(
            source_type=SourceType.HH,
            external_id=vacancy_id,
            url=VACANCY_URL.format(id=vacancy_id),
            opportunity=opportunity,
        )
        session.add(record)
        session.flush()
        stats.new += 1
        outcome.append("new")
    else:
        o = record.opportunity
        if location:
            o.location = location  # the page knows the work format; the digest did not
        o.salary = o.salary or salary
    description = str(detail.get("description") or "").strip()[:MAX_TEXT]
    if not description:
        outcome.append("no description found on the page")
    else:
        _, saved = add_snapshot(session, record, description)
        stats.descriptions += int(saved)
        outcome.append("description saved" if saved else "description already saved")
    session.flush()
    session.refresh(record.opportunity)
    apply_rules(session, record.opportunity)
    o = record.opportunity
    stats.touch(o.id)
    stats.vacancy = f"{o.company.name} — {o.title}: {', '.join(outcome)}"
