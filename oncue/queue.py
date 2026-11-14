"""The job queue: background work in a database table, written by hand.

Why a table and not Redis? Render's free plan has no Redis and no separate
worker, and a table is enough to learn every idea a real queue uses:

ENQUEUE   enqueue() adds a row: what to do (kind + payload) and when (run_at).
          dedupe_key is UNIQUE, so the same work can't be queued twice.

CLAIM     claim() takes due jobs for one worker. Two workers must never take
          the same job:
          - PostgreSQL: SELECT ... FOR UPDATE SKIP LOCKED. Each worker locks the
            rows it picked; another worker skips locked rows instead of waiting.
          - SQLite (one writer at a time) and both, as a second guard:
            UPDATE jobs SET status='running' WHERE id = ? AND status = 'queued'.
            Only one UPDATE can match; the loser sees rowcount 0 and moves on.

LEASE     A running job belongs to its worker until lease_until. A long job
          extends it (heartbeat). If the worker dies, nobody extends it, and
          reap() puts the job back. So delivery is AT LEAST ONCE: a job may run
          twice, and every handler must be safe to run twice (idempotent).

RETRY     A handler raises Retry when trying again later may work. The wait
          grows exponentially (30 s, 60 s, 2 min, ...) up to an hour, with FULL
          JITTER: a random wait between 0 and that, so a thousand emails that
          failed together don't all come back at the same second. A mail
          service's Retry-After wins over our guess.

DEFER     Defer is "not now, and not your fault" (a rate limit, quiet hours):
          the job waits without using up a try.

DEAD      After max_attempts tries the job is a DEAD LETTER. Nothing retries it
          by itself; Backstage shows it with Retry Now and Discard.

Handlers get (s, job, now). Whatever they change is committed with the job's
"done". If they raise, their changes are rolled back, unless they committed
first on purpose (the sender saves "On Stage" before calling the mail service).
"""
import logging
import random
import traceback
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from . import db
from .models import Job

log = logging.getLogger(__name__)

QUEUED, RUNNING, DONE, DEAD, DISCARDED = "queued", "running", "done", "dead", "discarded"
LEASE = timedelta(seconds=60)
BACKOFF_BASE = 30  # seconds before the first retry (at most)
BACKOFF_CAP = 3600  # never wait more than an hour between tries
MAX_ATTEMPTS = 6


class Retry(Exception):
    """Try again later. after: seconds to wait (None = the backoff decides)."""
    def __init__(self, reason, after=None):
        super().__init__(reason)
        self.after = after


class Defer(Exception):
    """Not now, and it doesn't count as a try (a rate limit, quiet hours)."""
    def __init__(self, until, reason=""):
        super().__init__(reason)
        self.until = until


class GiveUp(Exception):
    """No point trying again: straight to dead letters."""


@dataclass
class Handler:
    run: object
    on_dead: object = None  # function(s, job, now): tidy up when the job dies (a message becomes Dropped)


HANDLERS = {}


def handler(kind, on_dead=None):
    """Register the function that does one kind of job."""
    def register(function):
        HANDLERS[kind] = Handler(function, on_dead)
        return function
    return register


# ---------- Putting work in ----------

def enqueue(s, kind, payload=None, *, run_at, workspace_id=None, priority=5, dedupe_key=None,
            max_attempts=MAX_ATTEMPTS):
    """Add a job (the caller commits). With a dedupe_key that is already taken,
    nothing is added and the existing job is returned."""
    if dedupe_key:
        existing = s.scalar(select(Job).where(Job.dedupe_key == dedupe_key))
        if existing is not None:
            return existing
    job = Job(kind=kind, payload=payload or {}, run_at=run_at, workspace_id=workspace_id, priority=priority,
              dedupe_key=dedupe_key, max_attempts=max_attempts, created_at=run_at)
    if not dedupe_key:
        s.add(job)
        s.flush()
        return job
    try:
        with s.begin_nested():  # a savepoint: if another worker added the same key a moment ago, only this undoes
            s.add(job)
            s.flush()
    except IntegrityError:
        return s.scalar(select(Job).where(Job.dedupe_key == dedupe_key))
    return job


# ---------- Taking work out ----------

def claim(s, worker_id, now, limit=10):
    """Take up to `limit` due jobs for this worker. Returns their ids (committed)."""
    due = (select(Job.id).where(Job.status == QUEUED, Job.run_at <= now)
           .order_by(Job.priority, Job.run_at, Job.id).limit(limit))
    if db.is_postgres(s):
        due = due.with_for_update(skip_locked=True)
    claimed = []
    for job_id in s.scalars(due).all():
        result = s.execute(update(Job).where(Job.id == job_id, Job.status == QUEUED)
                           .values(status=RUNNING, locked_by=worker_id, lease_until=now + LEASE,
                                   attempts=Job.attempts + 1, started_at=now)
                           .execution_options(synchronize_session=False))
        if result.rowcount == 1:
            claimed.append(job_id)
    s.commit()
    return claimed


def heartbeat(s, job, worker_id, now):
    """Keep the lease of a long job (the fan-out of a big audience)."""
    s.execute(update(Job).where(Job.id == job.id, Job.locked_by == worker_id, Job.status == RUNNING)
              .values(lease_until=now + LEASE).execution_options(synchronize_session=False))


def backoff(attempt, rng=random):
    """Seconds to wait before try number attempt + 1: full jitter on an exponential cap."""
    ceiling = min(BACKOFF_CAP, BACKOFF_BASE * 2 ** (attempt - 1))
    return max(1.0, rng.uniform(0, ceiling))


def _finish(job, status, now, error=None):
    job.status, job.finished_at, job.lease_until, job.locked_by = status, now, None, None
    if error is not None:
        job.last_error = error[:2000]


def fail(s, job, now, error, after=None, rng=random):
    """A try failed. Queue it again after a wait, or make it a dead letter."""
    if job.attempts >= job.max_attempts:
        die(s, job, now, f"Gave up after {job.attempts} tries. Last error: {error}")
        return
    wait = after if after is not None else backoff(job.attempts, rng)
    job.status, job.run_at, job.lease_until, job.locked_by = QUEUED, now + timedelta(seconds=wait), None, None
    job.last_error = error[:2000]


def die(s, job, now, error):
    _finish(job, DEAD, now, error)
    entry = HANDLERS.get(job.kind)
    if entry and entry.on_dead:
        entry.on_dead(s, job, now)


def defer(job, until, reason=""):
    """Wait until `until` without using up a try."""
    job.status, job.run_at, job.lease_until, job.locked_by = QUEUED, until, None, None
    job.attempts = max(job.attempts - 1, 0)
    if reason:
        job.last_error = reason[:2000]


def reap(s, now):
    """Put back running jobs whose lease ran out (their worker stopped). Returns how many."""
    stuck = s.scalars(select(Job).where(Job.status == RUNNING, Job.lease_until < now)
                      .execution_options(populate_existing=True)).all()
    for job in stuck:
        note = f"The worker {job.locked_by} stopped during the job (its lease ran out)."
        if job.attempts >= job.max_attempts:
            die(s, job, now, note)
        else:
            job.status, job.run_at, job.lease_until, job.locked_by = QUEUED, now, None, None
            job.last_error = note + " Trying again."
    s.commit()
    return len(stuck)


def run_job(s, job_id, worker_id, now, rng=random):
    """Run one claimed job and record how it went. Returns its new status."""
    # Forget every copy this session holds: a worker's session lives for many jobs, and
    # meanwhile a page may have put the show on Hold. Each job reads everything fresh.
    s.expire_all()
    job = s.get(Job, job_id)
    if job is None or job.status != RUNNING or job.locked_by != worker_id:
        return None
    entry = HANDLERS.get(job.kind)
    try:
        if entry is None:
            raise GiveUp(f"Nobody knows how to do a {job.kind!r} job.")
        entry.run(s, job, now)
    except Defer as wait:
        s.rollback()
        job = s.get(Job, job_id)
        defer(job, wait.until, str(wait))
    except Retry as problem:
        s.rollback()
        job = s.get(Job, job_id)
        fail(s, job, now, str(problem), problem.after, rng)
    except GiveUp as problem:
        s.rollback()
        job = s.get(Job, job_id)
        die(s, job, now, str(problem))
    except Exception as problem:  # a bug or a dropped connection: retry, and keep the details for Backstage
        log.exception("Job %s (%s) failed", job_id, job.kind)
        s.rollback()
        job = s.get(Job, job_id)
        fail(s, job, now, "".join(traceback.format_exception_only(problem)).strip(), rng=rng)
    else:
        if job.status == RUNNING:  # a handler may have re-queued its own job
            _finish(job, DONE, now)
    s.commit()
    return job.status


def work(s, worker_id, now, limit=10, rng=random):
    """Claim due jobs and run them. Returns how many ran."""
    ids = claim(s, worker_id, now, limit)
    for job_id in ids:
        run_job(s, job_id, worker_id, now, rng)
    return len(ids)


# ---------- Dead letters (Backstage) ----------

def retry_now(s, job, now):
    """A dead letter gets a fresh set of tries, starting now."""
    job.status, job.run_at, job.attempts, job.finished_at = QUEUED, now, 0, None
    job.last_error = f"Retried by hand. Before: {job.last_error}"[:2000]


def discard(s, job, now):
    _finish(job, DISCARDED, now)


def next_due(s):
    """When the next queued job is due (None if nothing is queued)."""
    return s.scalar(select(Job.run_at).where(Job.status == QUEUED).order_by(Job.run_at).limit(1))
