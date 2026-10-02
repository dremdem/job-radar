"""Rejections, applications and employer messages from hh.ru mail. See docs/hh.md.

Bodies are fetched from Gmail and dropped: only what is found is stored.
"""

import re
from dataclasses import dataclass

from bs4 import BeautifulSoup
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from ..dedup import get_or_create_company
from ..linkedin.rejections import _REJECTION, FetchBody
from ..models import (
    Application,
    ApplicationReview,
    ApplicationSource,
    EmailDirection,
    EmailMessage,
    Opportunity,
    OpportunityState,
    Rejection,
    RejectionSource,
    SourceRecord,
    SourceType,
)
from ..rejections import mark_rejected

SENDER = "noreply@hh.ru"
VACANCY_URL = "https://hh.ru/vacancy/{id}"
_VACANCY_LINK = re.compile(r"hh\.ru/vacancy/(\d+)")
_INVISIBLE = re.compile("[​-‍⠀͏﻿ ]")
# "Вакансия: <title> Компания: <name>" (newer) or "... компании: <name>" (older mail).
_FIELDS = re.compile(
    r"Вакансия:\s*(?P<title>.+?)\s+Компани[яи]:\s*(?P<company>.+?)\s+"
    r"(?:Посмотреть|Перейти|Сообщение|Выбрать|Если)",
    re.IGNORECASE,
)
_KINDS = [
    ("rejection", re.compile(r"не готов пригласить", re.IGNORECASE)),
    ("closed", re.compile(r"закрыл позицию", re.IGNORECASE)),
    ("message", re.compile(r"вам написали|хотят связаться|новое сообщение", re.IGNORECASE)),
]
_DIGEST = re.compile(r"вакансии по подписке", re.IGNORECASE)
DIGEST_EVENT = "hh_digest"
OUTREACH_EVENT = "recruiter_outreach"


@dataclass(frozen=True)
class Notice:
    kind: str  # rejection / closed / message
    title: str
    company: str
    vacancy_id: str | None
    text: str  # the visible text, to check a message for rejection wording


def _text(html: str) -> str:
    text = BeautifulSoup(html, "html.parser").get_text(" ")
    return " ".join(_INVISIBLE.sub(" ", text).split())


def _kind(subject: str) -> str | None:
    subject = " ".join(_INVISIBLE.sub(" ", subject).split())  # subjects use no-break spaces
    return next((k for k, pattern in _KINDS if pattern.search(subject)), None)


def parse_notice(subject: str, html: str | None) -> Notice | None:
    kind = _kind(subject)
    if kind is None or not html:
        return None
    text = _text(html)
    match = _FIELDS.search(text)
    if not match:
        return None
    link = _VACANCY_LINK.search(html)
    return Notice(
        kind=kind,
        title=match["title"].strip(),
        company=match["company"].strip().strip('«»"'),
        vacancy_id=link.group(1) if link else None,
        text=text,
    )


@dataclass
class HhStats:
    emails: int = 0
    notices: int = 0
    rejections: int = 0
    messages: int = 0
    vacancies: int = 0
    fetched: int = 0
    pending: int = 0  # wait for a run with Gmail access


def _vacancy(session: Session, notice: Notice, company_id: int) -> Opportunity | None:
    if notice.vacancy_id is None:
        return None
    record = session.scalar(
        select(SourceRecord).where(
            SourceRecord.source_type == SourceType.HH,
            SourceRecord.external_id == notice.vacancy_id,
        )
    )
    if record is not None:
        return record.opportunity
    opportunity = Opportunity(
        title=notice.title[:255], company_id=company_id, state=OpportunityState.APPLIED
    )
    session.add(opportunity)
    session.flush()
    session.add(
        SourceRecord(
            source_type=SourceType.HH,
            external_id=notice.vacancy_id,
            url=VACANCY_URL.format(id=notice.vacancy_id),
            opportunity=opportunity,
        )
    )
    session.flush()
    return opportunity


def _application(session: Session, email: EmailMessage, notice: Notice, company_id: int, o):
    existing = session.scalar(
        select(Application).where(
            Application.company_id == company_id,
            Application.source == ApplicationSource.HH,
            Application.role == notice.title,
        )
    )
    if existing is not None:
        existing.opportunity_id = existing.opportunity_id or (o.id if o else None)
        return
    session.add(
        Application(
            company_id=company_id,
            opportunity_id=o.id if o else None,
            applied_at=email.sent_at,
            role=notice.title,
            source=ApplicationSource.HH,
            review=ApplicationReview.CONFIRMED,
            email_message_id=email.id,
            notes="Applied on hh.ru; date unknown, set to the first hh.ru notice about it.",
        )
    )


def import_hh_notifications(session: Session, fetch_body: FetchBody | None) -> HhStats:
    """hh.ru mail not handled yet (or only marked as a rejection without a company)."""
    stats = HhStats()
    emails = session.scalars(
        select(EmailMessage)
        .where(
            EmailMessage.direction == EmailDirection.IN,
            EmailMessage.from_addr.like(f"%{SENDER}%"),
            EmailMessage.company_id.is_(None),
            or_(EmailMessage.detected_event.is_(None), EmailMessage.detected_event == "rejection"),
        )
        .order_by(EmailMessage.sent_at)
    ).all()
    for email in emails:
        stats.emails += 1
        if _DIGEST.search(email.subject.replace("\u00a0", " ")):
            email.detected_event = DIGEST_EVENT
            continue
        if _kind(email.subject) is None:
            continue
        if fetch_body is None:
            stats.pending += 1
            continue
        html, _ = fetch_body(email.gmail_message_id)
        stats.fetched += 1
        notice = parse_notice(email.subject, html)
        if notice is None:
            continue
        stats.notices += 1
        company = get_or_create_company(session, notice.company)
        session.flush()
        email.company_id, email.company_match = company.id, "exact"
        rejected = notice.kind in ("rejection", "closed") or bool(_REJECTION.search(notice.text))
        if not rejected:
            email.detected_event = OUTREACH_EVENT
            stats.messages += 1
            session.commit()
            continue
        known = notice.vacancy_id and session.scalar(
            select(SourceRecord.id).where(
                SourceRecord.source_type == SourceType.HH,
                SourceRecord.external_id == notice.vacancy_id,
            )
        )
        o = _vacancy(session, notice, company.id)
        stats.vacancies += int(o is not None and not known)
        _application(session, email, notice, company.id, o)
        if not session.scalar(select(Rejection.id).where(Rejection.email_message_id == email.id)):
            mark_rejected(
                session,
                company_id=company.id,
                opportunity_id=o.id if o else None,
                source=RejectionSource.GMAIL,
                role=notice.title,
                rejected_at=email.sent_at,
                email_message_id=email.id,
            )
            stats.rejections += 1
        email.detected_event = "rejection"
        session.commit()
    session.commit()
    return stats
