"""Delivery tracking: news counted once, never backwards, from Brevo, the practice
server, our pixel and our click links."""
from datetime import datetime, timedelta

from sqlalchemy import func, select

from oncue import audience, db, results, sending, tokens, tracking, worker
from oncue.models import Contact, Link, Message, MessageEvent, Show, Workspace
from oncue.views.public import unsubscribe_token

from .conftest import CALM, post, text, words

T0 = datetime(2026, 11, 16, 1, 0)


def setup(ctx, emails=("ana@example.com",), body="Book [here](https://teatro.example/book).", **settings):
    workspace = Workspace(name="Teatro", sender_name="Teatro", sender_email="hi@teatro.example",
                          **{**CALM, **settings})
    ctx.add(workspace)
    ctx.flush()
    for email in emails:
        ctx.add(Contact(workspace_id=workspace.id, email=email, first_name="X", fields={}))
    show = Show(workspace_id=workspace.id, name="Opening", subject="Hi", body=body, status="standby", send_at=T0)
    ctx.add(show)
    ctx.commit()
    return workspace, show


def sent_message(ctx, email="ana@example.com"):
    ctx.expire_all()
    return ctx.scalar(select(Message).where(Message.email == email))


def send_now(ctx, until=T0 + timedelta(seconds=1)):
    """Send the show, but stop before the practice server's news arrives."""
    worker.simulate(ctx, T0, until)


def test_news_is_counted_once(ctx):
    _workspace, show = setup(ctx)
    send_now(ctx)
    message = sent_message(ctx)
    assert tracking.record(ctx, message, "delivered", "brevo", "x:1", T0) is True
    assert tracking.record(ctx, message, "delivered", "brevo", "x:1", T0) is False
    ctx.commit()
    assert ctx.scalar(select(func.count()).select_from(MessageEvent).where(MessageEvent.source == "brevo")) == 1


def test_news_out_of_order_never_goes_backwards(ctx):
    _workspace, show = setup(ctx)
    send_now(ctx)
    message = sent_message(ctx)
    tracking.record(ctx, message, "opened", "brevo", "o", T0 + timedelta(minutes=5))
    assert (message.status, message.delivered_at) == ("opened", T0 + timedelta(minutes=5))
    tracking.record(ctx, message, "delivered", "brevo", "d", T0 + timedelta(seconds=10))  # late news
    assert message.status == "opened" and message.delivered_at == T0 + timedelta(seconds=10)
    tracking.record(ctx, message, "clicked", "brevo", "c", T0 + timedelta(minutes=6))
    tracking.record(ctx, message, "delivered", "brevo", "d2", T0 + timedelta(minutes=9))
    assert message.status == "clicked" and message.delivered_at == T0 + timedelta(seconds=10)


def test_a_bounce_or_a_complaint_blocks_the_address(ctx):
    workspace, show = setup(ctx, ("ana@example.com", "ben@example.com"))
    send_now(ctx)
    tracking.record(ctx, sent_message(ctx, "ana@example.com"), "bounced", "brevo", "b", T0, "550 no such user")
    tracking.record(ctx, sent_message(ctx, "ben@example.com"), "complained", "brevo", "c", T0)
    ctx.commit()
    assert audience.is_suppressed(ctx, workspace.id, "ana@example.com")
    assert audience.is_suppressed(ctx, workspace.id, "ben@example.com")
    assert sent_message(ctx, "ana@example.com").status == "bounced"


def test_news_clears_up_an_unsure_email(ctx):
    _workspace, show = setup(ctx)
    send_now(ctx)
    message = sent_message(ctx)
    message.status = sending.UNSURE
    tracking.record(ctx, message, "delivered", "brevo", "d", T0)
    assert message.status == "delivered" and message.last_error == "News arrived after all: it did go out."


def test_the_practice_server_sends_its_own_news(ctx):
    _workspace, show = setup(ctx, [f"p{i}@example.com" for i in range(30)] + ["bounce.x@example.com",
                                                                               "complain.y@example.com"], send_rate=50)
    worker.simulate(ctx, T0, T0 + timedelta(hours=3))
    found = results.for_show(ctx, show.id)
    assert found["total"] == found["sent"] == 32
    assert found["delivered"] == 31 and found["bounced"] == 1 and found["complained"] == 1
    assert 5 <= found["opened"] <= 25 and found["clicked"] >= 1  # about half open, a third of those click
    assert sent_message(ctx, "complain.y@example.com").status == "complained"
    assert results.link_clicks(ctx, show.id)[0][0] == "https://teatro.example/book"


def test_links_in_the_email_are_wrapped_and_the_pixel_is_there(app, ctx):
    _workspace, show = setup(ctx)
    send_now(ctx)
    from oncue.models import PracticeMail
    mail = ctx.scalar(select(PracticeMail))
    assert "https://teatro.example/book" not in mail.html  # wrapped
    assert "http://localhost/c/" in mail.html and "http://localhost/o/" in mail.html
    assert "https://teatro.example/book" in mail.text  # the plain text keeps the real link


def test_the_click_link_counts_and_redirects_only_to_its_link(app, client):
    with app.app_context():
        s = db.session()
        _workspace, show = setup(s)
        send_now(s)
        message = sent_message(s)
        link = s.scalar(select(Link))
        good = tokens.make("click", message.id, link.id)
        message_id, link_id = message.id, link.id
    response = client.get(f"/c/{good}")
    assert response.status_code == 302 and response.headers["Location"] == "https://teatro.example/book"
    client.get(f"/c/{good}")  # twice: still one click
    forged = good.replace(f"{message_id}.{link_id}.", f"{message_id}.{link_id + 1}.", 1)
    assert client.get(f"/c/{forged}").status_code == 404
    assert client.get("/c/1.1.notasignature").status_code == 404
    with app.app_context():
        s = db.session()
        assert sent_message(s).clicked_at is not None
        assert s.scalar(select(func.count()).select_from(MessageEvent).where(MessageEvent.source == "click")) == 1


def test_the_open_pixel(app, client):
    with app.app_context():
        s = db.session()
        setup(s)
        send_now(s)
        message_id = sent_message(s).id
        token = tokens.make("open", message_id)
    forged = client.get(f"/o/{token[:-2]}xx.gif")
    assert forged.status_code == 200 and forged.mimetype == "image/gif"
    with app.app_context():
        assert sent_message(db.session()).opened_at is None
    client.get(f"/o/{token}.gif")
    with app.app_context():
        assert sent_message(db.session()).status == "opened"


def test_the_brevo_webhook(app, client):
    app.config["BREVO_WEBHOOK_SECRET"] = "s3cret"
    with app.app_context():
        s = db.session()
        _workspace, show = setup(s, ("ana@example.com", "ben@example.com"))
        send_now(s)
        ana, ben = sent_message(s, "ana@example.com"), sent_message(s, "ben@example.com")
        ana.provider_id = "<ana@brevo>"
        s.commit()
        ben_id = ben.id
    assert client.post("/hooks/brevo/wrong", json={}).status_code == 404
    assert client.post("/hooks/brevo/s3cret", data="not json", content_type="text/plain").status_code == 400
    events = [
        {"event": "delivered", "message-id": "<ana@brevo>", "ts_event": 1763254800},
        {"event": "delivered", "message-id": "<ana@brevo>", "ts_event": 1763254800},  # Brevo sent it twice
        {"event": "click", "message-id": "<ana@brevo>", "ts_event": 1763254900, "link": "https://teatro.example/book"},
        {"event": "unsubscribed", "tag": f"oncue-message-{ben_id}", "ts_event": 1763255000},  # found by its tag
        {"event": "delivered", "message-id": "<somebody-else@brevo>"},
        {"event": "a_new_kind"},
    ]
    answer = client.post("/hooks/brevo/s3cret", json=events).get_json()
    assert answer["results"] == ["recorded", "duplicate", "recorded", "recorded", "ignored", "ignored"]
    with app.app_context():
        s = db.session()
        ana = sent_message(s, "ana@example.com")
        assert ana.status == "clicked" and ana.delivered_at == datetime(2025, 11, 16, 1, 0)
        assert s.scalar(select(Contact.status).where(Contact.email == "ben@example.com")) == "unsubscribed"


def test_unsubscribing_from_an_email_is_news_about_it(app, client):
    with app.app_context():
        s = db.session()
        setup(s)
        send_now(s)
        message = sent_message(s)
        token = unsubscribe_token(message.contact_id, message.id)
    post(client, f"/u/{token}", {"action": "all"})
    with app.app_context():
        assert sent_message(db.session()).unsubscribed_at is not None


def test_the_results_and_email_pages(app, owner):
    client, workspace_id = owner
    with app.app_context():
        s = db.session()
        for index in range(4):
            s.add(Contact(workspace_id=workspace_id, email=f"p{index}@example.com", fields={}))
        s.add(Contact(workspace_id=workspace_id, email="bounce.z@example.com", fields={}))
        show = Show(workspace_id=workspace_id, name="Opening", subject="Hi", body="[Book](https://t.example/b)",
                    status="standby", send_at=T0)
        s.add(show)
        s.commit()
        show_id = show.id
        worker.simulate(s, T0, T0 + timedelta(hours=2))
        bounced = sent_message(s, "bounce.z@example.com")
        contact_id = bounced.contact_id
    page = words(client.get(f"/shows/{show_id}"))
    assert "Results" in page and "Delivered 80%" in page and "Bounced 1" in page
    emails = text(client.get(f"/shows/{show_id}/emails?group=trouble"))
    assert "bounce.z@example.com" in emails and "p0@example.com" not in emails
    timeline = words(client.get(f"/shows/{show_id}/emails/{bounced.id}"))
    assert "Taken by the mail service" in timeline and "Bounced" in timeline and "mailbox doesn't exist" in timeline
    assert "Emails They Got" in text(client.get(f"/audience/{contact_id}"))
