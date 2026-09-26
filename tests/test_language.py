import pytest

from job_radar.language import detect_title_language, language_risk


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
    assert points == -3.0
    assert reason.sign == "-"
    assert reason.text == "Title in Spanish: the job likely needs Spanish"
    assert language_risk("Senior Python Developer") is None
