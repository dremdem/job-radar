import pytest

from job_radar.scoring import score_job


def signs(result) -> dict[str, str]:
    return {r.text: r.sign for r in result.reasons}


def test_strong_match_scores_high():
    result = score_job("Senior Python Backend Engineer (FastAPI)", "Worldwide (Remote)")
    assert result.value >= 9
    reasons = signs(result)
    assert reasons["Senior level"] == "+"
    assert reasons["Python"] == "+"
    assert reasons["Backend"] == "+"
    assert reasons["Worldwide / anywhere"] == "+"


def test_country_limited_remote_is_a_risk():
    result = score_job("Sr Python Developer", "Spain (Remote)")
    assert signs(result)["Remote listed for Spain: check if Thailand is allowed"] == "-"


def test_other_stack_without_python_is_a_risk():
    result = score_job("Senior Java Software Engineer", "Poland (Remote)")
    assert signs(result)["Java, no Python in title"] == "-"
    assert "Python" not in signs(result)


@pytest.mark.parametrize(
    ("title", "location", "reason"),
    [
        ("Junior Python Developer", "Remote", "Junior / intern"),
        ("Frontend Engineer", "Remote", "Frontend"),
        ("Python Developer", "Berlin, Germany (Hybrid)", "Hybrid / on-site"),
        ("Python Developer", "Manila, Philippines", "No remote marker in location"),
    ],
)
def test_risks(title, location, reason):
    assert signs(score_job(title, location))[reason] == "-"


def test_score_is_clamped():
    assert score_job("Junior Frontend Java Intern", "London (On-site)").value == 0
    assert (
        0 <= score_job("Staff Senior Lead Python Backend Django Platform", "Anywhere").value <= 10
    )


def test_unknown_location_is_neutral():
    result = score_job("Python Developer", None)
    assert all("location" not in r.text.lower() for r in result.reasons)
