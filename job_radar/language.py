"""Guess the language of a vacancy title, offline and rule-based.

English and Russian are fine for the user; any other language gives a risk. See
docs/applications.md#vacancy-language.
"""

import re

from .scoring import Reason

LANGUAGE_PENALTY = -3.0
MIN_EVIDENCE = 2  # a single weak hint (one accented letter, "para") is not enough

# Non-Latin scripts, checked in this order (Japanese uses Han characters too).
_SCRIPTS = [
    ("Japanese", re.compile(r"[぀-ヿ]")),
    ("Korean", re.compile(r"[가-힯]")),
    ("Chinese", re.compile(r"[一-鿿]")),
    ("Arabic", re.compile(r"[؀-ۿ]")),
    ("Hebrew", re.compile(r"[֐-׿]")),
    ("Thai", re.compile(r"[฀-๿]")),
    ("Greek", re.compile(r"[Ͱ-Ͽ]")),
]
_CYRILLIC = re.compile(r"[Ѐ-ӿ]")

# Words that name the role or the job in that language (2 points each) ...
_STRONG = {
    "Spanish": {"desarrollador", "desarrolladora", "ingeniero", "ingeniera", "programador",
                "programadora", "certificación", "empleo"},
    "Portuguese": {"desenvolvedor", "desenvolvedora", "engenheiro", "engenheira", "pleno", "vaga"},
    "French": {"développeur", "développeuse", "developpeur", "ingénieur", "ingenieur", "logiciel",
               "confirmé", "télétravail", "h/f", "f/h"},
    "German": {"entwickler", "entwicklerin", "schwerpunkt", "werkstudent", "werkstudentin",
               "leitung", "teamleiter", "bereich", "mitarbeiter"},
    "Italian": {"sviluppatore", "sviluppatrice", "ingegnere", "programmatore"},
    "Polish": {"programista", "programistka", "inżynier", "zdalnie", "zdalna", "praca"},
    "Dutch": {"ontwikkelaar", "medior", "vacature"},
}  # fmt: skip
# ... and common small words (1 point each).
_WEAK = {
    "Spanish": {"para", "con", "del", "remoto"},
    "Portuguese": {"para", "com", "remoto"},
    "French": {"pour", "avec", "des"},
    "German": {"und", "für", "mit", "plattform"},
    "Italian": {"per", "remoto"},
    "Polish": {"dla"},
    "Dutch": {"voor", "met"},
}
_LETTERS = {
    "Spanish": "ñáíóú",
    "Portuguese": "ãõçê",
    "French": "çéèêàâôûîïë",
    "German": "ßäöü",
    "Polish": "łżśźćńąę",
}
# German compounds: Softwareentwickler, Backendentwickler, ...
_GERMAN_COMPOUND = re.compile(r"entwickler")


def _latin_language(title: str) -> str | None:
    text = title.casefold()
    tokens = set(re.findall(r"[^\W\d_]+(?:/[^\W\d_]+)?", text))
    scores: dict[str, int] = {}
    for language, words in _STRONG.items():
        scores[language] = 2 * len(tokens & words)
    for language, words in _WEAK.items():
        scores[language] += len(tokens & words)
    for language, letters in _LETTERS.items():
        scores[language] += int(any(ch in text for ch in letters))
    if any(_GERMAN_COMPOUND.search(t) for t in tokens):
        scores["German"] += 2
    best = max(scores, key=scores.get)
    ranked = sorted(scores.values(), reverse=True)
    if ranked[0] >= MIN_EVIDENCE and ranked[0] > ranked[1]:
        return best
    return None


def detect_title_language(title: str) -> str | None:
    """The title's language when it is neither English nor Russian, else None."""
    for language, pattern in _SCRIPTS:
        if pattern.search(title):
            return language
    if _CYRILLIC.search(title):
        return None  # Russian is fine
    return _latin_language(title)


def language_risk(title: str) -> tuple[float, Reason] | None:
    language = detect_title_language(title)
    if language is None:
        return None
    return LANGUAGE_PENALTY, Reason("-", f"Title in {language}: the job likely needs {language}")
