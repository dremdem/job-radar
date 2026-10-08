"""Guess the language of a vacancy title and description, offline and rule-based.

English and Russian are fine for the user; any other language gives a risk. See
docs/applications.md#vacancy-language.
"""

import re
from functools import lru_cache

from .scoring import Reason

LANGUAGE_PENALTY = -8.0  # a hard blocker (#115)
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


# Common small words of long text, for descriptions (docs/applications.md#vacancy-language).
_COMMON = {
    "English": "the and to of in for with you we our is are will be your this on as",
    "German": "und der die das mit für wir sie ist zu den von ein eine bei du dich dir auf",
    "Spanish": "de la el y en para con que los las una por del se tu nuestro",
    "French": "et le la les des pour avec vous nous une est dans du au sur",
    "Portuguese": "de e para com que os as uma em do da no na você nosso",
    "Italian": "di e il la per con che una sono del della nel gli le",
    "Polish": "i w z na do jest się oraz dla jak przez",
    "Dutch": "en de het van voor met een wij je zijn bij ons",
}
_COMMON_SETS = {language: set(words.split()) for language, words in _COMMON.items()}
_VIETNAMESE = re.compile(r"[ạảấầẩẫậắằẳẵặẹẻẽếềểễệỉịọỏốồổỗộớờởỡợụủứừửữựỳỵỷỹđ]", re.IGNORECASE)
MIN_SHARE = 0.05  # of all words
SCRIPT_SHARE = 0.3  # of all letters


def detect_text_language(text: str) -> str | None:
    """The language of a long text when it is neither English nor Russian, else None."""
    letters = re.findall(r"[^\W\d_]", text)
    if not letters:
        return None
    for language, pattern in [*_SCRIPTS, ("Russian", _CYRILLIC)]:
        if len(pattern.findall(text)) > SCRIPT_SHARE * len(letters):
            return None if language == "Russian" else language
    words = re.findall(r"[^\W\d_]+", text.casefold())
    if len(_VIETNAMESE.findall(text)) > 0.05 * len(letters):
        return "Vietnamese"
    shares = {lang: sum(w in s for w in words) / len(words) for lang, s in _COMMON_SETS.items()}
    english = shares.pop("English")
    best = max(shares, key=shares.get)
    if shares[best] >= MIN_SHARE and shares[best] > 2 * english:
        return best
    return None


# Required languages in an English text (#115). Name -> language; English / Russian are fine.
_NAMES = {
    "german": "German", "deutsch": "German", "dutch": "Dutch", "french": "French",
    "français": "French", "francais": "French", "spanish": "Spanish", "español": "Spanish",
    "italian": "Italian", "portuguese": "Portuguese", "polish": "Polish", "swedish": "Swedish",
    "danish": "Danish", "norwegian": "Norwegian", "finnish": "Finnish", "czech": "Czech",
    "slovak": "Slovak", "hungarian": "Hungarian", "romanian": "Romanian", "greek": "Greek",
    "turkish": "Turkish", "hebrew": "Hebrew", "arabic": "Arabic", "japanese": "Japanese",
    "korean": "Korean", "chinese": "Chinese", "mandarin": "Chinese", "cantonese": "Chinese",
    "vietnamese": "Vietnamese", "thai": "Thai", "hindi": "Hindi", "ukrainian": "Ukrainian",
}  # fmt: skip
_NAME = re.compile(r"(?<![\w-])(" + "|".join(_NAMES) + r")(?=[\W_]|kenntnis|$)", re.IGNORECASE)
_NEEDED = re.compile(
    r"fluen|proficien|native|required|requirement|mandatory|\bmust\b|\bspeak|written and"
    r"|\b[BC][12]\b|fließend|fliessend|kenntnisse|sehr gut",
    re.IGNORECASE,
)
_OPTIONAL = re.compile(
    r"\bplus\b|nice[ -]to[ -]have|advantage|\bbonus\b|prefer|desirable|helpful|beneficial"
    r"|not (be )?(required|a must|mandatory|needed)|\bclubs?\b|von vorteil|wünschenswert",
    re.IGNORECASE,
)
_SENTENCE = re.compile(r"[^.!?\n]+[.!?]?")


def required_language(text: str) -> tuple[str, str] | None:
    """(language, sentence) for the first sentence that asks for another language."""
    for match in _SENTENCE.finditer(text):
        sentence = " ".join(match.group(0).split())
        names = [_NAMES[m.group(1).casefold()] for m in _NAME.finditer(sentence)]
        if names and _NEEDED.search(sentence) and not _OPTIONAL.search(sentence):
            return names[0], sentence.strip(" *•-·")
    return None


def vacancy_language(title: str, description: str | None) -> str | None:
    """Language other than English or Russian, from the title first, then the description."""
    if found := detect_title_language(title):
        return found
    if not description:
        return None
    required = required_language(description)
    return detect_text_language(description) or (required[0] if required else None)


@lru_cache(maxsize=8192)  # pure text check, run for every vacancy in a list (#180)
def language_risk(title: str, description: str | None = None) -> tuple[float, Reason] | None:
    if language := detect_title_language(title):
        return LANGUAGE_PENALTY, Reason(
            "-", f"Title in {language}: the job likely needs {language}"
        )
    if description and (language := detect_text_language(description)):
        return LANGUAGE_PENALTY, Reason(
            "-", f"Description in {language}: the job likely needs {language}"
        )
    if description and (required := required_language(description)):
        language, sentence = required
        shown = sentence if len(sentence) <= 160 else sentence[:159] + "…"
        return LANGUAGE_PENALTY, Reason("-", f"Requires {language} — “{shown}”")
    return None
