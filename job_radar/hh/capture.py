"""An hh.ru vacancy page sent by the bookmarklet: description and work format (#136).

The bookmarklet reads only the page the user opened; nothing is fetched from hh.ru.
``import_hh_capture`` stores the description on the matching vacancy, or creates one,
and ``location_for`` turns hh.ru's work formats into a location (remote wins when it is
one of them). See docs/hh.md#descriptions-the-bookmarklet-136.

Usage:
    import job_radar.hh.capture as hh_capture
"""

import typing as t

import sqlalchemy as sa
import sqlalchemy.orm as sa_orm

import job_radar.dedup as dedup
import job_radar.hh.notifications as hh_notifications
import job_radar.models as models
import job_radar.scoring.rules as scoring_rules

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
    return address.split(",")[0].strip()


def import_hh_capture(session: sa_orm.Session, payload: dict[str, t.Any], stats) -> None:
    import job_radar.review.auto_ignore as review_auto_ignore

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
        sa.select(models.SourceRecord).where(
            models.SourceRecord.source_type == models.SourceType.HH,
            models.SourceRecord.external_id == vacancy_id,
        )
    )
    outcome = []
    if record is None:
        score = scoring_rules.score_job(title, location or None)
        opportunity = models.Opportunity(
            title=title[:255],
            company=dedup.get_or_create_company(session, company or f"Unknown (hh {vacancy_id})"),
            location=location or None,
            salary=salary,
            score=score.value,
            score_reasons=[r.as_dict() for r in score.reasons],
        )
        record = models.SourceRecord(
            source_type=models.SourceType.HH,
            external_id=vacancy_id,
            url=hh_notifications.VACANCY_URL.format(id=vacancy_id),
            opportunity=opportunity,
        )
        session.add(record)
        session.flush()
        stats.new += 1
        outcome.append("new")
    else:
        o = record.opportunity
        if location:
            # the page knows the work format; the digest did not
            o.location = location
        o.salary = o.salary or salary
    description = str(detail.get("description") or "").strip()[:MAX_TEXT]
    if not description:
        outcome.append("no description found on the page")
    else:
        _, saved = dedup.add_snapshot(session, record, description)
        stats.descriptions += int(saved)
        outcome.append("description saved" if saved else "description already saved")
    session.flush()
    session.refresh(record.opportunity)
    review_auto_ignore.apply_rules(session, record.opportunity)
    o = record.opportunity
    stats.touch(o.id)
    stats.vacancy = f"{o.company.name} — {o.title}: {', '.join(outcome)}"
