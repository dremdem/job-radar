"""Rejections, applications and employer messages from hh.ru mail. See docs/hh.md.

Bodies are fetched from Gmail and dropped: only what is found is stored.
"""

import re
from dataclasses import dataclass

from bs4 import BeautifulSoup
from sqlalchemy import or_, select, update
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
    # The same vacancy may already be here from Telegram or LinkedIn (#172).
    opportunity = same_vacancy(session, company_id, notice.title)
    if opportunity is not None:
        _mark_applied(opportunity)
    else:
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


def _same_title(title: str) -> str:
    return " ".join(title.casefold().split())


def _has_hh(opportunity: Opportunity) -> bool:
    return any(r.source_type is SourceType.HH for r in opportunity.source_records)


def same_vacancy(session: Session, company_id: int, title: str) -> Opportunity | None:
    """The one vacancy of the company with this title and no hh.ru source; None if 0 or 2+."""
    found = [
        o
        for o in session.scalars(select(Opportunity).where(Opportunity.company_id == company_id))
        if _same_title(o.title) == _same_title(title) and not _has_hh(o)
    ]
    return found[0] if len(found) == 1 else None


def _mark_applied(opportunity: Opportunity) -> None:
    # An hh.ru notice about a vacancy means you applied to it.
    if opportunity.state is not OpportunityState.APPLIED:
        opportunity.state = OpportunityState.APPLIED
        opportunity.auto_ignore_reason = None


def merge_hh_duplicates(session: Session, *, apply: bool) -> list[tuple[int, int]]:
    """Merge vacancies made from hh.ru notices into the same vacancy from another source.

    Returns (kept, merged) pairs; with apply=False nothing changes (a dry run).
    """
    pairs = []
    hh_vacancies = session.scalars(
        select(Opportunity)
        .join(SourceRecord)
        .where(SourceRecord.source_type == SourceType.HH)
        .distinct()
    ).all()
    for dup in hh_vacancies:
        keep = same_vacancy(session, dup.company_id, dup.title)
        # 1:1 only: the other vacancy must match no other hh.ru vacancy either.
        rivals = [
            o
            for o in hh_vacancies
            if o.company_id == dup.company_id and _same_title(o.title) == _same_title(dup.title)
        ]
        if keep is None or len(rivals) != 1 or keep.id > dup.id:
            continue
        pairs.append((keep.id, dup.id))
        if apply:
            _merge(session, keep, dup)
    if apply:
        session.commit()
    return pairs


def _merge(session: Session, keep: Opportunity, dup: Opportunity) -> None:
    from ..models import (
        Application,
        ClaudeAnalysis,
        ClaudeMessage,
        DescriptionAnalysis,
        OpportunityNote,
        Rejection,
        TelegramPost,
    )

    for model in (
        SourceRecord,
        Rejection,
        Application,
        OpportunityNote,
        TelegramPost,
        DescriptionAnalysis,
        ClaudeAnalysis,
        ClaudeMessage,
    ):
        session.execute(
            update(model).where(model.opportunity_id == dup.id).values(opportunity_id=keep.id)
        )
    if dup.state is OpportunityState.APPLIED:
        _mark_applied(keep)
    session.flush()
    session.expire(keep)
    session.expire(dup)
    session.delete(dup)
    session.flush()


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
