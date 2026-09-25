from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from job_radar.dedup import get_or_create_company
from job_radar.models import (
    Application,
    ApplicationReview,
    ApplicationSource,
    EmailDirection,
    EmailMessage,
)
from job_radar.rejections import mark_rejected

T0 = datetime(2026, 9, 1, tzinfo=UTC)


def add_app(session, company, review, *, role="Python Developer", at=T0, thread="t1"):
    email = EmailMessage(
        gmail_message_id=f"m-{company}",
        gmail_thread_id=thread,
        direction=EmailDirection.IN,
        sent_at=at,
        from_addr="x <x@example.com>",
        to_addr="user@example.com",
        subject="s",
    )
    session.add(email)
    session.flush()
    app = Application(
        company=get_or_create_company(session, company),
        applied_at=at,
        role=role,
        source=ApplicationSource.ATS,
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
    add_app(session, "Lingaro", ApplicationReview.SUGGESTED)
    app = add_app(session, "Capitole", ApplicationReview.CONFIRMED, thread="thr-9")
    mark_rejected(session, company_id=app.company_id, rejected_at=T0 + timedelta(days=3))
    session.commit()

    text = client.get("/applications").text
    assert "To review (1)" in text
    assert "Lingaro" in text
    assert "Capitole" in text
    assert "rejected" in text
    assert "https://mail.google.com/mail/u/0/#all/thr-9" in text


def test_dismissed_are_hidden(client, session):
    add_app(session, "Hidden Co", ApplicationReview.DISMISSED)
    assert "Hidden Co" not in client.get("/applications").text


def test_confirm_dismiss_and_confirm_all(client, session):
    a = add_app(session, "A Co", ApplicationReview.SUGGESTED)
    b = add_app(session, "B Co", ApplicationReview.SUGGESTED)
    c = add_app(session, "C Co", ApplicationReview.SUGGESTED)

    assert client.post(f"/applications/{a.id}/confirm").status_code == 200
    assert client.post(f"/applications/{b.id}/dismiss").status_code == 200
    assert client.post("/applications/confirm-all").status_code == 200

    reviews = {x.company.name: x.review for x in session.scalars(select(Application))}
    for x in (a, b, c):
        session.refresh(x)
    assert (a.review, b.review, c.review) == (
        ApplicationReview.CONFIRMED,
        ApplicationReview.DISMISSED,
        ApplicationReview.CONFIRMED,
    )
    assert reviews


def test_unknown_application_is_404(client):
    assert client.post("/applications/999/confirm").status_code == 404


def test_queue_links_to_applications(client):
    assert 'href="/applications"' in client.get("/").text
