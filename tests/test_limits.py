"""Sending politely and patiently: token buckets, the daily cap, slowing down,
the Safety Curtain, the practice server's moods, timeouts, Brevo, tests."""
import io
import json
import threading
import urllib.error
from datetime import datetime, timedelta

import pytest
from sqlalchemy import func, select

from oncue import audience, buckets, db, mailers, queue, sending, worker
from oncue.brevo import BrevoMailer
from oncue.dates import utcnow
from oncue.models import Bucket, Contact, Job, Message, PracticeMail, Show, Suppression, Workspace
from oncue.practice_server import PracticeMailer

from .conftest import CALM, post, text

T0 = datetime(2026, 11, 16, 1, 0)  # Monday 9:00 AM in Manila


def setup(ctx, emails, send_at=T0, **settings):
    workspace = Workspace(name="Teatro", sender_name="Teatro", sender_email="hi@teatro.example",
                          **{**CALM, **settings})
    ctx.add(workspace)
    ctx.flush()
    for email in emails:
        ctx.add(Contact(workspace_id=workspace.id, email=email, first_name="X", fields={}))
    show = Show(workspace_id=workspace.id, name="Opening", subject="Hi", body="Come!", status="standby",
                send_at=send_at)
    ctx.add(show)
    ctx.commit()
    return workspace, show


def statuses(ctx, show_id):
    """Message counts, with every "went out" status (sent, delivered, opened, clicked) counted as sent."""
    ctx.expire_all()
    collapsed = {}
    for status, number in sending.counts(ctx, show_id).items():
        key = "sent" if status in sending.ACCEPTED else status
        collapsed[key] = collapsed.get(key, 0) + number
    return collapsed


def message_for(ctx, email):
    ctx.expire_all()
    return ctx.scalar(select(Message).where(Message.email == email))


# ---------- Token buckets ----------

def test_a_bucket_allows_a_burst_then_a_steady_pace(ctx):
    assert buckets.take(ctx, "k", None, capacity=2, rate=1, now=T0) == 0
    assert buckets.take(ctx, "k", None, capacity=2, rate=1, now=T0) == 0
    assert buckets.take(ctx, "k", None, capacity=2, rate=1, now=T0) == pytest.approx(1.0)
    assert buckets.take(ctx, "k", None, capacity=2, rate=1, now=T0 + timedelta(seconds=0.5)) == pytest.approx(0.5)
    assert buckets.take(ctx, "k", None, capacity=2, rate=1, now=T0 + timedelta(seconds=1)) == 0


def test_floating_point_dust_never_makes_a_job_spin(ctx):
    """0.9999999999 tokens is a token; and a wait is never so small that it rounds to nothing."""
    from oncue.models import Bucket
    ctx.add(Bucket(key="dust", tokens=0.9999999999, updated_at=T0))
    ctx.commit()
    assert buckets.take(ctx, "dust", None, capacity=5, rate=5, now=T0) == 0
    ctx.add(Bucket(key="almost", tokens=0.99999, updated_at=T0))
    ctx.commit()
    assert buckets.take(ctx, "almost", None, capacity=5, rate=5, now=T0) >= buckets.MIN_WAIT


def test_a_big_backlog_doesnt_stampede(ctx, monkeypatch):
    """100 emails due at once at 5 a second: waiting emails are spread over the backlog,
    so each is deferred about once, not again and again (a thundering herd)."""
    defers = []
    real_defer = queue.defer
    monkeypatch.setattr(queue, "defer", lambda job, until, reason="": (defers.append(job.id), real_defer(job, until, reason)))
    _workspace, show = setup(ctx, [f"p{i}@example.com" for i in range(100)])
    worker.simulate(ctx, T0, T0 + timedelta(minutes=5))
    assert statuses(ctx, show.id) == {"sent": 100}
    assert len(defers) < 200  # about once each; before the spread it was over 2,000


def test_two_workers_never_spend_the_same_token(app):
    """50 tokens, two threads grabbing at once: exactly 50 grabs succeed (compare-and-swap)."""
    with app.app_context():
        s = db.new_session()
        buckets.take(s, "shared", None, capacity=51, rate=1e-9, now=T0)  # make it, spend 1: 50 left
        s.commit()
        s.close()
    won = []
    ready = threading.Barrier(2)

    def grab():
        with app.app_context():
            session = db.new_session()
            ready.wait()
            for _ in range(40):
                if buckets.take(session, "shared", None, capacity=51, rate=1e-9, now=T0) == 0:
                    won.append(1)
                session.commit()
            session.close()

    threads = [threading.Thread(target=grab) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(won) == 50


def test_pacing_defers_without_using_tries(ctx):
    workspace, show = setup(ctx, [f"p{i}@example.com" for i in range(8)], send_rate=2)
    worker.simulate(ctx, T0, T0 + timedelta(minutes=1))
    assert statuses(ctx, show.id) == {"sent": 8}
    ctx.expire_all()
    times = sorted(m.sent_at for m in ctx.scalars(select(Message)))
    assert (times[-1] - times[0]).total_seconds() >= 3  # 2 at once, then 2 a second
    assert ctx.scalar(select(func.max(Job.attempts)).where(Job.kind == "send_message")) == 1


def test_the_daily_cap_continues_tomorrow(ctx):
    workspace, show = setup(ctx, [f"p{i}@example.com" for i in range(5)], daily_cap=3)
    worker.simulate(ctx, T0, T0 + timedelta(hours=10))
    assert statuses(ctx, show.id) == {"sent": 3, "wings": 2}
    estimate = buckets.finish_estimate(ctx, workspace, "practice", 2, T0 + timedelta(hours=1))
    assert estimate.date() == T0.date() + timedelta(days=1)
    worker.simulate(ctx, T0 + timedelta(hours=10), T0 + timedelta(days=1, hours=1))
    assert statuses(ctx, show.id) == {"sent": 5}
    ctx.expire_all()
    assert ctx.get(Show, show.id).status == "curtain_down"


def test_a_429_slows_the_bucket_down(ctx):
    workspace, show = setup(ctx, ["busy.ana@example.com"])
    worker.simulate(ctx, T0, T0 + timedelta(seconds=1))
    bucket = ctx.get(Bucket, f"rate:{workspace.id}:practice")
    assert bucket.slow_until == T0 + buckets.SLOW_DOWN_FOR
    _tokens, slow = buckets.level(ctx, bucket.key, 5, 5, T0 + timedelta(minutes=1))
    assert slow


def test_a_domain_limit_paces_one_domain(ctx):
    workspace, show = setup(ctx, ["a@gmail.example", "b@gmail.example", "c@yahoo.example"])
    from oncue.models import DomainLimit
    ctx.add(DomainLimit(workspace_id=workspace.id, domain="gmail.example", per_minute=6))
    ctx.commit()
    worker.simulate(ctx, T0, T0 + timedelta(minutes=1))
    gmail = sorted(m.sent_at for m in ctx.scalars(select(Message).where(Message.email.like("%gmail%"))))
    assert (gmail[1] - gmail[0]).total_seconds() >= 9  # 6 a minute: one every 10 seconds


# ---------- The practice server's special addresses ----------

def test_reject_drops_and_blocks_the_address(ctx):
    workspace, show = setup(ctx, ["reject.ana@example.com", "ben@example.com"])
    worker.simulate(ctx, T0, T0 + timedelta(minutes=1))
    assert statuses(ctx, show.id) == {"dropped": 1, "sent": 1}
    assert "550" in message_for(ctx, "reject.ana@example.com").last_error
    assert audience.is_suppressed(ctx, workspace.id, "reject.ana@example.com")
    assert ctx.get(Show, show.id).status == "curtain_down"


def test_flaky_works_on_the_third_try_with_backoff(ctx):
    _workspace, show = setup(ctx, ["flaky.ana@example.com"])
    worker.simulate(ctx, T0, T0 + timedelta(hours=1))
    message = message_for(ctx, "flaky.ana@example.com")
    assert message.status in sending.ACCEPTED and message.attempts == 3
    assert ctx.scalar(select(Job.attempts).where(Job.kind == "send_message")) == 3


def test_busy_ends_as_a_dead_letter_and_dropped(ctx):
    _workspace, show = setup(ctx, ["busy.ana@example.com"])
    worker.simulate(ctx, T0, T0 + timedelta(hours=2))
    message = message_for(ctx, "busy.ana@example.com")
    job = ctx.scalar(select(Job).where(Job.kind == "send_message"))
    assert job.status == "dead" and job.attempts == queue.MAX_ATTEMPTS
    assert message.status == "dropped" and "Gave up after 6 tries" in message.last_error
    assert ctx.get(Show, show.id).status == "curtain_down"


# ---------- The Safety Curtain ----------

def test_the_safety_curtain_falls_and_rises(ctx):
    people = [f"busy.p{i}@example.com" for i in range(10)] + ["ana@example.com"]
    workspace, show = setup(ctx, people, send_rate=50)
    worker.simulate(ctx, T0, T0 + timedelta(seconds=5))
    curtain = buckets.breaker(ctx, workspace.id, "practice")
    assert curtain.state == "open" and curtain.failures >= buckets.TRIP_AFTER
    # While it is down, nothing is sent, not even Ana's.
    assert message_for(ctx, "ana@example.com").status == "wings"
    with pytest.raises(queue.Defer):
        buckets.allow(ctx, workspace.id, "practice", T0 + timedelta(minutes=1))
    # After 5 minutes one probe goes. Ana's works, so the curtain rises.
    later = curtain.retry_at
    for person in people[:10]:  # stop the busy ones failing, to let the probe succeed
        ctx.scalar(select(Contact).where(Contact.email == person)).status = "unsubscribed"
    ctx.commit()
    worker.simulate(ctx, later, later + timedelta(minutes=2))
    assert buckets.breaker(ctx, workspace.id, "practice").state == "closed"
    assert message_for(ctx, "ana@example.com").status in sending.ACCEPTED


def test_a_failed_probe_lowers_it_again(ctx):
    workspace, _show = setup(ctx, [])
    for _ in range(buckets.TRIP_AFTER):
        buckets.record(ctx, workspace.id, "practice", False, T0, "503")
    ctx.commit()
    later = T0 + buckets.CURTAIN_DOWN_FOR
    buckets.allow(ctx, workspace.id, "practice", later)  # this caller sends the probe
    with pytest.raises(queue.Defer):
        buckets.allow(ctx, workspace.id, "practice", later)  # everyone else waits for it
    buckets.record(ctx, workspace.id, "practice", False, later, "503 again")
    ctx.commit()
    curtain = buckets.breaker(ctx, workspace.id, "practice")
    assert curtain.state == "open" and curtain.retry_at == later + buckets.CURTAIN_DOWN_FOR


def test_a_bounce_doesnt_count_against_the_service(ctx):
    workspace, _show = setup(ctx, [f"reject.p{i}@example.com" for i in range(12)], send_rate=50)
    worker.simulate(ctx, T0, T0 + timedelta(minutes=1))
    assert buckets.breaker(ctx, workspace.id, "practice") is None


# ---------- Timeouts and idempotency ----------

def test_a_timeout_is_retried_with_the_same_token_and_sent_once(ctx, monkeypatch):
    _workspace, show = setup(ctx, ["ana@example.com"])
    real_send = PracticeMailer.send
    calls = []

    def send_once_then_lose_the_answer(self, s, outgoing, now):
        calls.append(outgoing.send_token)
        if len(calls) == 1:
            self._take(s, outgoing, now, "deliver")  # the server took it...
            raise mailers.MailerTimeout("no answer")  # ...but we never heard back
        return real_send(self, s, outgoing, now)

    monkeypatch.setattr(PracticeMailer, "send", send_once_then_lose_the_answer)
    worker.simulate(ctx, T0, T0 + timedelta(hours=1))
    assert calls[0] == calls[1]  # the retry used the same token...
    assert ctx.scalar(select(func.count()).select_from(PracticeMail)) == 1  # ...so it went once
    assert message_for(ctx, "ana@example.com").status in sending.ACCEPTED


def test_a_clear_refusal_gets_a_new_token(ctx):
    _workspace, show = setup(ctx, ["flaky.ana@example.com"])
    tokens = []
    real_send = PracticeMailer.send

    def spy(self, s, outgoing, now):
        tokens.append(outgoing.send_token)
        return real_send(self, s, outgoing, now)

    import oncue.practice_server as ps
    original = ps.PracticeMailer.send
    ps.PracticeMailer.send = spy
    try:
        worker.simulate(ctx, T0, T0 + timedelta(hours=1))
    finally:
        ps.PracticeMailer.send = original
    assert len(tokens) == 3 and len(set(tokens)) == 3


# ---------- Brevo ----------

class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def outgoing():
    return mailers.Outgoing(workspace_id=1, message_id=9, to="ana@example.com", from_name="Teatro",
                            from_email="hi@teatro.example", subject="Hi", html="<p>Hi</p>", text="Hi",
                            send_token="t", headers={"List-Unsubscribe": "<https://x/u>"})


def http_error(code, body=b'{"message": "nope"}', headers=None):
    return urllib.error.HTTPError("https://api.brevo.com", code, "error", headers or {}, io.BytesIO(body))


def test_brevo_answers_become_our_answers():
    sent = {}

    def accept(request, timeout):
        sent.update(json.loads(request.data), key=request.headers["Api-key"])
        return FakeResponse(b'{"messageId": "<abc@smtp-relay.mailin.fr>"}')

    result = BrevoMailer("key-1", opener=accept).send(None, outgoing(), T0)
    assert result == mailers.Accepted("<abc@smtp-relay.mailin.fr>")
    assert sent["key"] == "key-1" and sent["to"] == [{"email": "ana@example.com"}]
    assert sent["headers"]["List-Unsubscribe"] == "<https://x/u>" and sent["tags"] == ["oncue-message-9"]

    def refuse(code, headers=None):
        def opener(request, timeout):
            raise http_error(code, headers=headers)
        return BrevoMailer("k", opener=opener).send(None, outgoing(), T0)

    assert isinstance(refuse(400), mailers.Rejected)
    busy = refuse(429, {"Retry-After": "12"})
    assert isinstance(busy, mailers.TryLater) and busy.retry_after == 12 and busy.status == 429
    assert "API key" in refuse(401).reason
    assert isinstance(refuse(502), mailers.TryLater)

    def silent(request, timeout):
        raise TimeoutError("timed out")
    with pytest.raises(mailers.MailerTimeout):
        BrevoMailer("k", opener=silent).send(None, outgoing(), T0)


def test_a_brevo_timeout_makes_the_email_unsure_not_double(app, ctx, monkeypatch):
    workspace, show = setup(ctx, ["ana@example.com"], mailer="brevo")
    app.config["BREVO_API_KEY"] = "k"

    def silent(self, s, outgoing, now):
        raise mailers.MailerTimeout("no answer")

    monkeypatch.setattr(BrevoMailer, "send", silent)
    worker.simulate(ctx, T0, T0 + timedelta(hours=1))
    message = message_for(ctx, "ana@example.com")
    assert message.status == "unsure" and "may or may not have gone" in message.last_error
    assert ctx.scalar(select(Job.status).where(Job.kind == "send_message")) == "done"  # not retried
    assert ctx.get(Show, show.id).status == "curtain_down"


def test_a_demo_always_uses_the_practice_server(app, ctx):
    app.config["BREVO_API_KEY"] = "k"
    demo = Workspace(name="Demo", is_demo=True, mailer="brevo")
    assert mailers.for_workspace(demo).name == "practice"


# ---------- Settings and Send a Test ----------

def test_sending_settings_are_checked(app, owner):
    client, workspace_id = owner
    bad = post(client, "/settings", {"action": "sending", "send_rate": "999", "daily_cap": "0", "mailer": "brevo"})
    page = text(bad)
    assert bad.status_code == 400 and "from 0.2 to 50" in page and "Set BREVO_API_KEY" in page
    post(client, "/settings", {"action": "sending", "send_rate": "2.5", "daily_cap": "250", "mailer": "practice"})
    post(client, "/settings", {"action": "domains", "domains": "gmail.com 60\n@Yahoo.com, 30"})
    wrong = post(client, "/settings", {"action": "domains", "domains": "gmail.com sixty"})
    assert "write a domain and a number" in text(wrong)
    post(client, "/settings", {"action": "practice", "practice_busy": "20", "practice_timeout": "5",
                               "practice_bounce": "5", "practice_slow": "0"})
    with app.app_context():
        s = db.session()
        workspace = s.get(Workspace, workspace_id)
        assert (workspace.send_rate, workspace.daily_cap, workspace.practice_busy) == (2.5, 250, 20)
        from oncue.models import DomainLimit
        assert [(d.domain, d.per_minute) for d in s.scalars(select(DomainLimit).order_by(DomainLimit.domain))] == \
            [("gmail.com", 60), ("yahoo.com", 30)]
    assert "gmail.com 60" in text(client.get("/settings"))


def test_send_a_test(app, owner):
    client, workspace_id = owner
    with app.app_context():
        s = db.session()
        s.add(Contact(workspace_id=workspace_id, email="ana@example.com", first_name="Ana", fields={}))
        show = Show(workspace_id=workspace_id, name="Opening", subject="Hi {{first_name}}", body="x")
        s.add(show)
        s.commit()
        show_id = show.id
    response = post(client, f"/shows/{show_id}/test", {"email": "me@example.com"}, follow_redirects=True)
    assert "A test is on its way to the practice mail server" in text(response)
    with app.app_context():
        s = db.session()
        worker.run_until_idle(s)
        mail = s.scalar(select(PracticeMail))
        assert mail.to == "me@example.com" and mail.subject == "[Test] Hi Ana"
        assert s.get(Show, show_id).status == "rehearsal"  # a test doesn't start the show
    assert "me@example.com" in text(client.get(f"/shows/{show_id}"))
