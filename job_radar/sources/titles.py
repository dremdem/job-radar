"""Title and keyword filters shared by the sources.

``DEFAULT_KEYWORDS`` is the technology a vacancy must mention, ``DEFAULT_TITLE`` the
developer-type roles a title must name and ``DEFAULT_EXCLUDE_TITLE`` the titles that are
measured noise in the Telegram channels. ``BOARD_EXCLUDE_TITLE`` adds the noise of the job
boards. The Telegram allowlist uses them as the defaults of each channel. See
docs/telegram.md and docs/himalayas.md.

Usage:
    import job_radar.sources.titles as sources_titles
"""

DEFAULT_KEYWORDS = r"python|django|fastapi|flask"
# The vacancy title must name a developer-type role ...
DEFAULT_TITLE = (
    r"engineer|developer|разработчик|программист|programmer|architect|архитектор|devops|\bsre\b|"
    r"\bml\b|\bai\b|\blead\b|лид|\bcto\b|back-?end|бэкенд|бекенд|python"
)
# ... and not one of these (measured noise in the real channels, see docs/telegram.md).
DEFAULT_EXCLUDE_TITLE = (
    r"analyst|аналитик|manager|менеджер|\bqa\b|\btest|тестиров|recruit|рекрут|designer|дизайн|"
    r"sales|продаж|marketing|маркетолог|support|поддержк|intern\b|стажер|стажёр|дайджест|digest|подборк"
)
# Measured noise of the job boards (docs/himalayas.md), on top of the Telegram title filter.
BOARD_EXCLUDE_TITLE = (
    DEFAULT_EXCLUDE_TITLE + r"|secur|appsec|scientist|research|mathematic|physicist|"
    r"civil|structural|aerodynam|control system"
)
