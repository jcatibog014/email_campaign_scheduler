"""Reviews (charts and numbers) and Backstage (the job console and its switches)."""
from datetime import timedelta

from sqlalchemy import select

from oncue import buckets, charts, db, mailers, queue, worker
from oncue.brevo import BrevoMailer
from oncue.dates import utcnow
from oncue.models import Contact, Job, Message, PracticeMail, Show, Workspace

from .conftest import browser, make_owner, post, text, words


def add_show(app, workspace_id, emails, sent_ago=timedelta(days=2), name="Opening", **settings):
    """A show with these people, sent (played forward) sent_ago. settings change the workspace."""
    with app.app_context():
        s = db.session()
        workspace = s.get(Workspace, workspace_id)
        for key, value in settings.items():
            setattr(workspace, key, value)
        for email in emails:
            s.add(Contact(workspace_id=workspace_id, email=email, first_name="X", fields={}))
        start = utcnow() - sent_ago
        show = Show(workspace_id=workspace_id, name=name, subject="Hi",
                    body="[Book](https://t.example/b)", status="standby", send_at=start)
        s.add(show)
        s.commit()
        worker.simulate(s, start, start + timedelta(hours=3))
        return show.id


# ---------- Charts ----------

def test_nice_axis_tops():
    assert [charts.nice_max(v) for v in (0, 3, 7, 12, 99, 101, 4500)] == [1, 5, 10, 20, 100, 200, 5000]


def test_column_labels_never_touch():
    points = [(f"Nov {day}", day, "") for day in range(1, 31)]
    drawn = charts.columns(points)
    shown = sorted(drawn["shown"])
    assert 29 in shown  # the last label is always there
    centers = [drawn["bars"][i].x_mid for i in shown]
    widest = max(len(label) for label, _v, _t in points) * charts.CHAR_WIDTH
    assert all(b - a >= widest for a, b in zip(centers, centers[1:]))


def test_bars_sit_on_the_baseline_with_rounded_tops():
    drawn = charts.columns([("a", 5, ""), ("b", 0, ""), ("c", 10, "")])
    assert drawn["bars"][1].path == ""  # nothing drawn for zero
    path = drawn["bars"][2].path
    assert path.startswith("M") and f",{drawn['base']:.1f} V" in path  # starts on the baseline
    assert path.count(" Q") == 2 and path.endswith(f"V{drawn['base']:.1f} Z")  # two rounded top corners, flat foot


# ---------- Reviews ----------

def test_reviews_with_and_without_sends(app, owner):
    client, workspace_id = owner
    assert "No Reviews Yet" in text(client.get("/reviews/"))
    add_show(app, workspace_id, [f"p{i}@example.com" for i in range(20)] + ["bounce.z@example.com"], send_rate=50)
    page = text(client.get("/reviews/"))
    assert "Delivery Funnel" in page and "First Opens per Day" in page and 'class="chart-bar"' in page
    assert "View data table" in page and "Opening" in page and "mailbox doesn" in page
    assert ">21<" in page.replace(" ", "")  # 21 sent
    assert "First Opens per Week" in text(client.get("/reviews/?days=365"))
    assert client.get("/reviews/?days=12345").status_code == 200  # an unknown period falls back to 30 days


def test_a_show_older_than_the_period_is_left_out(app, owner):
    client, workspace_id = owner
    add_show(app, workspace_id, ["a@example.com"], sent_ago=timedelta(days=20), name="Old Show")
    assert "Old Show" not in text(client.get("/reviews/?days=7"))
    assert "Old Show" in text(client.get("/reviews/?days=30"))


# ---------- Backstage ----------

def test_the_crew_and_the_counts(app, owner):
    client, workspace_id = owner
    add_show(app, workspace_id, ["a@example.com", "b@example.com"], sent_ago=timedelta(hours=1))
    page = words(client.get("/backstage/"))
    assert "The Crew" in page and "rehearsal" in page and "Off duty" in page
    assert "Auto-pause Off: sending as usual" in page and "of 300 emails" in page


def test_pause_all_sending(app, owner):
    client, workspace_id = owner
    post(client, "/backstage/pause", {"paused": "yes"})
    assert "Sending is paused" in text(client.get("/stage"))
    with app.app_context():
        s = db.session()
        s.add(Contact(workspace_id=workspace_id, email="a@example.com", fields={}))
        show = Show(workspace_id=workspace_id, name="Paused", subject="Hi", body="x", status="standby",
                    send_at=utcnow())
        s.add(show)
        s.commit()
        worker.simulate(s, utcnow(), utcnow() + timedelta(minutes=5))
        message = s.scalar(select(Message))
        job = s.scalar(select(Job).where(Job.kind == "send_message"))
        assert message.status == "wings" and job.attempts == 0  # waiting costs no tries
    post(client, "/backstage/pause", {"paused": "no"})
    with app.app_context():
        s = db.session()
        worker.simulate(s, utcnow() + timedelta(minutes=5), utcnow() + timedelta(minutes=10))
        assert s.scalar(select(Message.status)) != "wings"


def test_dead_letters_retry_and_discard(app, owner):
    client, workspace_id = owner
    add_show(app, workspace_id, ["busy.a@example.com", "busy.b@example.com"])
    page = text(client.get("/backstage/dead-letters"))
    assert page.count("Retry now") == 2 and "Gave up after 6 tries" in page
    assert ">2<" in text(client.get("/stage")).replace(" ", "")  # the menu's badge
    with app.app_context():
        first, second = db.session().scalars(select(Job.id).where(Job.status == "dead").order_by(Job.id)).all()
    post(client, f"/backstage/jobs/{first}/retry")
    post(client, f"/backstage/jobs/{second}/discard")
    with app.app_context():
        s = db.session()
        retried = s.get(Job, first)
        message = s.get(Message, retried.payload["message_id"])
        assert (retried.status, retried.attempts, message.status) == ("queued", 0, "wings")
        assert s.get(Job, second).status == "discarded"
    assert post(client, f"/backstage/jobs/{second}/retry").status_code == 409


def test_unsure_emails_are_decided_by_a_person(app, owner, monkeypatch):
    client, workspace_id = owner
    app.config["BREVO_API_KEY"] = "k"

    def silent(self, s, outgoing, now):
        raise mailers.MailerTimeout("no answer")

    monkeypatch.setattr(BrevoMailer, "send", silent)
    add_show(app, workspace_id, ["a@example.com", "b@example.com"], mailer="brevo")
    page = text(client.get("/backstage/unsure"))
    assert "a@example.com" in page and "a missing email is better than a double one" in page
    with app.app_context():
        first, second = db.session().scalars(select(Message.id).order_by(Message.id)).all()
    post(client, f"/backstage/messages/{first}/went")
    post(client, f"/backstage/messages/{second}/resend")
    with app.app_context():
        s = db.session()
        assert s.get(Message, first).status == "sent"
        resent = s.get(Message, second)
        assert resent.status == "wings" and resent.send_token is None
        assert s.scalar(select(Job).where(Job.status == "queued", Job.kind == "send_message")) is not None


def test_the_practice_inbox(app, owner):
    client, workspace_id = owner
    add_show(app, workspace_id, ["a@example.com"])
    assert "a@example.com" in text(client.get("/backstage/inbox"))
    with app.app_context():
        mail_id = db.session().scalar(select(PracticeMail.id))
    page = text(client.get(f"/backstage/inbox/{mail_id}"))
    assert "List-Unsubscribe" in page and "Plain-text version" in page
    frame = client.get(f"/backstage/inbox/{mail_id}/frame")
    assert "default-src 'none'" in frame.headers["Content-Security-Policy"]


def test_the_safety_curtain_on_the_stage(app, owner):
    client, workspace_id = owner
    with app.app_context():
        s = db.session()
        for _ in range(buckets.TRIP_AFTER):
            buckets.record(s, workspace_id, "practice", False, utcnow(), "503 Service unavailable")
        s.commit()
    page = text(client.get("/stage"))
    assert "The Safety Curtain is down" in page
    post(client, "/backstage/curtain", {"back": "/stage"})
    assert "The Safety Curtain is down" not in text(client.get("/stage"))


def test_another_workspaces_jobs_are_not_found(app, owner):
    client, workspace_id = owner
    add_show(app, workspace_id, ["busy.a@example.com"])
    with app.app_context():
        s = db.session()
        job_id = s.scalar(select(Job.id).where(Job.status == "dead"))
        message_id = s.scalar(select(Message.id))
        mail_count = len(s.scalars(select(PracticeMail.id)).all())
    other_user, _ = make_owner(app, name="Other", email="other@example.com")
    other = browser(app, other_user)
    assert post(other, f"/backstage/jobs/{job_id}/retry").status_code == 404
    assert post(other, f"/backstage/messages/{message_id}/resend").status_code == 404
    assert "busy.a@example.com" not in text(other.get("/backstage/dead-letters"))
    assert mail_count == 0  # busy addresses never reached the inbox
    with app.app_context():
        assert db.session().get(Workspace, workspace_id) is not None
