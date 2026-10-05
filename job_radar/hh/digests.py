"""Vacancies from hh.ru «Вакансии по подписке» digests into the queue. See docs/hh.md#digests."""

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime

from bs4 import BeautifulSoup, Tag
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..dedup import get_or_create_company
from ..himalayas import EXCLUDE_TITLE
from ..linkedin.rejections import FetchBody
from ..models import EmailMessage, Opportunity, SourceRecord, SourceType
from ..scoring import score_job
from ..telegram.sources import DEFAULT_TITLE
from .notifications import DIGEST_EVENT, SENDER, VACANCY_URL

DONE_EVENT = "hh_digest_done"
_VACANCY = re.compile(r"hh\.ru/vacancy/(\d+)")
_OPEN = re.compile(r"посмотреть вакансию|перейти к вакансии", re.IGNORECASE)
_SALARY = re.compile(r"₽|\$|€|за (месяц|час|смену|день)|^от$|^до$|^\d[\d\s]*$", re.IGNORECASE)
# On top of the usual title filter: what this search brings besides developers.
# The Himalayas noise filter (security, research, maths, ...) and what hh.ru adds to it.
_EXCLUDE = EXCLUDE_TITLE + (
    r"|преподават|наставник|репетитор|учител|автор|ментор|teacher|tutor|математик"
    r"|юрист|бухгалт|безопасност|офис"
)
_INVISIBLE = re.compile("[​-‍⠀͏﻿ ]")


@dataclass(frozen=True)
class DigestItem:
    vacancy_id: str
    title: str
    company: str
    city: str | None
    salary: str | None


def _clean(text: str) -> str:
    return " ".join(_INVISIBLE.sub(" ", text).split())


def _block(anchor: Tag, vacancy_id: str) -> Tag:
    """Largest ancestor that mentions this vacancy only."""
    node = anchor
    while node.parent is not None:
        ids = set(_VACANCY.findall(str(node.parent)))
        if ids - {vacancy_id}:
            break
        node = node.parent
    return node


def parse_digest(html: str) -> list[DigestItem]:
    soup = BeautifulSoup(html, "html.parser")
    items: list[DigestItem] = []
    seen: set[str] = set()
    for anchor in soup.find_all("a", href=True):
        match = _VACANCY.search(anchor["href"])
        title = _clean(anchor.get_text(" "))
        if not match or not title or _OPEN.search(title) or match.group(1) in seen:
            continue
        vacancy_id = match.group(1)
        seen.add(vacancy_id)
        strings = [_clean(s) for s in _block(anchor, vacancy_id).stripped_strings]
        strings = [s for s in strings if s]
        after = strings[strings.index(title) + 1 :] if title in strings else []
        # Only up to "Посмотреть вакансию": the last block would run into the mail's footer.
        end = next((i for i, s in enumerate(after) if _OPEN.search(s)), len(after))
        after = after[:end]
        salary_parts = [s for s in after if _SALARY.search(s)]
        rest = [s for s in after if s not in salary_parts]
        employer = rest[-1] if rest else ""
        parts = [p.strip() for p in employer.split(",")]
        parts = [p for p in parts if p]
        cut = next((i for i, p in enumerate(parts) if p.startswith("м.")), len(parts))
        parts = parts[:cut]
        items.append(
            DigestItem(
                vacancy_id=vacancy_id,
                title=title,
                company=parts[0] if parts else "",
                city=parts[1] if len(parts) > 1 else None,
                salary=" ".join(salary_parts) or None,
            )
        )
    return items


def wanted(title: str) -> bool:
    return bool(re.search(DEFAULT_TITLE, title, re.IGNORECASE)) and not re.search(
        _EXCLUDE, title, re.IGNORECASE
    )


@dataclass
class DigestStats:
    digests: int = 0
    vacancies: int = 0
    known: int = 0
    skipped: int = 0
    fetched: int = 0
    pending: int = 0  # wait for Gmail access


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _add(session: Session, item: DigestItem) -> None:
    from ..auto_ignore import apply_rules

    location = f"Remote ({item.city})" if item.city else "Remote"
    score = score_job(item.title, location)
    opportunity = Opportunity(
        title=item.title[:255],
        company=get_or_create_company(session, item.company or f"Unknown (hh {item.vacancy_id})"),
        location=location,
        salary=item.salary,
        score=score.value,
        score_reasons=[r.as_dict() for r in score.reasons],
    )
    session.add(
        SourceRecord(
            source_type=SourceType.HH,
            external_id=item.vacancy_id,
            url=VACANCY_URL.format(id=item.vacancy_id),
            opportunity=opportunity,
        )
    )
    session.flush()
    session.refresh(opportunity)
    apply_rules(session, opportunity)


def import_hh_digests(
    session: Session, fetch_body: FetchBody | None, *, since: date | None
) -> DigestStats:
    """Digests received on or after ``since`` (the switch to the remote search)."""
    stats = DigestStats()
    if since is None:
        return stats
    start = datetime(since.year, since.month, since.day, tzinfo=UTC)
    emails = session.scalars(
        select(EmailMessage)
        .where(
            EmailMessage.from_addr.like(f"%{SENDER}%"),
            EmailMessage.detected_event == DIGEST_EVENT,
        )
        .order_by(EmailMessage.sent_at)
    ).all()
    for email in emails:
        if _as_utc(email.sent_at) < start:
            continue  # the earlier office search
        if fetch_body is None:
            stats.pending += 1
            continue
        html, _ = fetch_body(email.gmail_message_id)
        stats.fetched += 1
        for item in parse_digest(html or ""):
            if not wanted(item.title):
                stats.skipped += 1
                continue
            known = session.scalar(
                select(SourceRecord.id).where(
                    SourceRecord.source_type == SourceType.HH,
                    SourceRecord.external_id == item.vacancy_id,
                )
            )
            if known:
                stats.known += 1
                continue
            _add(session, item)
            stats.vacancies += 1
        email.detected_event = DONE_EVENT
        stats.digests += 1
        session.commit()
    return stats
