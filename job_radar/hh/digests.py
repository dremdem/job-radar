"""Vacancies from hh.ru «Вакансии по подписке» digests into the queue.

``parse_digest`` reads the vacancies of one digest email (id, title, company, city,
salary), ``wanted`` filters them, and ``import_hh_digests`` creates vacancies for new
ones from digests received after the switch to the remote search. Bodies are fetched from
Gmail when needed. See docs/hh.md#digests.

Usage:
    import job_radar.hh.digests as hh_digests
"""

import dataclasses
import datetime as dt
import re

import bs4
import sqlalchemy as sa
import sqlalchemy.orm as sa_orm

import job_radar.dedup as dedup
import job_radar.hh.notifications as hh_notifications
import job_radar.mail as mail
import job_radar.models as models
import job_radar.scoring.rules as scoring_rules
import job_radar.sources.titles as sources_titles

DONE_EVENT = "hh_digest_done"
_VACANCY = re.compile(r"hh\.ru/vacancy/(\d+)")
_OPEN = re.compile(r"посмотреть вакансию|перейти к вакансии", re.IGNORECASE)
_SALARY = re.compile(r"₽|\$|€|за (месяц|час|смену|день)|^от$|^до$|^\d[\d\s]*$", re.IGNORECASE)
# On top of the usual title filter: what this search brings besides developers.
# The Himalayas noise filter (security, research, maths, ...) and what hh.ru adds to it.
_EXCLUDE = sources_titles.BOARD_EXCLUDE_TITLE + (
    r"|преподават|наставник|репетитор|учител|автор|ментор|teacher|tutor|математик"
    r"|юрист|бухгалт|безопасност|офис"
)
_INVISIBLE = re.compile("[​-‍⠀͏﻿ ]")


@dataclasses.dataclass(frozen=True)
class DigestItem:
    """One vacancy in an hh.ru digest email."""

    vacancy_id: str
    title: str
    company: str
    city: str | None
    salary: str | None


def _clean(text: str) -> str:
    return " ".join(_INVISIBLE.sub(" ", text).split())


def _block(anchor: bs4.Tag, vacancy_id: str) -> bs4.Tag:
    """Largest ancestor that mentions this vacancy only."""
    node = anchor
    while node.parent is not None:
        ids = set(_VACANCY.findall(str(node.parent)))
        if ids - {vacancy_id}:
            break
        node = node.parent
    return node


def parse_digest(html: str) -> list[DigestItem]:
    soup = bs4.BeautifulSoup(html, "html.parser")
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
    return bool(re.search(sources_titles.DEFAULT_TITLE, title, re.IGNORECASE)) and not re.search(
        _EXCLUDE, title, re.IGNORECASE
    )


@dataclasses.dataclass
class DigestStats:
    """Counters of one digest import, for the sync status line."""

    digests: int = 0
    vacancies: int = 0
    known: int = 0
    skipped: int = 0
    fetched: int = 0
    # wait for Gmail access
    pending: int = 0
    # deleted from Gmail before the body was read
    gone: int = 0


def _as_utc(value: dt.datetime) -> dt.datetime:
    return value if value.tzinfo else value.replace(tzinfo=dt.UTC)


def _add(session: sa_orm.Session, item: DigestItem) -> None:
    import job_radar.review.auto_ignore as review_auto_ignore

    location = f"Remote ({item.city})" if item.city else "Remote"
    score = scoring_rules.score_job(item.title, location)
    opportunity = models.Opportunity(
        title=item.title[:255],
        company=dedup.get_or_create_company(
            session, item.company or f"Unknown (hh {item.vacancy_id})"
        ),
        location=location,
        salary=item.salary,
        score=score.value,
        score_reasons=[r.as_dict() for r in score.reasons],
    )
    session.add(
        models.SourceRecord(
            source_type=models.SourceType.HH,
            external_id=item.vacancy_id,
            url=hh_notifications.VACANCY_URL.format(id=item.vacancy_id),
            opportunity=opportunity,
        )
    )
    session.flush()
    session.refresh(opportunity)
    review_auto_ignore.apply_rules(session, opportunity)


def import_hh_digests(
    session: sa_orm.Session,
    fetch_body: mail.FetchBody | None,
    *,
    since: dt.date | None,
) -> DigestStats:
    """Digests received on or after ``since`` (the switch to the remote search)."""
    stats = DigestStats()
    if since is None:
        return stats
    start = dt.datetime(since.year, since.month, since.day, tzinfo=dt.UTC)
    emails = session.scalars(
        sa.select(models.EmailMessage)
        .where(
            models.EmailMessage.from_addr.like(f"%{hh_notifications.SENDER}%"),
            models.EmailMessage.detected_event == hh_notifications.DIGEST_EVENT,
        )
        .order_by(models.EmailMessage.sent_at)
    ).all()
    for email in emails:
        if _as_utc(email.sent_at) < start:
            # the earlier office search
            continue
        if fetch_body is None:
            stats.pending += 1
            continue
        try:
            html, _ = fetch_body(email.gmail_message_id)
        except mail.MessageGone:
            email.detected_event = mail.GONE_EVENT
            stats.gone += 1
            session.commit()
            continue
        stats.fetched += 1
        for item in parse_digest(html or ""):
            if not wanted(item.title):
                stats.skipped += 1
                continue
            known = session.scalar(
                sa.select(models.SourceRecord.id).where(
                    models.SourceRecord.source_type == models.SourceType.HH,
                    models.SourceRecord.external_id == item.vacancy_id,
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
