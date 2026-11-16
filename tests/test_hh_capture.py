"""Tests for job_radar.hh.capture: an hh.ru vacancy page from the bookmarklet becomes
a vacancy or fills in its description and work format (docs/hh.md).
"""

import sqlalchemy as sa

import job_radar.linkedin.capture as linkedin_capture
import job_radar.models as models

DESCRIPTION = "Мы ищем ML Engineer.\nТребования: Python, PyTorch, опыт от 3 лет."


def payload(**detail):
    base = {
        "job_id": "138030671",
        "title": "ML Engineer (Senior)",
        "company": "ТОО Agile Soft",
        "salary": "",
        "work_format": "Формат работы: удалённо или гибрид",
        "address": "Алматы , Абая , Алмалы",
        "description": DESCRIPTION,
    }
    return {
        "source": "hh",
        "source_url": "https://hh.ru/vacancy/138030671",
        "detail": base | detail,
    }


def test_new_hh_vacancy_from_its_page(session):
    stats = linkedin_capture.import_capture(session, payload())
    record = session.scalars(sa.select(models.SourceRecord)).one()
    assert record.source_type is models.SourceType.HH and record.external_id == "138030671"
    assert record.url == "https://hh.ru/vacancy/138030671"
    o = record.opportunity
    assert o.title == "ML Engineer (Senior)" and o.company.name == "ТОО Agile Soft"
    assert o.location == "Remote (Алматы)"
    assert record.snapshots[0].raw_text == DESCRIPTION
    assert stats.descriptions == 1 and stats.new == 1
    assert "ML Engineer (Senior)" in stats.vacancy and "description saved" in stats.vacancy
    assert stats.opportunity_ids == [o.id]


def test_existing_hh_vacancy_gets_the_description_and_format(session):
    import job_radar.dedup as dedup

    o = models.Opportunity(
        title="Senior ML-инженер",
        company=dedup.get_or_create_company(session, "Автоспот"),
        location="Remote (Москва)",
        state=models.OpportunityState.NEW,
    )
    session.add(o)
    session.flush()
    session.add(models.SourceRecord(source_type=models.SourceType.HH, external_id="138030671",
                             url="https://hh.ru/vacancy/138030671", opportunity=o))  # fmt: skip
    session.commit()
    stats = linkedin_capture.import_capture(session, payload(work_format="Формат работы: гибрид"))
    session.refresh(o)
    assert stats.new == 0 and stats.descriptions == 1
    assert o.location == "Hybrid, Алматы"
    assert session.query(models.Opportunity).count() == 1
    # The same text again is not stored twice.
    assert (
        linkedin_capture.import_capture(
            session, payload(work_format="Формат работы: гибрид")
        ).descriptions
        == 0
    )


def test_on_site_and_unknown_format():
    import job_radar.hh.capture as hh_capture

    assert (
        hh_capture.location_for("Формат работы: на месте работодателя", "Москва")
        == "On-site, Москва"
    )
    assert hh_capture.location_for("", "Москва") == "Москва"
    assert hh_capture.location_for("Формат работы: удалённо", "") == "Remote"


def test_page_without_id_or_title_is_not_recognised(session):
    stats = linkedin_capture.import_capture(session, payload(job_id="", title=""))
    assert stats.new == 0 and "not recognised" in stats.vacancy


def test_capture_endpoint_accepts_hh(client, session):
    response = client.post("/api/capture", json=payload())
    assert response.status_code == 200 and response.json()["descriptions"] == 1


def test_bookmarklet_knows_hh():
    import job_radar.web.app as web_app

    code = web_app.bookmarklet("http://127.0.0.1:8000")
    assert "hh\\.(ru|kz|by|uz)" in code and 'data-qa="vacancy-description"' in code


def test_city_is_the_first_part_of_the_address():
    import job_radar.hh.capture as hh_capture

    assert hh_capture._city(" Москва , Тверская улица, 1 ") == "Москва"
    assert hh_capture._city("") == "" and hh_capture._city("   ") == ""
    # linear: CodeQL py/polynomial-redos (#11)
    assert hh_capture._city(" " * 50_000 + ",x") == ""
