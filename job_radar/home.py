"""Where the user lives: the profile's ``home`` and ``home_regions``.

Scoring, the local model's residence check and Claude's prompt read the home country and
the regions that include it from here, so no country is written into the code.
``current_home`` re-reads the profile only when the file changes and returns an empty
``Home`` without a profile. See docs/ai.md#stack-vs-your-cv.

Usage:
    import job_radar.home as home
"""

import dataclasses
import functools
import pathlib
import re

import job_radar.config as config
import job_radar.stack as stack


@dataclasses.dataclass(frozen=True)
class Home:
    """The user's home country and the regions that include it; empty without a profile."""

    # e.g. "Thailand"; empty = not stated
    country: str = ""
    # wider areas that include it, e.g. ("Asia", "APAC")
    regions: tuple[str, ...] = ()

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


@functools.lru_cache(maxsize=1)
def _profile_path() -> pathlib.Path:
    return config.Settings().profile


def current_home() -> Home:
    """Home from the profile file, re-read when it changes; empty without a profile."""
    profile = stack.current_profile(_profile_path())
    if profile is None:
        return Home()
    return Home(profile.home, tuple(profile.home_regions))
