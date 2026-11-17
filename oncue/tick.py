"""The timer: wakes the site, runs the crew until nothing is due, tidies up.

Render's free plan sleeps when nobody visits, and a sleeping site sends
nothing. GitHub Actions calls POST /tasks/tick on a schedule
(.github/workflows/tick.yml). The call wakes the site, and the timer itself:
- runs worker rounds until nothing is due (at most ~25 seconds per call);
- deletes demos whose 24 hours are up, and CSV imports left half-way;
- forgets finished jobs after a week and stopped workers after a day.

Any request without the right X-Tick-Key header gets a 404, so nobody can
find it. From the command line: flask --app oncue tick.
"""
import hmac
from datetime import timedelta

import click
from flask import Blueprint, abort, current_app, jsonify, request
from flask.cli import with_appcontext
from sqlalchemy import delete

from . import csv_import, db, queue, seed, worker
from .dates import utcnow
from .models import Job, Worker

bp = Blueprint("tick", __name__)
BUDGET_SECONDS = 25
KEEP_DONE_JOBS = timedelta(days=7)
KEEP_STOPPED_WORKERS = timedelta(days=1)


def tidy(s, moment):
    done = {"demos_deleted": seed.purge(s, moment), "imports_deleted": csv_import.purge(s, moment)}
    done["old_jobs_deleted"] = s.execute(delete(Job).where(Job.status.in_((queue.DONE, queue.DISCARDED)),
                                                           Job.finished_at < moment - KEEP_DONE_JOBS)).rowcount
    done["old_workers_forgotten"] = s.execute(delete(Worker).where(
        Worker.last_seen < moment - KEEP_STOPPED_WORKERS)).rowcount
    s.commit()
    return done


def run(s):
    jobs = worker.run_until_idle(s, worker_id=worker.worker_name("timer"), budget_seconds=BUDGET_SECONDS)
    return {"jobs_run": jobs, **tidy(s, utcnow())}


@bp.post("/tasks/tick")
def tick():
    key = current_app.config.get("TICK_KEY")
    sent = request.headers.get("X-Tick-Key", "")
    if not key or not hmac.compare_digest(sent, key):
        abort(404)
    s = db.new_session()
    try:
        return jsonify(run(s))
    finally:
        s.close()


@click.command("tick")
@with_appcontext
def tick_command():
    """Run the timer once: the crew until nothing is due, and the tidying."""
    s = db.new_session()
    try:
        for name, count in run(s).items():
            click.echo(f"{name}: {count}")
    finally:
        s.close()


def init_app(app):
    exempt = set(app.config.get("CSRF_EXEMPT", ()))
    exempt.add("tick.tick")  # a machine calls it, with its own secret instead of a form token
    app.config["CSRF_EXEMPT"] = exempt
    app.cli.add_command(tick_command)
