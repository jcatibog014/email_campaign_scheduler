"""From Standby to Curtain Down: the scheduler, the fan-out, sending, Hold, Cut,
a worker dying mid-send, and the timer. Time is played forward with simulate()."""
from datetime import timedelta

from sqlalchemy import func, select

from oncue import audience, db, queue, scheduler, sending, shows, worker
from oncue.dates import utcnow
from oncue.models import Contact, Job, Message, Performance, PracticeMail, Show, Workspace

from .conftest import CALM

T0 = utcnow().replace(second=0, microsecond=0)


def setup_show(ctx, people=5, send_at=None, owner=True, **settings):
    workspace = Workspace(name="Teatro", sender_name="Teatro", sender_email="hi@teatro.example",
                          postal_address="1 Stage Rd", **{**CALM, **settings})
    ctx.add(workspace)
    ctx.flush()
    for index in range(people):
        ctx.add(Contact(workspace_id=workspace.id, email=f"p{index}@example.com", first_name=f"P{index}", fields={}))
    show = Show(workspace_id=workspace.id, name="Opening", subject="Hi {{first_name}}", body="Come!",
                status="standby", send_at=send_at or T0 + timedelta(minutes=10))
    ctx.add(show)
    ctx.commit()
    return workspace, show


def count(ctx, model, *where):
    return ctx.scalar(select(func.count()).select_from(model).where(*where))


def statuses(ctx, show_id):
    """Message counts, with every "went out" status (sent, delivered, opened, clicked) counted as sent."""
    ctx.expire_all()
    collapsed = {}
    for status, number in sending.counts(ctx, show_id).items():
        key = "sent" if status in sending.ACCEPTED else status
        collapsed[key] = collapsed.get(key, 0) + number
    return collapsed


def test_a_show_goes_out_at_its_time_and_the_curtain_comes_down(ctx):
    workspace, show = setup_show(ctx)
    # Two people who must never get it: one unsubscribed, one on the do-not-send list.
    unsubscribed = ctx.scalar(select(Contact).where(Contact.email == "p3@example.com"))
    audience.unsubscribe(ctx, unsubscribed, T0)
    audience.suppress(ctx, workspace.id, "p4@example.com", "bounced", T0)
    ctx.commit()
    worker.simulate(ctx, T0, T0 + timedelta(minutes=9))
    ctx.expire_all()
    assert ctx.get(Show, show.id).status == "standby"  # not yet
    worker.simulate(ctx, T0 + timedelta(minutes=10), T0 + timedelta(minutes=11))
    ctx.expire_all()
    show = ctx.get(Show, show.id)
    assert show.status == "curtain_down" and show.started_at == T0 + timedelta(minutes=10)
    assert statuses(ctx, show.id) == {"sent": 3}
    perf = ctx.scalar(select(Performance))
    assert (perf.status, perf.audience_size, perf.fanned_out) == ("done", 3, True)
    inbox = ctx.scalars(select(PracticeMail).order_by(PracticeMail.to)).all()
    assert [mail.to for mail in inbox] == ["p0@example.com", "p1@example.com", "p2@example.com"]
    assert inbox[0].subject == "Hi P0" and "/one-click>" in inbox[0].headers["List-Unsubscribe"]
    assert inbox[0].headers["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"
    assert "/u/" in inbox[0].html  # the unsubscribe link is in every email


def test_two_schedulers_start_a_show_once(app, ctx):
    _workspace, show = setup_show(ctx, send_at=T0)
    with app.app_context():
        other = db.new_session()
        assert scheduler.run_due(ctx, T0) == 1
        assert scheduler.run_due(other, T0) == 0
        other.close()
    assert count(ctx, Performance) == 1 and count(ctx, Job, Job.kind == "start_performance") == 1


def test_the_fan_out_in_batches_and_twice_adds_nobody_twice(ctx, monkeypatch):
    monkeypatch.setattr(scheduler, "BATCH", 2)
    _workspace, show = setup_show(ctx, people=5, send_at=T0)
    scheduler.run_due(ctx, T0)
    first = ctx.scalar(select(Job).where(Job.kind == "start_performance"))
    for _ in range(2):  # the same batch twice, as after a crash
        scheduler.start_performance(ctx, first, T0)
        ctx.commit()
    assert count(ctx, Message) == 2
    worker.run_until_idle(ctx, now_fn=lambda: T0)
    assert count(ctx, Job, Job.kind == "start_performance") == 3  # 2 + 2 + 1 people
    assert count(ctx, Message) == 5 and count(ctx, PracticeMail) == 5
    ctx.expire_all()
    assert ctx.get(Show, show.id).status == "curtain_down"


def test_hold_parks_the_rest_and_resume_sends_them(ctx):
    _workspace, show = setup_show(ctx, people=4, send_at=T0)
    scheduler.run_due(ctx, T0)
    queue.work(ctx, "w1", T0, limit=1)  # the fan-out: 4 send jobs queued
    queue.work(ctx, "w1", T0, limit=1)  # one email goes
    show = ctx.get(Show, show.id)
    shows.move(ctx, show, shows.HOLD, T0)
    ctx.commit()
    worker.run_until_idle(ctx, now_fn=lambda: T0)
    assert statuses(ctx, show.id) == {"sent": 1, "wings": 3}  # parked, not lost
    show = ctx.get(Show, show.id)
    shows.move(ctx, show, shows.GO, T0 + timedelta(minutes=5))
    ctx.commit()
    worker.run_until_idle(ctx, now_fn=lambda: T0 + timedelta(minutes=5))
    assert statuses(ctx, show.id) == {"sent": 4}
    assert ctx.get(Show, show.id).status == "curtain_down"


def test_cut_stops_everything_still_waiting(ctx):
    _workspace, show = setup_show(ctx, people=4, send_at=T0)
    scheduler.run_due(ctx, T0)
    queue.work(ctx, "w1", T0, limit=1)  # the fan-out (it queues the send jobs)
    queue.work(ctx, "w1", T0, limit=1)  # one email
    show = ctx.get(Show, show.id)
    shows.move(ctx, show, shows.CUT, T0)
    ctx.commit()
    worker.run_until_idle(ctx, now_fn=lambda: T0)
    assert statuses(ctx, show.id) == {"sent": 1, "cut": 3}
    assert ctx.scalar(select(Performance.status)) == "cut" and count(ctx, PracticeMail) == 1


def test_someone_who_unsubscribes_before_their_turn_is_skipped(ctx):
    _workspace, show = setup_show(ctx, people=2, send_at=T0)
    scheduler.run_due(ctx, T0)
    queue.work(ctx, "w1", T0, limit=1)  # the fan-out only
    audience.unsubscribe(ctx, ctx.scalar(select(Contact).where(Contact.email == "p1@example.com")), T0)
    ctx.commit()
    worker.run_until_idle(ctx, now_fn=lambda: T0)
    assert statuses(ctx, show.id) == {"sent": 1, "skipped": 1}
    assert ctx.get(Show, show.id).status == "curtain_down"


def test_a_worker_dying_mid_send_never_sends_twice(ctx):
    """The practice server keeps the send_token: the retry finds the email already taken."""
    _workspace, show = setup_show(ctx, people=1, send_at=T0)
    scheduler.run_due(ctx, T0)
    queue.work(ctx, "w1", T0, limit=1)
    message = ctx.scalar(select(Message))
    # Play the crash: the worker claimed the job, saved On Stage, the server took the email...
    job_id = queue.claim(ctx, "doomed", T0, limit=1)[0]
    assert sending.claim_message(ctx, message, T0)
    contact = ctx.get(Contact, message.contact_id)
    workspace = ctx.get(Workspace, message.workspace_id)
    from oncue.practice_server import PracticeMailer
    PracticeMailer().send(ctx, sending.build(message, ctx.get(Show, show.id), workspace, contact), T0)
    ctx.commit()
    # ...and died before writing "Sent". The lease runs out; the reaper puts the job back.
    later = T0 + timedelta(minutes=2)
    assert queue.reap(ctx, later) == 1
    worker.run_until_idle(ctx, now_fn=lambda: later)
    ctx.expire_all()
    assert count(ctx, PracticeMail) == 1  # one email, not two
    assert ctx.get(Message, message.id).status in sending.ACCEPTED
    assert ctx.get(Job, job_id).attempts == 2


def test_an_empty_audience_finishes_at_once(ctx):
    _workspace, show = setup_show(ctx, people=0, send_at=T0)
    worker.simulate(ctx, T0, T0 + timedelta(minutes=1))
    assert ctx.get(Show, show.id).status == "curtain_down"


def test_the_timer_needs_its_key(app, client):
    assert client.post("/tasks/tick").status_code == 404
    app.config["TICK_KEY"] = "secret"
    assert client.post("/tasks/tick", headers={"X-Tick-Key": "wrong"}).status_code == 404
    with app.app_context():
        setup_show(db.session(), people=2, send_at=utcnow() - timedelta(minutes=1))
    answer = client.post("/tasks/tick", headers={"X-Tick-Key": "secret"}).get_json()
    assert answer["jobs_run"] == 3  # the fan-out and two emails


def test_the_worker_command_runs_once(app):
    with app.app_context():
        setup_show(db.session(), people=2, send_at=utcnow() - timedelta(minutes=1))
    result = app.test_cli_runner().invoke(args=["worker", "--once"])
    assert result.exit_code == 0 and "Ran 3 jobs." in result.output


def test_the_show_page_shows_progress(app, owner):
    client, workspace_id = owner
    with app.app_context():
        s = db.session()
        for index in range(3):
            s.add(Contact(workspace_id=workspace_id, email=f"p{index}@example.com", fields={}))
        show = Show(workspace_id=workspace_id, name="Opening", subject="Hi", body="x", status="standby", send_at=T0)
        s.add(show)
        s.commit()
        show_id = show.id
        worker.simulate(s, T0, T0 + timedelta(minutes=1))
    page = client.get(f"/shows/{show_id}").get_data(as_text=True)
    assert "3</strong> of <span data-count=\"total\">3</span> handled" in page
    assert client.get(f"/shows/{show_id}/progress.json").get_json()["meter"]["gone"] == 3
