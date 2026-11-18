"""The crew: workers that run the queue.

A worker repeats one ROUND:
1. reap(): put back jobs whose worker died (their lease ran out);
2. run_due(): start the shows whose time has come (scheduler.py);
3. work(): claim due jobs and run them;
4. heartbeat: write last_seen, so Backstage knows it is alive.

Then it sleeps. How long is a real decision: asking the database every second
is simple, but on the live site it would keep the free Neon database awake
all day and use up its free hours. So a worker sleeps UNTIL THE NEXT THING IS
DUE (the next queued job, the next show on Standby, the next lease to check),
at most IDLE_MAX, and a page that adds work (Start now, Resume) wakes it
early with nudge().

Where workers run:
- `flask --app oncue worker`: its own process (start two to watch them share
  the work: neither ever takes the other's job);
- a thread inside the web app (RUN_WORKER, on by default), started by the
  first request. Render's free plan has no separate worker service, so this
  is how the live site sends;
- the timer (/tasks/tick, tick.py) runs rounds until nothing is due, so a show
  still goes out when the site was asleep.
"""
import logging
import os
import random
import secrets
import threading
from datetime import timedelta

import click
from flask.cli import with_appcontext
from sqlalchemy import select, update

from . import db, queue, scheduler, shows
from .dates import utcnow
from .models import Job, Show, Worker

log = logging.getLogger(__name__)
POLL_MIN = 0.5  # seconds: never spin faster than this
IDLE_MAX = 30 * 60  # seconds: sleep at most this long with nothing to do
JOBS_PER_ROUND = 10
QUIET_AFTER = timedelta(minutes=2)  # a worker silent this long is shown as gone quiet


def worker_name(kind):
    return f"{kind}-{os.getpid()}-{secrets.token_hex(2)}"


def register(s, worker_id, kind, now):
    s.merge(Worker(id=worker_id, kind=kind, started_at=now, last_seen=now, jobs_done=0))
    s.commit()


def beat(s, worker_id, now, done):
    s.execute(update(Worker).where(Worker.id == worker_id)
              .values(last_seen=now, jobs_done=Worker.jobs_done + done).execution_options(synchronize_session=False))
    s.commit()


def sign_off(s, worker_id, now):
    s.execute(update(Worker).where(Worker.id == worker_id).values(stopped_at=now, last_seen=now)
              .execution_options(synchronize_session=False))
    s.commit()


def run_round(s, worker_id, now, rng=random, workspace_id=None):
    """One round. Returns how many jobs ran."""
    s.expire_all()  # read everything fresh: pages change shows between rounds
    if workspace_id is None:
        queue.reap(s, now)  # leases are real time: only the live crew checks them
    scheduler.run_due(s, now, workspace_id)
    done = queue.work(s, worker_id, now, JOBS_PER_ROUND, rng, workspace_id)
    beat(s, worker_id, now, done)
    return done


def next_event(s, now, workspace_id=None):
    """When something will next need a worker: a queued job, a show on Standby, a lease to check.

    Leases only matter to the live crew (it runs the reaper). A one-workspace replay must not
    look at them: another worker's lease that ran out while that worker was dead is a moment in
    the past, and a replay jumping to it would jump to the same moment forever."""
    moments = [
        queue.next_due(s, workspace_id),
        s.scalar(select(Show.send_at).where(Show.status == shows.STANDBY,
                                            queue.in_scope(Show.workspace_id, workspace_id))
                 .order_by(Show.send_at).limit(1)),
    ]
    if workspace_id is None:
        moments.append(s.scalar(select(Job.lease_until).where(Job.status == queue.RUNNING)
                                .order_by(Job.lease_until).limit(1)))
    moments = [moment for moment in moments if moment is not None]
    return min(moments) if moments else None


def seconds_to_sleep(s, now):
    upcoming = next_event(s, now)
    if upcoming is None:
        return IDLE_MAX
    return min(IDLE_MAX, max(POLL_MIN, (upcoming - now).total_seconds()))


class Crew:
    """One worker loop (a thread in the web app, or `flask worker`)."""

    def __init__(self, app, kind):
        self.app, self.kind = app, kind
        self.id = worker_name(kind.split()[0])
        self.stopping = threading.Event()
        self.wake = threading.Event()

    def loop(self):
        with self.app.app_context():
            s = db.new_session()
            try:
                register(s, self.id, self.kind, utcnow())
                while not self.stopping.is_set():
                    try:
                        done = run_round(s, self.id, utcnow())
                        pause = 0 if done else seconds_to_sleep(s, utcnow())
                    except Exception:  # a dropped database connection: wait, then carry on
                        log.exception("A worker round failed")
                        s.rollback()
                        pause = 5
                    if pause:
                        self.wake.wait(pause)
                        self.wake.clear()
                sign_off(s, self.id, utcnow())
            finally:
                s.close()

    def stop(self):
        self.stopping.set()
        self.wake.set()


_in_app = {"crew": None, "thread": None}
_lock = threading.Lock()


def in_app_running():
    thread = _in_app["thread"]
    return thread is not None and thread.is_alive()


def start_in_app(app):
    """The worker thread inside the web app: started once per process, on the first request,
    and again if it has stopped (Backstage's Start a Worker button)."""
    with _lock:
        if in_app_running():
            return _in_app["crew"]
        crew = Crew(app, "in the web app")
        thread = threading.Thread(target=crew.loop, name="oncue-worker", daemon=True)
        thread.start()
        _in_app["crew"], _in_app["thread"] = crew, thread
        return crew


def nudge():
    """New work right now (Start now, Resume): wake the in-app worker instead of waiting."""
    crew = _in_app["crew"]
    if crew is not None:
        crew.wake.set()


def run_until_idle(s, worker_id="timer", now_fn=utcnow, max_rounds=500, budget_seconds=None):
    """Rounds until nothing is due (the timer, and tests). Returns how many jobs ran."""
    started, total = utcnow(), 0
    for _ in range(max_rounds):
        done = run_round(s, worker_id, now_fn())
        total += done
        if not done or (budget_seconds and (utcnow() - started).total_seconds() > budget_seconds):
            break
    return total


def simulate(s, start, until, worker_id="rehearsal", rng=None, workspace_id=None, on_round=None):
    """Play the queue forward in time, jumping from one due moment to the next,
    without waiting for real seconds. Tests use it ("an hour later, the third
    retry went out"), and so does the demo, to play past shows. Returns the
    moment it stopped at."""
    rng = rng or random.Random(7)
    register(s, worker_id, "rehearsal", start)
    now = start
    while now <= until:
        if on_round:
            on_round()  # e.g. keep a long job's lease (the demo's replay)
        if run_round(s, worker_id, now, rng, workspace_id):
            continue
        upcoming = next_event(s, now, workspace_id)
        if upcoming is None or upcoming > until:
            break
        # Always move forward: a "next" moment that isn't later than now (something nothing in
        # this round could run) must never make the loop stand still.
        now = max(now + timedelta(milliseconds=1), upcoming)
    sign_off(s, worker_id, min(now, until))
    return now


# ---------- Command line ----------

@click.command("worker")
@with_appcontext
@click.option("--once", is_flag=True, help="Run until nothing is due, then stop.")
def worker_command(once):
    """Run a worker in this window (Ctrl+C to stop)."""
    from flask import current_app
    if once:
        s = db.new_session()
        try:
            name = worker_name("once")
            register(s, name, "own process", utcnow())
            click.echo(f"Ran {run_until_idle(s, name)} jobs.")
            sign_off(s, name, utcnow())
        finally:
            s.close()
        return
    crew = Crew(current_app._get_current_object(), "own process")
    click.echo(f"{crew.id} is on duty. Press Ctrl+C to stop (the job in hand finishes first).")
    thread = threading.Thread(target=crew.loop, name="oncue-worker")
    thread.start()
    try:
        while thread.is_alive():
            thread.join(0.5)
    except KeyboardInterrupt:
        click.echo("Stopping after the job in hand...")
        crew.stop()
        thread.join()
    click.echo("Off duty.")


def init_app(app):
    app.cli.add_command(worker_command)

    @app.before_request
    def start_the_crew():
        if app.config.get("RUN_WORKER") and not app.testing and _in_app["crew"] is None:
            start_in_app(app)
