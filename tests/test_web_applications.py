"""Tests for the Applications page: suggestions, history with status, confirm and
dismiss (docs/applications.md).
"""

import datetime as dt

import sqlalchemy as sa

import job_radar.dedup as dedup
import job_radar.models as models
import job_radar.rejections as rejections

T0 = dt.datetime(2026, 9, 1, tzinfo=dt.UTC)


def add_app(session, company, review, *, role="Python Developer", at=T0, thread="t1"):
    email = models.EmailMessage(
        gmail_message_id=f"m-{company}",
        gmail_thread_id=thread,
        direction=models.EmailDirection.IN,
        sent_at=at,
        from_addr="x <x@example.com>",
        to_addr="user@example.com",
        subject="s",
    )
    session.add(email)
    session.flush()
    app = models.Application(
        company=dedup.get_or_create_company(session, company),
        applied_at=at,
        role=role,
        source=models.ApplicationSource.ATS,
        review=review,
        email_message_id=email.id,
    )
    session.add(app)
    session.commit()
    return app


def test_empty_page(client):
    response = client.get("/applications")
    assert response.status_code == 200
    assert "No applications yet" in response.text


def test_lists_suggestions_and_history_with_status(client, session):
    add_app(session, "Lingaro", models.ApplicationReview.SUGGESTED)
    app = add_app(session, "Capitole", models.ApplicationReview.CONFIRMED, thread="thr-9")
    rejections.mark_rejected(
        session, company_id=app.company_id, rejected_at=T0 + dt.timedelta(days=3)
    )
    session.commit()

    text = client.get("/applications").text
    assert "To review (1)" in text
    assert "Lingaro" in text
    assert "Capitole" in text
    assert "rejected" in text
    assert "https://mail.google.com/mail/u/0/#all/thr-9" in text


def test_dismissed_are_hidden(client, session):
    add_app(session, "Hidden Co", models.ApplicationReview.DISMISSED)
    assert "Hidden Co" not in client.get("/applications").text


def test_confirm_dismiss_and_confirm_all(client, session):
    a = add_app(session, "A Co", models.ApplicationReview.SUGGESTED)
    b = add_app(session, "B Co", models.ApplicationReview.SUGGESTED)
    c = add_app(session, "C Co", models.ApplicationReview.SUGGESTED)

    assert client.post(f"/applications/{a.id}/confirm").status_code == 200
    assert client.post(f"/applications/{b.id}/dismiss").status_code == 200
    assert client.post("/applications/confirm-all").status_code == 200

    reviews = {x.company.name: x.review for x in session.scalars(sa.select(models.Application))}
    for x in (a, b, c):
        session.refresh(x)
    assert (a.review, b.review, c.review) == (
        models.ApplicationReview.CONFIRMED,
        models.ApplicationReview.DISMISSED,
        models.ApplicationReview.CONFIRMED,
    )
    assert reviews


def test_unknown_application_is_404(client):
    assert client.post("/applications/999/confirm").status_code == 404


def test_queue_links_to_applications(client):
    assert 'href="/applications"' in client.get("/").text


def seed_named(session):
    add_app(session, "Value Innovation Labs", models.ApplicationReview.CONFIRMED, thread="t-v")
    add_app(session, "Caixa Mágica Software", models.ApplicationReview.CONFIRMED, thread="t-c")
    add_app(session, "Lingaro", models.ApplicationReview.SUGGESTED, thread="t-l")
    add_app(session, "Valtech", models.ApplicationReview.SUGGESTED, thread="t-vt")


def test_filter_by_company_is_partial_and_case_insensitive(client, session):
    seed_named(session)
    text = client.get("/applications?company=VAL").text
    assert "Value Innovation Labs" in text and "Valtech" in text
    assert "Lingaro" not in text and "Caixa" not in text
    assert "To review (1)" in text and "History (1)" in text
    assert "Caixa Mágica Software" in client.get("/applications?company=mÁgica").text


def test_filter_field_keeps_value_and_nothing_found(client, session):
    seed_named(session)
    text = client.get("/applications?company=nobody").text
    assert 'name="company" value="nobody"' in text
    assert "No applications match" in text


def test_confirm_all_only_confirms_the_filtered(client, session):
    seed_named(session)
    client.post("/applications/confirm-all", data={"company": "val"})
    reviews = {a.company.name: a.review for a in session.scalars(sa.select(models.Application))}
    for a in session.scalars(sa.select(models.Application)):
        session.refresh(a)
    reviews = {a.company.name: a.review for a in session.scalars(sa.select(models.Application))}
    assert reviews["Valtech"] is models.ApplicationReview.CONFIRMED
    assert reviews["Lingaro"] is models.ApplicationReview.SUGGESTED


def test_review_buttons_keep_the_filter(client, session):
    seed_named(session)
    text = client.get("/applications?company=val").text
    assert 'hx-include="#applications-filter"' in text
    lingaro = next(
        a for a in session.scalars(sa.select(models.Application)) if a.company.name == "Lingaro"
    )
    response = client.post(f"/applications/{lingaro.id}/dismiss", data={"company": "val"})
    assert "Lingaro" not in response.text and "Valtech" in response.text
