"""Company history: everything known about a company, for the queue.

Mail is linked to companies by thread and by sender domain, then labeled by rules
(rejection, interview or other). The history merges that mail with
applications, rejections, notes and events added by hand, newest first. These are
rule-based hints, not facts: a probable match is marked as such.

Main entry points: ``link_and_label`` (idempotent), ``company_history``,
``other_vacancies`` and ``delete_history_item``. See
docs/applications.md#company-history.

Usage:
    import job_radar.contacts as contacts
"""

import collections
import dataclasses
import datetime as dt
import re

import sqlalchemy as sa
import sqlalchemy.orm as sa_orm

import job_radar.applications as applications
import job_radar.dedup as dedup
import job_radar.models as models
import job_radar.notes as notes
import job_radar.rejections as rejections

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
    if rejections.WORDING.search(text) or _RUSSIAN_REJECTION.search(text):
        return "rejection"
    if _INTERVIEW.search(text):
        return "interview"
    return "other"


def _as_utc(value: dt.datetime) -> dt.datetime:
    # SQLite drops tzinfo; everything stored is UTC.
    return value if value.tzinfo else value.replace(tzinfo=dt.UTC)


def _domain(from_addr: str) -> str:
    return applications._split_sender(from_addr)[1].rpartition("@")[2]


def _is_ats(domain: str) -> bool:
    return any(domain == d or domain.endswith("." + d) for d in applications.ATS_DOMAINS)


def _is_company_domain(domain: str) -> bool:
    return (
        bool(domain)
        and domain not in FREE_MAIL
        and not _is_ats(domain)
        and not any(domain == d or domain.endswith("." + d) for d in NOTIFICATION_DOMAINS)
    )


@dataclasses.dataclass
class ContactStats:
    """Counters of one ``link_and_label`` run: how mail was linked and labeled."""

    by_thread: int = 0
    by_domain: int = 0
    probable: int = 0
    labeled: int = 0
    rejections: int = 0


def _link(email: models.EmailMessage, company_id: int, match: str) -> None:
    email.company_id = company_id
    email.company_match = match


def _undo_ats_domains(session: sa_orm.Session, emails: list[models.EmailMessage]) -> int:
    """An ATS sender domain names no company (#178): forget it where it was learned, and move
    its mail to the employer named in the subject when that is another company."""
    learned: dict[int, set[str]] = {}
    for company in session.scalars(sa.select(models.Company)):
        ats = {d for d in company.domains or [] if _is_ats(d)}
        if ats:
            company.domains = [d for d in company.domains if d not in ats]
            learned[company.id] = ats
    moved = 0
    for e in emails:
        if e.company_id not in learned or _domain(e.from_addr) not in learned[e.company_id]:
            continue
        name = applications.ats_subject_company(e.subject)
        current = session.get(models.Company, e.company_id)
        if name and dedup.normalize_company_name(name) != current.normalized_name:
            _link(e, dedup.get_or_create_company(session, name).id, EXACT)
            moved += 1
    return moved


def _propagate_threads(emails: list[models.EmailMessage]) -> int:
    companies: dict[str, set[int]] = collections.defaultdict(set)
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


def _learn_domains(session: sa_orm.Session, emails: list[models.EmailMessage]) -> int:
    owners: dict[str, set[int]] = collections.defaultdict(set)
    for e in emails:
        exact_inbound = (
            e.company_id and e.company_match == EXACT and e.direction is models.EmailDirection.IN
        )
        if exact_inbound and _is_company_domain(domain := _domain(e.from_addr)):
            owners[domain].add(e.company_id)
    # A domain seen for several companies (a recruiting agency) identifies none of them.
    owners = {d: ids for d, ids in owners.items() if len(ids) == 1}
    for domain, ids in owners.items():
        company = session.get(models.Company, next(iter(ids)))
        if domain not in (company.domains or []):
            company.domains = [*(company.domains or []), domain]
    known = {d: next(iter(ids)) for d, ids in owners.items()}
    for company in session.scalars(sa.select(models.Company)):
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


def _match_names(session: sa_orm.Session, emails: list[models.EmailMessage]) -> int:
    names = {}
    for company in session.scalars(sa.select(models.Company)):
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


def _thread_starts(emails: list[models.EmailMessage]) -> set[int]:
    first: dict[str, models.EmailMessage] = {}
    for e in emails:
        cur = first.get(e.gmail_thread_id)
        if cur is None or _as_utc(e.sent_at) < _as_utc(cur.sent_at):
            first[e.gmail_thread_id] = e
    return {e.id for e in first.values()}


def link_and_label(session: sa_orm.Session) -> ContactStats:
    """Link unlinked mail to companies and label linked mail without an event. Idempotent."""
    stats = ContactStats()
    emails = session.scalars(sa.select(models.EmailMessage)).all()
    _undo_ats_domains(session, emails)

    for e in emails:
        # Links set by the application/rejection import come from the email itself.
        if e.company_id is not None and e.company_match is None:
            e.company_match = EXACT
        if (
            e.detected_event is None
            and "jobs-noreply@linkedin.com" in e.from_addr.lower()
            and (viewed := _LINKEDIN_VIEWED.search(e.subject.strip()))
        ):
            _link(e, dedup.get_or_create_company(session, viewed["company"].strip()).id, EXACT)
            e.detected_event = "viewed"

    # Rejections in ATS or direct mail (LinkedIn ones are handled by linkedin.rejections).
    for e in emails:
        if e.detected_event is not None or e.direction is not models.EmailDirection.IN:
            continue
        if e.hidden_at is not None:
            continue
        if classify(e.subject, e.snippet) != "rejection":
            continue
        display, address = applications._split_sender(e.from_addr)
        domain = address.rpartition("@")[2]
        name = applications._ats_company(display, domain, e.subject) if _is_ats(domain) else None
        if e.company_id is None and name:
            _link(e, dedup.get_or_create_company(session, name).id, EXACT)
        e.detected_event = "rejection"
        if e.company_id is not None:
            rejections.mark_rejected(
                session,
                company_id=e.company_id,
                source=models.RejectionSource.GMAIL,
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
            and e.direction is models.EmailDirection.IN
            and not _is_ats(domain)
            and "noreply" not in e.from_addr.lower().replace("-", "")
        ):
            label = "recruiter_outreach"
        if label == "rejection" and e.hidden_at is None:
            rejections.mark_rejected(
                session,
                company_id=e.company_id,
                source=models.RejectionSource.GMAIL,
                rejected_at=e.sent_at,
                email_message_id=e.id,
            )
            stats.rejections += 1
        e.detected_event = label
        stats.labeled += 1
    session.commit()
    return stats


@dataclasses.dataclass(frozen=True)
class HistoryEvent:
    """One line of a company's history in the queue: mail, application, note or event."""

    at: dt.datetime
    # applied, applied?, rejection, interview, recruiter_outreach, other, ...
    kind: str
    text: str
    gmail_url: str | None
    probable: bool = False
    # reasons and notes can be deleted from the history
    note_id: int | None = None
    # so can events added by hand (#182)
    event_id: int | None = None
    # the reason's picture for ignores and notes (#195)
    icon: str = ""
    # and every other record (#225)
    application_id: int | None = None
    rejection_id: int | None = None
    email_id: int | None = None

    @property
    def ref(self) -> tuple[str, int] | None:
        """(kind, id) for delete_history_item."""
        for kind in HISTORY_KINDS:
            if (item_id := getattr(self, f"{kind}_id")) is not None:
                return kind, item_id
        return None


HISTORY_KINDS = ("note", "event", "application", "rejection", "email")


def gmail_url(thread_id: str | None) -> str | None:
    return f"https://mail.google.com/mail/u/0/#all/{thread_id}" if thread_id else None


YOUR_STATES = (
    models.OpportunityState.APPLIED,
    models.OpportunityState.INTERESTED,
    models.OpportunityState.IGNORE,
)


def other_vacancies(
    session: sa_orm.Session, company_id: int, opportunity_id: int
) -> list[tuple[models.Opportunity, dt.datetime]]:
    """The company's other vacancies you acted on, with when, newest first (#209)."""
    found = session.scalars(
        sa.select(models.Opportunity).where(
            models.Opportunity.company_id == company_id,
            models.Opportunity.id != opportunity_id,
            models.Opportunity.state.in_(YOUR_STATES),
        )
    )
    dated = [(o, _as_utc(o.state_changed_at or o.updated_at)) for o in found]
    return sorted(dated, key=lambda pair: pair[1], reverse=True)


def company_history(session: sa_orm.Session, company_id: int) -> list[HistoryEvent]:
    """Everything known about the company, newest first."""
    threads = dict(
        session.execute(
            sa.select(models.EmailMessage.id, models.EmailMessage.gmail_thread_id).where(
                models.EmailMessage.company_id == company_id
            )
        ).all()
    )
    events: list[HistoryEvent] = []
    represented: set[int] = set()
    for app in session.scalars(
        sa.select(models.Application).where(
            models.Application.company_id == company_id,
            models.Application.review != models.ApplicationReview.DISMISSED,
        )
    ):
        represented.add(app.email_message_id)
        kind = "applied" if app.review is models.ApplicationReview.CONFIRMED else "applied?"
        url = gmail_url(
            threads.get(app.email_message_id) or _thread_of(session, app.email_message_id)
        )
        events.append(
            HistoryEvent(
                _as_utc(app.applied_at),
                kind,
                app.role or "unknown role",
                url,
                application_id=app.id,
            )
        )
    for r in session.scalars(
        sa.select(models.Rejection).where(models.Rejection.company_id == company_id)
    ):
        represented.add(r.email_message_id)
        url = gmail_url(threads.get(r.email_message_id) or _thread_of(session, r.email_message_id))
        events.append(
            HistoryEvent(_as_utc(r.rejected_at), "rejection", r.role or "", url, rejection_id=r.id)
        )
    for e in session.scalars(
        sa.select(models.EmailMessage).where(models.EmailMessage.company_id == company_id)
    ):
        if e.id in represented or e.detected_event in ("application_confirmation", "rejection"):
            continue
        if e.hidden_at is not None:
            continue
        events.append(
            HistoryEvent(
                _as_utc(e.sent_at),
                e.detected_event or "email",
                e.subject,
                gmail_url(e.gmail_thread_id),
                probable=e.company_match == PROBABLE,
                email_id=e.id,
            )
        )
    for note in session.scalars(
        sa.select(models.OpportunityNote).where(models.OpportunityNote.company_id == company_id)
    ):
        kind = "note" if note.kind is models.NoteKind.NOTE else "ignored"
        events.append(
            HistoryEvent(
                _as_utc(note.created_at),
                kind,
                notes.describe(note),
                None,
                note_id=note.id,
                icon=notes.reason_icon(note.kind),
            )
        )
    for ev in session.scalars(
        sa.select(models.CompanyEvent).where(models.CompanyEvent.company_id == company_id)
    ):
        events.append(HistoryEvent(_as_utc(ev.at), ev.kind, ev.comment or "", None, event_id=ev.id))
    return sorted(events, key=lambda ev: ev.at, reverse=True)


def delete_history_item(session: sa_orm.Session, kind: str, item_id: int) -> bool:
    """Delete one record of the company history (#225). Your notes, events and rejections
    are deleted; mail stays in the database (and in Gmail) but is hidden, and an application
    is dismissed, so the next sync does not bring them back. False when there is no such record.
    """
    model = {
        "note": models.OpportunityNote,
        "event": models.CompanyEvent,
        "application": models.Application,
        "rejection": models.Rejection,
        "email": models.EmailMessage,
    }.get(kind)
    item = session.get(model, item_id) if model else None
    if item is None:
        return False
    if isinstance(item, models.Application):
        item.review = models.ApplicationReview.DISMISSED
    elif isinstance(item, models.EmailMessage):
        item.hidden_at = dt.datetime.now(dt.UTC)
    else:
        email_id = item.email_message_id if isinstance(item, models.Rejection) else None
        if email_id is not None and (email := session.get(models.EmailMessage, email_id)):
            # or the importer records it again
            email.hidden_at = dt.datetime.now(dt.UTC)
        session.delete(item)
    session.commit()
    return True


def _thread_of(session: sa_orm.Session, email_id: int | None) -> str | None:
    if email_id is None:
        return None
    return session.scalar(
        sa.select(models.EmailMessage.gmail_thread_id).where(models.EmailMessage.id == email_id)
    )
