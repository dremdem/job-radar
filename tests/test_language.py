import pytest

from job_radar.language import (
    detect_text_language,
    detect_title_language,
    language_risk,
    required_language,
    vacancy_language,
)


@pytest.mark.parametrize(
    ("title", "language"),
    [
        # Real titles from the queue.
        ("Desarrollador/a Backend | Certificación APX | 100% Remoto", "Spanish"),
        ("Python Backend Developer (m/w/d) - Schwerpunkt KI-Plattform", "German"),
        ("全栈高级工程师", "Chinese"),
        (
            "【リモート可】ソフトウェアエンジニア／要件定義～開発・運用◇AI実装スキル習得◎グロース上場",
            "Japanese",
        ),
        # Typical ones.
        ("Développeur Python H/F", "French"),
        ("Ingénieur logiciel backend confirmé", "French"),
        ("Desenvolvedor Python Pleno", "Portuguese"),
        ("Softwareentwickler Python (m/w/d)", "German"),
        ("Sviluppatore Python Senior", "Italian"),
        ("Programista Python - praca zdalna", "Polish"),
        ("Python Ontwikkelaar", "Dutch"),
    ],
)
def test_detects_non_english_titles(title, language):
    assert detect_title_language(title) == language


@pytest.mark.parametrize(
    "title",
    [
        "Senior Python Backend Engineer",
        "Backend Software Engineer - AI Systems (m/w/d)",
        "Backend Developer — Apache Fineract",
        "🚨Urgent Hiring | Senior Data Engineer - Data bricks / Pyspark | Immediate Joiners Only",
        "🟠Senior Python Developer🟠",
        "Senior Software Engineer – IT Banking & Payment Operations",
        "Python-разработчик (Senior)",  # Russian is fine
        "Senior Engineer, Remote (Brazil)",
        "Café Platform Engineer",  # one accented letter is not a language
    ],
)
def test_english_russian_and_noise_are_fine(title):
    assert detect_title_language(title) is None


def test_language_risk():
    points, reason = language_risk("Desarrollador Python Senior")
    assert points == -8.0
    assert reason.sign == "-"
    assert reason.text == "Title in Spanish: the job likely needs Spanish"
    assert language_risk("Senior Python Developer") is None


GERMAN = (
    "Wir suchen einen erfahrenen Backend-Entwickler für unser Team in Berlin. "
    "Du arbeitest mit Python und Django an der Plattform, die von unseren Kunden "
    "jeden Tag genutzt wird. Wir bieten dir eine faire Bezahlung und ein tolles Team."
)
ENGLISH_WITH_GERMAN_WORDS = (
    "We are looking for a backend engineer to join our team in Munich. You will work "
    "with Python and the Plattform team. Nice to have: German (Deutsch) at B1 level."
)
SPANISH = (
    "Buscamos un desarrollador backend para trabajar en remoto con el equipo de "
    "producto. Las tareas incluyen el diseño de la API y la integración con los sistemas."
)
RUSSIAN = "Ищем опытного Python-разработчика в команду платформы. Удалённая работа, гибкий график."
CHINESE = "我们正在寻找一名高级后端工程师，负责设计和开发分布式系统。熟悉 Python 和 Go。"
VIETNAMESE = (
    "Chúng tôi đang tìm kiếm kỹ sư phần mềm có kinh nghiệm với Python để phát triển "
    "sản phẩm AI. Làm việc từ xa 100%, môi trường thân thiện."
)


@pytest.mark.parametrize(
    ("text", "language"),
    [
        (GERMAN, "German"),
        (SPANISH, "Spanish"),
        (CHINESE, "Chinese"),
        (VIETNAMESE, "Vietnamese"),
        (ENGLISH_WITH_GERMAN_WORDS, None),
        (RUSSIAN, None),
        ("", None),
    ],
)
def test_detect_text_language(text, language):
    assert detect_text_language(text) == language


def test_description_language_risk():
    points, reason = language_risk("Senior Python Developer", GERMAN)
    assert points == -8.0
    assert reason.text == "Description in German: the job likely needs German"
    # Counted once when the title already says it.
    _, reason = language_risk("Python Entwickler (m/w/d)", GERMAN)
    assert reason.text.startswith("Title in German")
    assert language_risk("Senior Python Developer", ENGLISH_WITH_GERMAN_WORDS) is None


def test_language_of(session):
    assert vacancy_language("Senior Python Developer", GERMAN) == "German"
    assert vacancy_language("Desarrollador Python", None) == "Spanish"
    assert vacancy_language("Senior Python Developer", None) is None


@pytest.mark.parametrize(
    ("text", "language"),
    [
        ("Fluent German (C1/C2) is required", "German"),
        ("Speak fluent Dutch and/or French and have a good knowledge of English", "Dutch"),
        ("• Italian language proficiency is mandatory", "Italian"),
        ("You are fluent in both German and English, both written and spoken", "German"),
        ("* Languages: Fluency in Polish (C1+) and a strong command of English (B2+)", "Polish"),
        ("Fluent in Chinese & English", "Chinese"),
        ("Sprachkenntnisse: Fließend in Deutsch (Wort und Schrift)", "German"),
        ("Sehr gute Deutsch- und Englischkenntnisse in Wort und Schrift", "German"),
        ("Spanish proficiency", "Spanish"),
        ("Languages: You speak English fluently - German is a big plus", None),
        ("Fluent English required, Korean or Mandarin is a strong plus", None),
        ("Excellent English communication skills (German language not required)", None),
        ("English B2+ / C1 (German is a plus, but not required)", None),
        ("fluency in English, same in German will be a plus but not a must have", None),
        ("including medical insurance and Polish, Slovak speaking clubs", None),
        ("Arabic is a genuine advantage across the region", None),
        ("Fluent English and Russian required", None),
        ("We have offices in Germany and France.", None),
    ],
)
def test_required_language(text, language):
    found = required_language(f"About us.\n{text}\nApply now.")
    assert (found[0] if found else None) == language


def test_required_language_risk_is_a_hard_blocker():
    points, reason = language_risk(
        "Senior Python Developer", "Remote.\nFluent German is required.\n"
    )
    assert points == -8.0
    assert reason.text == "Requires German — “Fluent German is required.”"
    assert vacancy_language("Senior Python Developer", "Fluent German is required.") == "German"
