"""Sending limits: token buckets, the daily cap, and the Safety Curtain.

TOKEN BUCKET (how fast). Picture a bucket that holds up to `capacity` tokens
and fills at `rate` tokens a second. Each email takes one token; an empty
bucket means "wait until the next token drips in". That allows a small burst
and then a steady pace. We keep three per workspace:
- rate:<workspace>:<mailer>  - emails a second (Settings; Brevo's limit is generous, ours is polite);
- domain:<workspace>:<domain> - emails a minute to one receiving domain (gmail.com...), if set.
After a 429 ("too many requests") the mailer's bucket fills at HALF speed for
10 minutes, then goes back to normal: we slow down when the service asks.

Two workers may take tokens at the same moment, and both could read
"1 token left". So a bucket changes only by COMPARE-AND-SWAP:
    UPDATE buckets SET tokens = <new> WHERE key = ? AND updated_at = <what I read>
If another worker changed it in between, nothing matches (rowcount 0), and
we read again and recompute. Nobody holds a lock while thinking.

DAILY CAP (how many). Brevo's free plan takes 300 emails a day. One UPDATE
both checks and counts, so it can't be beaten by a race:
    UPDATE send_counts SET count = count + 1 WHERE key = ? AND count < cap
rowcount 1: go ahead. rowcount 0: the day is full; wait for tomorrow.

THE SAFETY CURTAIN (a circuit breaker). If the mail service fails 10 times
in a row, sending more just piles up failures (and may get us blocked). So
the curtain comes down: nothing is sent for 5 minutes. Then ONE email (the
probe) goes. If it works, the curtain goes up; if not, down for another 5.
Failures that are about one address (a bounce) don't count, only the
service being down or refusing us.

None of these count as a try: an email waiting for a token is deferred
(queue.Defer), not retried.
"""
import math
import random
from datetime import datetime, time, timedelta

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError

from . import mailers, queue, sending
from .models import Breaker, Bucket, DomainLimit, Job, SendCount

SLOW_DOWN_FOR = timedelta(minutes=10)
TRIP_AFTER = 10  # failures in a row
CURTAIN_DOWN_FOR = timedelta(minutes=5)
PROBE_GRACE = timedelta(minutes=2)  # a probe that never reported back (its worker died) is given up on
CAS_TRIES = 8
EPSILON = 1e-9  # floating-point dust: 0.9999999999 tokens is one token
MIN_WAIT = 0.05  # seconds: a wait that rounds to nothing would make a job spin


# ---------- Token buckets ----------

def _get_or_make(s, model, key, workspace_id, **fields):
    row = s.get(model, key, populate_existing=True)
    if row is None:
        try:
            with s.begin_nested():
                row = model(key=key, workspace_id=workspace_id, **fields)
                s.add(row)
                s.flush()
        except IntegrityError:  # another worker made it first
            row = s.get(model, key, populate_existing=True)
    return row


def take(s, key, workspace_id, capacity, rate, now):
    """Take one token. Returns 0 if taken, else the seconds to wait for one."""
    capacity = max(1.0, capacity)
    for _ in range(CAS_TRIES):
        bucket = _get_or_make(s, Bucket, key, workspace_id, tokens=capacity, updated_at=now)
        speed = rate / 2 if bucket.slow_until and bucket.slow_until > now else rate
        elapsed = max(0.0, (now - bucket.updated_at).total_seconds())
        tokens = min(capacity, bucket.tokens + elapsed * speed)
        if tokens < 1 - EPSILON:
            return max(MIN_WAIT, (1 - tokens) / speed)
        moved = s.execute(update(Bucket).where(Bucket.key == key, Bucket.updated_at == bucket.updated_at,
                                               Bucket.tokens == bucket.tokens)
                          .values(tokens=max(0.0, tokens - 1), updated_at=max(now, bucket.updated_at))
                          .execution_options(synchronize_session=False))
        if moved.rowcount == 1:
            return 0
    return 1.0  # very busy: try again in a second


def slow_down(s, key, workspace_id, now):
    bucket = _get_or_make(s, Bucket, key, workspace_id, tokens=0, updated_at=now)
    bucket.slow_until = now + SLOW_DOWN_FOR


def level(s, key, capacity, rate, now):
    """How full a bucket is now (for Backstage's gauges): (tokens, slowed down?)."""
    bucket = s.get(Bucket, key)
    if bucket is None:
        return capacity, False
    slow = bool(bucket.slow_until and bucket.slow_until > now)
    speed = rate / 2 if slow else rate
    return min(capacity, bucket.tokens + max(0.0, (now - bucket.updated_at).total_seconds()) * speed), slow


# ---------- The daily cap ----------

def day_key(workspace_id, mailer, now):
    return f"day:{workspace_id}:{mailer}:{now.date().isoformat()}"


def count_one(s, workspace_id, mailer, cap, now):
    """Count one email against today's cap (UTC days, like Brevo). True if there was room."""
    key = day_key(workspace_id, mailer, now)
    _get_or_make(s, SendCount, key, workspace_id, count=0)
    counted = s.execute(update(SendCount).where(SendCount.key == key, SendCount.count < cap)
                        .values(count=SendCount.count + 1).execution_options(synchronize_session=False))
    return counted.rowcount == 1


def sent_today(s, workspace_id, mailer, now):
    row = s.get(SendCount, day_key(workspace_id, mailer, now))
    return row.count if row else 0


def tomorrow(now):
    return datetime.combine(now.date() + timedelta(days=1), time(0, 0))


def finish_estimate(s, workspace, mailer, waiting, now):
    """About when `waiting` more emails will have gone, at this rate and daily cap."""
    if waiting <= 0:
        return now
    room_today = max(0, workspace.daily_cap - sent_today(s, workspace.id, mailer, now))
    seconds = waiting / max(workspace.send_rate, 0.01)
    if waiting <= room_today:
        return now + timedelta(seconds=seconds)
    rest = waiting - room_today
    days = math.ceil(rest / max(workspace.daily_cap, 1))
    last_day = rest - (days - 1) * workspace.daily_cap
    return tomorrow(now) + timedelta(days=days - 1, seconds=last_day / max(workspace.send_rate, 0.01))


# ---------- The Safety Curtain ----------

def breaker_key(workspace_id, mailer):
    return f"{workspace_id}:{mailer}"


def breaker(s, workspace_id, mailer):
    return s.get(Breaker, breaker_key(workspace_id, mailer), populate_existing=True)


def _cas_breaker(s, row, **values):
    """Change the breaker only if nobody else changed it since we read it."""
    moved = s.execute(update(Breaker).where(Breaker.key == row.key, Breaker.version == row.version)
                      .values(version=row.version + 1, **values).execution_options(synchronize_session=False))
    return moved.rowcount == 1


def allow(s, workspace_id, mailer, now):
    """Raise queue.Defer while the curtain is down. One worker gets to send the probe."""
    row = breaker(s, workspace_id, mailer)
    if row is None or row.state == "closed":
        return
    if row.state == "open" and now < row.retry_at:
        raise queue.Defer(row.retry_at, "The Safety Curtain is down: the mail service kept failing.")
    if row.state == "half_open" and row.probe_at and now - row.probe_at < PROBE_GRACE:
        raise queue.Defer(now + timedelta(seconds=15), "Waiting for the Safety Curtain's test email.")
    # Time for a probe: only the worker whose compare-and-swap wins sends it.
    if not _cas_breaker(s, row, state="half_open", probe_at=now):
        raise queue.Defer(now + timedelta(seconds=15), "Another worker is sending the test email.")
    s.commit()


def record(s, workspace_id, mailer, ok, now, error=""):
    """After an answer from the mail service: count failures, bring the curtain down or up."""
    row = breaker(s, workspace_id, mailer)
    if row is None:
        if ok:
            return
        row = _get_or_make(s, Breaker, breaker_key(workspace_id, mailer), workspace_id, mailer=mailer,
                           state="closed", failures=0, version=1)
    for _ in range(CAS_TRIES):
        if ok:
            values = {"state": "closed", "failures": 0, "probe_at": None}
            if row.state == "closed" and row.failures == 0:
                return
        elif row.state == "half_open" or row.failures + 1 >= TRIP_AFTER:
            values = {"state": "open", "failures": row.failures + 1, "opened_at": now,
                      "retry_at": now + CURTAIN_DOWN_FOR, "probe_at": None, "last_error": error[:300]}
        else:
            values = {"failures": row.failures + 1, "last_error": error[:300]}
        if _cas_breaker(s, row, **values):
            return
        row = breaker(s, workspace_id, mailer)


def raise_curtain(s, workspace_id, mailer):
    """Backstage's Raise Curtain button: try again now."""
    row = breaker(s, workspace_id, mailer)
    if row is not None:
        _cas_breaker(s, row, state="closed", failures=0, probe_at=None, retry_at=None)


# ---------- Hooked into the sender ----------

def domain_of(email):
    return email.rpartition("@")[2].lower()


def backlog(s, workspace_id, now):
    """How many send jobs of this workspace are waiting within the next hour. (Not only the ones
    due this second: once spread out, those are few, and the herd would come back.)"""
    return s.scalar(select(func.count()).select_from(Job).where(
        Job.workspace_id == workspace_id, Job.kind == "send_message", Job.status == queue.QUEUED,
        Job.run_at <= now + timedelta(hours=1)))


def wait_your_turn(s, workspace_id, wait, rate, now, reason):
    """Defer to a time spread over the whole backlog, not "everyone in 0.2 seconds".

    When 100 emails are due and the bucket allows 5 a second, telling all the
    losers to come back when the next token drips in makes them all come back
    at once, again and again (a thundering herd): 100 job runs to send 1 email.
    The backlog will take backlog / rate seconds anyway, so each waits a random
    moment in that span: most then find a token on their first return."""
    spread = (backlog(s, workspace_id, now) + 1) / max(rate, 0.01)  # + 1: this email itself
    s.commit()
    raise queue.Defer(now + timedelta(seconds=max(MIN_WAIT, wait) + random.uniform(0, spread)), reason)


def before_send(s, message, workspace, contact, now):
    """Every limit, checked just before an email goes. Raises queue.Defer to wait."""
    mailer = mailers.for_workspace(workspace).name
    allow(s, workspace.id, mailer, now)
    limit = s.scalar(select(DomainLimit).where(DomainLimit.workspace_id == workspace.id,
                                               DomainLimit.domain == domain_of(message.email)))
    if limit is not None:
        wait = take(s, f"domain:{workspace.id}:{limit.domain}", workspace.id, max(1, limit.per_minute / 6),
                    limit.per_minute / 60, now)
        if wait:
            wait_your_turn(s, workspace.id, wait, limit.per_minute / 60, now,
                           f"Pacing {limit.domain}: {limit.per_minute} a minute.")
    wait = take(s, f"rate:{workspace.id}:{mailer}", workspace.id, workspace.send_rate, workspace.send_rate, now)
    if wait:
        wait_your_turn(s, workspace.id, wait, workspace.send_rate, now,
                       f"Pacing: {workspace.send_rate:g} emails a second.")
    if not message.is_test and not count_one(s, workspace.id, mailer, workspace.daily_cap, now):
        s.commit()
        raise queue.Defer(tomorrow(now), f"Today's {workspace.daily_cap} emails are used up. More tomorrow.")
    s.commit()


def after_send(s, message, workspace, result, now):
    mailer = message.mailer or mailers.for_workspace(workspace).name
    if isinstance(result, mailers.TryLater):
        record(s, workspace.id, mailer, False, now, result.reason)
        if result.status == 429:
            slow_down(s, f"rate:{workspace.id}:{mailer}", workspace.id, now)
    else:
        # Accepted, or Rejected for one address: the service itself is working.
        record(s, workspace.id, mailer, True, now)


def init_app(app):
    if before_send not in sending.before_send:
        sending.before_send.append(before_send)
    if after_send not in sending.after_send:
        sending.after_send.append(after_send)
