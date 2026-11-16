"""Skills gap report: which missing technologies would open the most vacancies.

Claude's analyses list the skills each vacancy needs that the user lacks. The report
normalizes them to catalogue names, counts must-have and nice-to-have mentions, and
``best_set`` picks the few technologies that would complete the most vacancies.
``build_report`` builds it for the Skills page. See docs/skills.md (#154).

Usage:
    import job_radar.skills as skills
"""

import collections
import dataclasses
import datetime as dt
import re

import sqlalchemy as sa
import sqlalchemy.orm as sa_orm

import job_radar.models as models
import job_radar.profile.stack as profile_stack
import job_radar.scoring.description as scoring_description
import job_radar.scoring.findings as scoring_findings
import job_radar.scoring.language as scoring_language

# Gaps that are not technologies: listed apart, never "learn this".
_NON_TECH = re.compile(
    r"degree|bachelor|master|phd|диплом|образован|\byears?\b|\d+\+?\s*(лет|года|год)|опыт\s+\d"
    r"|experience as|lead|руковод|лидер|manag|менедж|english|german|french|spanish|dutch|"
    r"язык|fluent|домен|domain|fintech|финтех|industry|отрасл",
    re.IGNORECASE,
)
_HEAD = re.compile("\\s*[(:\u2014\u2013/]\\s*|\\s+-\\s+")
_BLOCKING_FORMAT = re.compile(r"\b(hybrid|on-?site)\b", re.IGNORECASE)


def normalize(raw: str) -> str | None:
    """The catalogue name of a gap, its cleaned head, or None for a non-tech gap."""
    text = " ".join(str(raw).split())
    if not text:
        return None
    for tech in profile_stack.CATALOGUE:
        if tech.search(text):
            return tech.name
    if _NON_TECH.search(text):
        return None
    head = _HEAD.split(text)[0].strip()
    return head[:40] or None


def _short(raw: str) -> str:
    head = _HEAD.split(" ".join(str(raw).split()))[0].strip()
    return head[:40]


@dataclasses.dataclass
class Example:
    """A vacancy that asks for a missing skill, with the quote from its analysis."""

    opportunity_id: int
    title: str
    company: str
    quote: str


@dataclasses.dataclass
class SkillRow:
    """One missing technology in the report: how often it is a must or a plus, and examples."""

    name: str
    must: int = 0
    nice: int = 0
    unlock: int = 0
    level: str = "unknown"
    scores: list[float] = dataclasses.field(default_factory=list)
    examples: list[Example] = dataclasses.field(default_factory=list)

    @property
    def avg_score(self) -> float | None:
        return round(sum(self.scores) / len(self.scores), 1) if self.scores else None


@dataclasses.dataclass
class Report:
    """The whole skills gap report shown on the Skills page."""

    rows: list[SkillRow]
    non_tech: collections.Counter
    vacancies: int
    best: dict[int, list[str]]
    best_unlocks: dict[int, int]


def _ignored_for_stack(session: sa_orm.Session, opportunity: models.Opportunity) -> bool:
    note = session.scalar(
        sa.select(models.OpportunityNote)
        .where(models.OpportunityNote.opportunity_id == opportunity.id)
        .order_by(models.OpportunityNote.id.desc())
        .limit(1)
    )
    return note is not None and note.kind is models.NoteKind.STACK


def _latest_claude(session: sa_orm.Session, opportunity_id: int) -> models.ClaudeAnalysis | None:
    return session.scalar(
        sa.select(models.ClaudeAnalysis)
        .where(
            models.ClaudeAnalysis.opportunity_id == opportunity_id,
            models.ClaudeAnalysis.state == "done",
        )
        .order_by(models.ClaudeAnalysis.id.desc())
        .limit(1)
    )


def best_set(missing: list[set[str]], size: int) -> list[str]:
    """Greedily pick up to ``size`` technologies that complete the most missing sets."""
    chosen: list[str] = []
    candidates = set().union(*missing) if missing else set()
    for _ in range(size):

        def opened(extra: str) -> int:
            have = set(chosen) | {extra}
            return sum(1 for m in missing if m and m <= have)

        def partial(extra: str) -> int:
            return sum(1 for m in missing if extra in m)

        left = candidates - set(chosen)
        if not left:
            break
        chosen.append(max(sorted(left), key=lambda t: (opened(t), partial(t))))
    return chosen


def build_report(
    session: sa_orm.Session,
    profile: profile_stack.Profile | None,
    *,
    days: int | None = None,
    everything: bool = False,
) -> Report:
    """``everything``: all analysed vacancies (market demand), not only the ones still open."""
    profile = profile or profile_stack.Profile()
    since = dt.datetime.now(dt.UTC) - dt.timedelta(days=days) if days else None
    rows: dict[str, SkillRow] = {}
    non_tech: collections.Counter = collections.Counter()
    unlockable: list[set[str]] = []
    counted = 0

    def row(name: str) -> SkillRow:
        if name not in rows:
            rows[name] = SkillRow(name=name, level=profile.level(name))
        return rows[name]

    states = (
        models.OpportunityState.NEW,
        models.OpportunityState.INTERESTED,
        models.OpportunityState.IGNORE,
    )
    query = (
        sa.select(models.Opportunity)
        if everything
        else sa.select(models.Opportunity).where(models.Opportunity.state.in_(states))
    )
    for o in session.scalars(query):
        if (
            not everything
            and o.state is models.OpportunityState.IGNORE
            and not _ignored_for_stack(session, o)
        ):
            continue
        created = o.created_at if o.created_at.tzinfo else o.created_at.replace(tzinfo=dt.UTC)
        if since and created < since:
            continue
        must: dict[str, str] = {}
        nice: dict[str, str] = {}
        blocked = False
        claude = _latest_claude(session, o.id)
        if claude and isinstance(claude.result, dict):
            result = claude.result
            if (result.get("location") or {}).get("verdict") == "blocker":
                blocked = True
            for g in result.get("gaps") or []:
                if not isinstance(g, dict):
                    continue
                name = normalize(g.get("skill", ""))
                if name is None:
                    non_tech[_short(g.get("skill", ""))] += 1
                    blocked = blocked or bool(g.get("blocker"))
                    continue
                if profile.level(name) == "known":
                    continue
                target = must if g.get("requirement") == "must_have" else nice
                target.setdefault(name, str(g.get("quote") or ""))
        analysis = scoring_findings.latest_analysis(o)
        if analysis is not None:
            if analysis.work_mode in ("hybrid", "onsite") or scoring_findings.residence_restriction(
                analysis
            ):
                blocked = True
            for item in analysis.stack or []:
                if item.get("status") != "required":
                    continue
                name = normalize(item["skill"]) or item["skill"]
                if profile.level(name) != "known":
                    must.setdefault(name, str(item.get("quote") or ""))
        if scoring_language.vacancy_language(o.title, scoring_description.latest_description(o)):
            blocked = True
        if _BLOCKING_FORMAT.search(o.location or ""):
            blocked = True
        for name in must:
            nice.pop(name, None)
        if not must and not nice:
            continue
        counted += 1
        for name, quote in must.items():
            r = row(name)
            r.must += 1
            if o.score is not None:
                r.scores.append(o.score)
            r.examples.append(Example(o.id, o.title, o.company.name, quote))
        for name in nice:
            row(name).nice += 1
        if not blocked and must:
            unlockable.append(set(must))
            if len(must) == 1:
                row(next(iter(must))).unlock += 1
    ordered = sorted(rows.values(), key=lambda r: (-r.unlock, -r.must, -r.nice, r.name))
    best = {k: best_set(unlockable, k) for k in (1, 2, 3)}
    best_unlocks = {k: sum(1 for m in unlockable if m <= set(chosen)) for k, chosen in best.items()}
    return Report(ordered, non_tech, counted, best, best_unlocks)


SORTS = {
    "name": lambda r: r.name.casefold(),
    "must": lambda r: r.must,
    "nice": lambda r: r.nice,
    "unlock": lambda r: r.unlock,
    "avg": lambda r: r.avg_score if r.avg_score is not None else float("-inf"),
    "level": lambda r: r.level,
}


def sort_rows(rows: list[SkillRow], sort: str, direction: str) -> list[SkillRow]:
    """Sort by a column (#156); ties keep the default order (unlock, must, name)."""
    key = SORTS.get(sort, SORTS["unlock"])
    base = sorted(rows, key=lambda r: (-r.unlock, -r.must, r.name))
    return sorted(base, key=key, reverse=direction != "asc")
