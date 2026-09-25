"""Company history: link mail to companies, label it, and merge it with applications and
rejections for the queue. Rule-based hints only; see docs/applications.md#company-history.
"""

import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from .applications import ATS_DOMAINS, _ats_company, _split_sender
from .dedup import get_or_create_company
from .linkedin.rejections import _REJECTION
from .models import (
    Application,
    ApplicationReview,
    Company,
    EmailDirection,
    EmailMessage,
    Rejection,
    RejectionSource,
)
from .rejections import mark_rejected

EXACT, PROBABLE = "exact", "probable"
FREE_MAIL = {
    "gmail.com", "googlemail.com", "yahoo.com", "outlook.com", "hotmail.com", "live.com",
    "icloud.com", "aol.com", "proton.me", "protonmail.com", "gmx.de", "gmx.net", "web.de",
    "mail.ru", "yandex.ru", "ya.ru",
}  # fmt: skip
NOTIFICATION_DOMAINS = ("linkedin.com", "hh.ru")
# Digests mention many companies at once; never guess a company from their subject.
_DIGEST_SENDERS = ("jobalerts-noreply@", "jobs-listings@", "messaging-digest-noreply@")
_RUSSIAN_REJECTION = re.compile(r"не готов пригласить|отказ", re.IGNORECASE)
_INTERVIEW = re.compile(
    r"interview|собеседовани|meeting invite|invitation:|technical (task|test|assessment)"
    r"|next steps?\b|calendly|schedule a (call|meeting)",
    re.IGNORECASE,
)
_MIN_SINGLE_WORD_NAME = 6
_LINKEDIN_VIEWED = re.compile(r"your application was viewed by (?P<company>.+)$", re.IGNORECASE)


def classify(subject: str, snippet: str) -> str:
    text = f"{subject}\n{snippet}"
    if _REJECTION.search(text) or _RUSSIAN_REJECTION.search(text):
        return "rejection"
    if _INTERVIEW.search(text):
        return "interview"
    return "other"


def _as_utc(value: datetime) -> datetime:
    # SQLite drops tzinfo; everything stored is UTC.
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _domain(from_addr: str) -> str:
    return _split_sender(from_addr)[1].rpartition("@")[2]


def _is_ats(domain: str) -> bool:
    return any(domain == d or domain.endswith("." + d) for d in ATS_DOMAINS)


def _is_company_domain(domain: str) -> bool:
    return (
        bool(domain)
        and domain not in FREE_MAIL
        and not _is_ats(domain)
        and not any(domain == d or domain.endswith("." + d) for d in NOTIFICATION_DOMAINS)
    )


@dataclass
class ContactStats:
    by_thread: int = 0
    by_domain: int = 0
    probable: int = 0
    labeled: int = 0
    rejections: int = 0


def _link(email: EmailMessage, company_id: int, match: str) -> None:
    email.company_id = company_id
    email.company_match = match


def _propagate_threads(emails: list[EmailMessage]) -> int:
    companies: dict[str, set[int]] = defaultdict(set)
    for e in emails:
        if e.company_id and e.company_match == EXACT:
            companies[e.gmail_thread_id].add(e.company_id)
    linked = 0
    for e in emails:
        found = companies.get(e.gmail_thread_id, set())
        if e.company_id is None and len(found) == 1:
            _link(e, next(iter(found)), EXACT)
            linked += 1
    return linked


def _learn_domains(session: Session, emails: list[EmailMessage]) -> int:
    owners: dict[str, set[int]] = defaultdict(set)
    for e in emails:
        exact_inbound = (
            e.company_id and e.company_match == EXACT and e.direction is EmailDirection.IN
        )
        if exact_inbound and _is_company_domain(domain := _domain(e.from_addr)):
            owners[domain].add(e.company_id)
    # A domain seen for several companies (a recruiting agency) identifies none of them.
    owners = {d: ids for d, ids in owners.items() if len(ids) == 1}
    for domain, ids in owners.items():
        company = session.get(Company, next(iter(ids)))
        if domain not in (company.domains or []):
            company.domains = [*(company.domains or []), domain]
    known = {d: next(iter(ids)) for d, ids in owners.items()}
    for company in session.scalars(select(Company)):
        for d in company.domains or []:
            known.setdefault(d, company.id)
    linked = 0
    for e in emails:
        if e.company_id is None and (cid := known.get(_domain(e.from_addr))):
            _link(e, cid, EXACT)
            linked += 1
    return linked


def _confirm_by_domain(normalized_name: str, from_addr: str) -> str:
    """Exact when the sender's own domain carries the company name (anna@northwind.com)."""
    domain = _domain(from_addr)
    compact = normalized_name.replace(" ", "")
    if _is_company_domain(domain) and compact in domain.split(".")[-2].replace("-", ""):
        return EXACT
    return PROBABLE


def _match_names(session: Session, emails: list[EmailMessage]) -> int:
    names = {}
    for company in session.scalars(select(Company)):
        n = company.normalized_name
        if len(n.split()) >= 2 or len(n) >= _MIN_SINGLE_WORD_NAME:
            names[n] = company.id
    if not names:
        return 0
    pattern = re.compile(
        r"\b(" + "|".join(re.escape(n) for n in sorted(names, key=len, reverse=True)) + r")\b"
    )
    linked = 0
    for e in emails:
        if e.company_id is not None or any(s in e.from_addr.lower() for s in _DIGEST_SENDERS):
            continue
        subject = re.sub(r"[^\w\s]", " ", e.subject.lower())
        if match := pattern.search(subject):
            match_kind = _confirm_by_domain(match.group(1), e.from_addr)
            _link(e, names[match.group(1)], match_kind)
            linked += match_kind == PROBABLE
    return linked


def _thread_starts(emails: list[EmailMessage]) -> set[int]:
    first: dict[str, EmailMessage] = {}
    for e in emails:
        cur = first.get(e.gmail_thread_id)
        if cur is None or _as_utc(e.sent_at) < _as_utc(cur.sent_at):
            first[e.gmail_thread_id] = e
    return {e.id for e in first.values()}


def link_and_label(session: Session) -> ContactStats:
    """Link unlinked mail to companies and label linked mail without an event. Idempotent."""
    stats = ContactStats()
    emails = session.scalars(select(EmailMessage)).all()

    for e in emails:
        # Links set by the application/rejection import come from the email itself.
        if e.company_id is not None and e.company_match is None:
            e.company_match = EXACT
        if (
            e.detected_event is None
            and "jobs-noreply@linkedin.com" in e.from_addr.lower()
            and (viewed := _LINKEDIN_VIEWED.search(e.subject.strip()))
        ):
            _link(e, get_or_create_company(session, viewed["company"].strip()).id, EXACT)
            e.detected_event = "viewed"

    # Rejections in ATS or direct mail (LinkedIn ones are handled by linkedin.rejections).
    for e in emails:
        if e.detected_event is not None or e.direction is not EmailDirection.IN:
            continue
        if classify(e.subject, e.snippet) != "rejection":
            continue
        display, address = _split_sender(e.from_addr)
        domain = address.rpartition("@")[2]
        name = _ats_company(display, domain, e.subject) if _is_ats(domain) else None
        if e.company_id is None and name:
            _link(e, get_or_create_company(session, name).id, EXACT)
        e.detected_event = "rejection"
        if e.company_id is not None:
            mark_rejected(
                session,
                company_id=e.company_id,
                source=RejectionSource.GMAIL,
                rejected_at=e.sent_at,
                email_message_id=e.id,
            )
            stats.rejections += 1

    stats.by_thread += _propagate_threads(emails)
    stats.probable += _match_names(session, emails)
    # Name matches confirmed by the sender domain are exact: learn from them too.
    stats.by_domain += _learn_domains(session, emails)
    stats.by_thread += _propagate_threads(emails)

    starts = _thread_starts(emails)
    for e in emails:
        if e.company_id is None or e.detected_event is not None:
            continue
        label = classify(e.subject, e.snippet)
        domain = _domain(e.from_addr)
        if (
            label == "other"
            and e.id in starts
            and e.direction is EmailDirection.IN
            and not _is_ats(domain)
            and "noreply" not in e.from_addr.lower().replace("-", "")
        ):
            label = "recruiter_outreach"
        if label == "rejection":
            mark_rejected(
                session,
                company_id=e.company_id,
                source=RejectionSource.GMAIL,
                rejected_at=e.sent_at,
                email_message_id=e.id,
            )
            stats.rejections += 1
        e.detected_event = label
        stats.labeled += 1
    session.commit()
    return stats


@dataclass(frozen=True)
class HistoryEvent:
    at: datetime
    kind: str  # applied, applied?, rejection, interview, recruiter_outreach, other, ...
    text: str
    gmail_url: str | None
    probable: bool = False


def gmail_url(thread_id: str | None) -> str | None:
    return f"https://mail.google.com/mail/u/0/#all/{thread_id}" if thread_id else None


def company_history(session: Session, company_id: int) -> list[HistoryEvent]:
    """Everything known about the company, newest first."""
    threads = dict(
        session.execute(
            select(EmailMessage.id, EmailMessage.gmail_thread_id).where(
                EmailMessage.company_id == company_id
            )
        ).all()
    )
    events: list[HistoryEvent] = []
    represented: set[int] = set()
    for app in session.scalars(
        select(Application).where(
            Application.company_id == company_id, Application.review != ApplicationReview.DISMISSED
        )
    ):
        represented.add(app.email_message_id)
        kind = "applied" if app.review is ApplicationReview.CONFIRMED else "applied?"
        url = gmail_url(
            threads.get(app.email_message_id) or _thread_of(session, app.email_message_id)
        )
        events.append(HistoryEvent(_as_utc(app.applied_at), kind, app.role or "unknown role", url))
    for r in session.scalars(select(Rejection).where(Rejection.company_id == company_id)):
        represented.add(r.email_message_id)
        url = gmail_url(threads.get(r.email_message_id) or _thread_of(session, r.email_message_id))
        events.append(HistoryEvent(_as_utc(r.rejected_at), "rejection", r.role or "", url))
    for e in session.scalars(select(EmailMessage).where(EmailMessage.company_id == company_id)):
        if e.id in represented or e.detected_event in ("application_confirmation", "rejection"):
            continue
        events.append(
            HistoryEvent(
                _as_utc(e.sent_at),
                e.detected_event or "email",
                e.subject,
                gmail_url(e.gmail_thread_id),
                probable=e.company_match == PROBABLE,
            )
        )
    return sorted(events, key=lambda ev: ev.at, reverse=True)


def _thread_of(session: Session, email_id: int | None) -> str | None:
    if email_id is None:
        return None
    return session.scalar(select(EmailMessage.gmail_thread_id).where(EmailMessage.id == email_id))
