"""Pages the user sent with the bookmarklet: job cards and, on a vacancy, its description.

The bookmarklet only reads a page the user opened; parsing happens here so it can be
tested. ``import_capture`` creates vacancies from search cards and stores the description
of the open vacancy; the Chrome fill reuses it. See docs/linkedin-capture.md.

Usage:
    import job_radar.linkedin.capture as linkedin_capture
"""

import dataclasses
import re
import typing as t

import sqlalchemy as sa
import sqlalchemy.orm as sa_orm

import job_radar.dedup as dedup
import job_radar.linkedin.alerts as linkedin_alerts
import job_radar.models as models

_NOISE = re.compile(
    r"^(promoted( by hirer)?|viewed|applied|saved|new|top applicant|easy apply|"
    r"be an early applicant|actively (reviewing applicants|recruiting|hiring)|"
    r"responses managed off linkedin|remote|hybrid|on-site|"
    r"(reposted |posted )?\d+\s+(second|minute|hour|day|week|month|year)s?\s+ago|"
    r"\d+\+?\s+(connections?|school alumni|alumni|applicants?)\b.*|over \d+ .*|"
    r"dismiss.*|your profile matches.*|.* with verification)$",
    re.IGNORECASE,
)
_SALARY = re.compile(r"[$€£¥₹]|/\s*(yr|year|hr|hour|mo|month)\b", re.IGNORECASE)
_MAX_TEXT = 20000


# Matched at the end of a stripped line: a leading \s* would be slow on long runs of spaces.
_VERIFIED = re.compile(r"\((verified job|with verification)\)$", re.IGNORECASE)


def _lines(text: str) -> list[str]:
    out: list[str] = []
    # "Be an early applicant · 1 day ago" is two noise items on one line.
    for raw in text.splitlines():
        for part in raw.split(" · "):
            # Semantic search repeats the title as "<title> (Verified job)".
            line = _VERIFIED.sub("", part.strip().strip("·").strip()).rstrip()
            if not line or _NOISE.match(line) or (out and line == out[-1]):
                continue
            out.append(line)
    return out


def parse_card_text(job_id: str, text: str) -> linkedin_alerts.AlertJob | None:
    """Title, company, location, salary and Easy Apply from a search card's visible text."""
    easy_apply = any(
        item.strip().lower() == "easy apply"
        for line in (text or "").splitlines()
        for item in line.split("·")
    )
    lines = _lines(text or "")
    salary = next((line for line in lines if _SALARY.search(line)), None)
    lines = [line for line in lines if line != salary]
    if len(lines) < 2:
        return None
    return linkedin_alerts.AlertJob(
        job_id=job_id,
        title=lines[0],
        company=lines[1],
        location=lines[2] if len(lines) > 2 else None,
        salary=salary,
        easy_apply=easy_apply,
    )


@dataclasses.dataclass
class CaptureStats:
    """What one bookmarklet page added: cards, new vacancies, descriptions."""

    cards: int = 0
    new: int = 0
    known: int = 0
    skipped: int = 0
    descriptions: int = 0
    # What happened to the open vacancy, e.g. "Acme — Engineer: description saved".
    vacancy: str = ""
    # New or changed opportunities, so an open queue tab can redraw them.
    opportunity_ids: list[int] = dataclasses.field(default_factory=list)

    def touch(self, opportunity_id: int | None) -> None:
        if opportunity_id is not None and opportunity_id not in self.opportunity_ids:
            self.opportunity_ids.append(opportunity_id)

    def as_dict(self) -> dict[str, int | str | list[int]]:
        return {**self.__dict__, "opportunity_ids": list(self.opportunity_ids)}


def _record(session: sa_orm.Session, job_id: str) -> models.SourceRecord | None:
    return session.scalar(
        sa.select(models.SourceRecord).where(
            models.SourceRecord.source_type.in_(linkedin_alerts.LINKEDIN_SOURCES),
            models.SourceRecord.external_id == job_id,
        )
    )


def _job_id(value: t.Any) -> str | None:
    text = str(value or "").strip()
    return text if text.isdigit() else None


def import_capture(session: sa_orm.Session, payload: dict[str, t.Any]) -> CaptureStats:
    stats = CaptureStats()
    # an hh.ru vacancy page (#136)
    if payload.get("source") == "hh":
        import job_radar.hh.capture as hh_capture

        hh_capture.import_hh_capture(session, payload, stats)
        session.commit()
        return stats
    detail = payload.get("detail") or {}
    detail_id = _job_id(detail.get("job_id"))
    own_card: linkedin_alerts.AlertJob | None = None
    for card in payload.get("cards") or []:
        job_id = _job_id(card.get("job_id"))
        if job_id and job_id == detail_id:
            # The open vacancy is reported on its own line, not as a card (on a vacancy page
            # this "card" is just a link to itself, e.g. its salary); its text may still help.
            own_card = own_card or parse_card_text(job_id, str(card.get("text") or "")[:_MAX_TEXT])
            continue
        stats.cards += 1
        job = parse_card_text(job_id, str(card.get("text") or "")[:_MAX_TEXT]) if job_id else None
        if job is None:
            stats.skipped += 1
        elif linkedin_alerts.add_linkedin_job(session, job, models.SourceType.LINKEDIN_CAPTURE):
            stats.new += 1
            stats.touch(_record(session, job.job_id).opportunity_id)
        else:
            stats.known += 1

    if job_id := detail_id:
        record = _record(session, job_id)
        created = False
        title, company = (detail.get("title") or "").strip(), (detail.get("company") or "").strip()
        if not (title and company):
            # The tab title of a job page: "<title> | <company> | LinkedIn".
            parts = [p.strip() for p in str(detail.get("page_title") or "").split(" | ")]
            if len(parts) >= 3 and parts[-1] == "LinkedIn":
                title, company = title or parts[0], company or parts[1]
        location = (detail.get("location") or "").strip() or None
        # Prefer the vacancy's own card (it has the location and Easy Apply), else the tab title.
        from_title = (
            linkedin_alerts.AlertJob(job_id, title, company, location, None, None)
            if title and company
            else None
        )
        job = own_card or from_title
        if record is None and job is not None:
            linkedin_alerts.add_linkedin_job(session, job, models.SourceType.LINKEDIN_CAPTURE)
            stats.new += 1
            created = True
            record = _record(session, job_id)
        description = str(detail.get("description") or "").strip()[:_MAX_TEXT]
        outcome = []
        if created:
            outcome.append("new")
        # before the rules below
        if record is not None and detail.get("not_accepting"):
            import job_radar.auto_ignore as auto_ignore

            auto_ignore.mark_not_accepting(session, record.opportunity)
            outcome.append("not accepting applications")
        if record is None:
            outcome.append("not recognised (no title or company on the page)")
        elif not description:
            outcome.append("no description found on the page")
        else:
            _, saved = dedup.add_snapshot(session, record, description)
            stats.descriptions += int(saved)
            outcome.append("description saved" if saved else "description already saved")
            if saved:
                import job_radar.auto_ignore as auto_ignore

                session.refresh(record.opportunity)
                auto_ignore.apply_rules(session, record.opportunity)
        if record is not None:
            o = record.opportunity
            title, company = o.title, o.company.name
            stats.touch(o.id)
        label = f"{company} — {title}" if title and company else f"job {job_id}"
        stats.vacancy = f"{label}: {', '.join(outcome)}"
    session.commit()
    return stats
