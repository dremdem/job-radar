"""Whether a posting on an employer's board is the vacancy from a LinkedIn alert.

The title only finds candidates: ``same_title`` compares titles after
``normalize_title`` (case, punctuation, "Sr."/"Senior"). Two teams can post the same
title in one city, so ``choose`` attaches a posting only when exactly one candidate has a
compatible location and no other candidate's location is unknown; anything less certain is
``ambiguous`` with its candidates, for the user to decide. ``location_compatible`` returns
True, False or None (unknown): a missing location is never taken as a match. See
docs/ats.md#matching.

Usage:
    import job_radar.sources.ats.matching as ats_matching
"""

import dataclasses
import re

import job_radar.sources.ats.boards as ats_boards

_WORDS = {"sr": "senior", "jr": "junior", "snr": "senior", "eng": "engineer", "dev": "developer"}
# words that say nothing about where: work modes and filler
_NOT_PLACES = {
    "remote", "hybrid", "on", "site", "onsite", "office", "in", "the", "of", "and", "or",
    "area", "metropolitan", "metro", "greater", "anywhere", "fully", "work", "from", "home",
    "region", "time", "zone", "zones", "only", "based", "new", "san", "st", "united",
    "city", "county", "state", "republic", "north", "south", "east", "west", "central",
}  # fmt: skip
# short names that would not overlap with the long ones
_PLACES = {"us": "states", "usa": "states", "uk": "kingdom", "gb": "kingdom", "uae": "emirates",
           "nyc": "york", "sf": "francisco", "eu": "europe"}  # fmt: skip


def normalize_title(title: str) -> str:
    words = re.sub(r"[^\w]+", " ", title.lower()).split()
    return " ".join(_WORDS.get(w, w) for w in words)


def same_title(a: str, b: str) -> bool:
    return bool(normalize_title(a)) and normalize_title(a) == normalize_title(b)


def _places(location: str) -> set[str]:
    words = re.sub(r"[^\w]+", " ", location.lower()).split()
    return {_PLACES.get(w, w) for w in words if w not in _NOT_PLACES and not w.isdigit()}


def _remote(location: str) -> bool:
    return "remote" in location.lower() or "anywhere" in location.lower()


def location_compatible(
    alert: str | None, posting: str | None, posting_remote: bool | None = None
) -> bool | None:
    """True when they name a common place, or both are remote and one names no place;
    False when they differ; None when either location is missing (unknown)."""
    if not alert or not posting:
        return None
    a, p = _places(alert), _places(posting)
    if a & p:
        return True
    remote_posting = _remote(posting) or posting_remote is True
    return bool(_remote(alert) and remote_posting and (not a or not p))


@dataclasses.dataclass(frozen=True)
class Decision:
    """The verdict for one vacancy: ``matched`` with its posting, ``ambiguous`` with the
    candidates, or ``no_match`` (candidates with the title but another location, if any)."""

    status: str
    posting: ats_boards.Posting | None = None
    candidates: tuple[ats_boards.Posting, ...] = ()


def choose(title: str, location: str | None, postings: list[ats_boards.Posting]) -> Decision:
    candidates = [p for p in postings if same_title(title, p.title)]
    if not candidates:
        return Decision("no_match")
    verdicts = [(p, location_compatible(location, p.location, p.remote)) for p in candidates]
    yes = [p for p, v in verdicts if v is True]
    unknown = [p for p, v in verdicts if v is None]
    if len(yes) == 1 and not unknown:
        return Decision("matched", yes[0], tuple(candidates))
    if not yes and not unknown:
        return Decision("no_match", candidates=tuple(candidates))
    return Decision("ambiguous", candidates=tuple(yes + unknown))
