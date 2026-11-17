"""The job queue: enqueue, claim, leases, retries with backoff, defer, dead letters."""
import random
import threading
import time
from datetime import timedelta

import pytest
from sqlalchemy import func, select

from oncue import db, queue, worker
from oncue.dates import utcnow
from oncue.models import Job

T0 = utcnow().replace(microsecond=0)
calls = []


@pytest.fixture(autouse=True)
def test_handlers():
    """A few handlers just for these tests (removed again afterwards)."""
    calls.clear()

    @queue.handler("t_ok")
    def ok(s, job, now):
        calls.append(("ok", job.id, now))

    @queue.handler("t_flaky")
    def flaky(s, job, now):
        calls.append(("flaky", job.attempts))
        if job.attempts < job.payload.get("succeed_on", 99):
            raise queue.Retry("busy", after=job.payload.get("after"))

    @queue.handler("t_defer")
    def wait(s, job, now):
        raise queue.Defer(now + timedelta(minutes=5), "rate limit")

    @queue.handler("t_bug")
    def bug(s, job, now):
        raise ZeroDivisionError("oops")

    @queue.handler("t_hopeless", on_dead=lambda s, job, now: calls.append(("on_dead", job.id)))
    def hopeless(s, job, now):
        raise queue.GiveUp("the address doesn't exist")

    yield
    for kind in ("t_ok", "t_flaky", "t_defer", "t_bug", "t_hopeless"):
        queue.HANDLERS.pop(kind, None)


def job(ctx, job_id):
    ctx.expire_all()
    return ctx.get(Job, job_id)


def test_enqueue_with_a_dedupe_key_adds_once(ctx):
    first = queue.enqueue(ctx, "t_ok", {"a": 1}, run_at=T0, dedupe_key="start:7")
    again = queue.enqueue(ctx, "t_ok", {"a": 2}, run_at=T0, dedupe_key="start:7")
    ctx.commit()
    assert first.id == again.id and ctx.scalar(select(func.count()).select_from(Job)) == 1


def test_only_due_jobs_run_in_priority_order(ctx):
    late = queue.enqueue(ctx, "t_ok", run_at=T0 + timedelta(minutes=1))
    low = queue.enqueue(ctx, "t_ok", run_at=T0, priority=9)
    high = queue.enqueue(ctx, "t_ok", run_at=T0, priority=1)
    ctx.commit()
    assert queue.work(ctx, "w1", T0) == 2
    assert [c[1] for c in calls] == [high.id, low.id]
    assert job(ctx, late.id).status == "queued"


def test_two_workers_never_take_the_same_job(app):
    """Two threads, each with its own session, claim from the same 200 jobs at once."""
    with app.app_context():
        s = db.new_session()
        for _ in range(200):
            queue.enqueue(s, "t_ok", run_at=T0)
        s.commit()
        s.close()
    taken = {}
    ready = threading.Barrier(2)  # both start at the same moment

    def take(name):
        with app.app_context():
            session = db.new_session()
            mine = []
            ready.wait()
            while True:
                batch = queue.claim(session, name, T0, limit=1)
                mine.extend(batch)
                # An empty answer can mean "the other worker won that one": stop only when none are left.
                if not batch and not session.scalar(select(func.count()).select_from(Job).where(Job.status == "queued")):
                    break
                time.sleep(0.001)  # let the other thread in between claims
            taken[name] = mine
            session.close()

    threads = [threading.Thread(target=take, args=(f"w{i}",)) for i in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    both = taken["w0"] + taken["w1"]
    assert len(both) == 200 and len(set(both)) == 200  # every job once, none twice
    assert taken["w0"] and taken["w1"]  # and both really took part


def test_retries_back_off_with_jitter_then_die(ctx):
    rng = random.Random(1)
    flaky = queue.enqueue(ctx, "t_flaky", run_at=T0, max_attempts=4)
    ctx.commit()
    now, waits = T0, []
    for attempt in range(1, 5):
        assert queue.work(ctx, "w1", now, rng=rng) == 1
        found = job(ctx, flaky.id)
        if found.status == "dead":
            break
        wait = (found.run_at - now).total_seconds()
        assert 1 <= wait <= min(3600, 30 * 2 ** (attempt - 1))  # full jitter under the exponential cap
        waits.append(wait)
        now = found.run_at
    found = job(ctx, flaky.id)
    assert found.status == "dead" and found.attempts == 4 and "Gave up after 4 tries" in found.last_error
    assert len(waits) == 3


def test_a_retry_after_from_the_service_wins(ctx):
    flaky = queue.enqueue(ctx, "t_flaky", {"after": 42}, run_at=T0)
    ctx.commit()
    queue.work(ctx, "w1", T0)
    assert job(ctx, flaky.id).run_at == T0 + timedelta(seconds=42)


def test_success_on_the_third_try(ctx):
    flaky = queue.enqueue(ctx, "t_flaky", {"succeed_on": 3, "after": 1}, run_at=T0)
    ctx.commit()
    for second in range(3):
        queue.work(ctx, "w1", T0 + timedelta(seconds=second * 2))
    found = job(ctx, flaky.id)
    assert found.status == "done" and found.attempts == 3


def test_defer_waits_without_using_a_try(ctx):
    waiting = queue.enqueue(ctx, "t_defer", run_at=T0, max_attempts=2)
    ctx.commit()
    for minute in range(0, 30, 5):
        queue.work(ctx, "w1", T0 + timedelta(minutes=minute))
    found = job(ctx, waiting.id)
    assert found.status == "queued" and found.attempts == 0 and found.last_error == "rate limit"


def test_a_crash_in_a_handler_is_retried_with_the_error_kept(ctx):
    buggy = queue.enqueue(ctx, "t_bug", run_at=T0)
    ctx.commit()
    queue.work(ctx, "w1", T0)
    found = job(ctx, buggy.id)
    assert found.status == "queued" and "ZeroDivisionError: oops" in found.last_error


def test_give_up_goes_straight_to_dead_letters(ctx):
    hopeless = queue.enqueue(ctx, "t_hopeless", run_at=T0)
    unknown = queue.enqueue(ctx, "no_such_kind", run_at=T0)
    ctx.commit()
    queue.work(ctx, "w1", T0)
    assert job(ctx, hopeless.id).status == "dead" and ("on_dead", hopeless.id) in calls
    assert job(ctx, unknown.id).status == "dead" and "Nobody knows how" in job(ctx, unknown.id).last_error


def test_a_dead_workers_job_comes_back_after_its_lease(ctx):
    stuck = queue.enqueue(ctx, "t_ok", run_at=T0)
    ctx.commit()
    assert queue.claim(ctx, "crashed-worker", T0) == [stuck.id]
    # The worker dies here. Before the lease runs out, nobody else may take it.
    assert queue.reap(ctx, T0 + timedelta(seconds=30)) == 0
    assert queue.claim(ctx, "w2", T0 + timedelta(seconds=30)) == []
    assert queue.reap(ctx, T0 + timedelta(seconds=61)) == 1
    assert "crashed-worker stopped during the job" in job(ctx, stuck.id).last_error
    assert queue.work(ctx, "w2", T0 + timedelta(seconds=61)) == 1
    found = job(ctx, stuck.id)
    assert found.status == "done" and found.attempts == 2


def test_a_heartbeat_keeps_the_lease(ctx):
    long = queue.enqueue(ctx, "t_ok", run_at=T0)
    ctx.commit()
    queue.claim(ctx, "w1", T0)
    queue.heartbeat(ctx, job(ctx, long.id), "w1", T0 + timedelta(seconds=50))
    ctx.commit()
    assert queue.reap(ctx, T0 + timedelta(seconds=70)) == 0


def test_dead_letters_can_be_retried_or_discarded(ctx):
    one = queue.enqueue(ctx, "t_hopeless", run_at=T0)
    two = queue.enqueue(ctx, "t_hopeless", run_at=T0)
    ctx.commit()
    queue.work(ctx, "w1", T0)
    queue.retry_now(ctx, job(ctx, one.id), T0)
    queue.discard(ctx, job(ctx, two.id), T0)
    ctx.commit()
    assert (job(ctx, one.id).status, job(ctx, one.id).attempts) == ("queued", 0)
    assert job(ctx, two.id).status == "discarded"


def test_the_worker_sleeps_until_the_next_thing(ctx):
    assert worker.seconds_to_sleep(ctx, T0) == worker.IDLE_MAX  # nothing at all: the longest nap
    queue.enqueue(ctx, "t_ok", run_at=T0 + timedelta(seconds=90))
    ctx.commit()
    assert worker.seconds_to_sleep(ctx, T0) == 90
    queue.enqueue(ctx, "t_ok", run_at=T0 - timedelta(seconds=5))
    ctx.commit()
    assert worker.seconds_to_sleep(ctx, T0) == worker.POLL_MIN  # overdue: straight back to work


def test_backoff_never_waits_more_than_an_hour():
    rng = random.Random(3)
    assert all(1 <= queue.backoff(attempt, rng) <= 3600 for attempt in range(1, 30))
