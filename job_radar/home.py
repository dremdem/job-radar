"""Where the user lives: Profile ``home`` and ``home_regions`` (docs/ai.md#stack-vs-your-cv).

Scoring, the local model's residence check and Claude's prompt read it from here, so no
country is written into the code.
"""

import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from .config import Settings
from .stack import current_profile


@dataclass(frozen=True)
class Home:
    country: str = ""  # e.g. "Thailand"; empty = not stated
    regions: tuple[str, ...] = ()  # wider areas that include it, e.g. ("Asia", "APAC")

    @property
    def places(self) -> tuple[str, ...]:
        return tuple(p for p in (self.country, *self.regions) if p)

    def listed_in(self, text: str | None) -> str | None:
        """The first of our places named in ``text`` as a whole word, ignoring case."""
        for place in self.places:
            if text and re.search(rf"(?<!\w){re.escape(place)}(?!\w)", text, re.IGNORECASE):
                return place
        return None

    @property
    def where(self) -> str:
        """For messages: "Thailand" or a neutral phrase when the country is not set."""
        return self.country or "your country"


@lru_cache(maxsize=1)
def _profile_path() -> Path:
    return Settings().profile


def current_home() -> Home:
    """Home from the profile file, re-read when it changes; empty without a profile."""
    profile = current_profile(_profile_path())
    if profile is None:
        return Home()
    return Home(profile.home, tuple(profile.home_regions))
