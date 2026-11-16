"""Sending one email: the send_message job.

The steps, and why each one is there:

1. Look again. The show may be on Hold (then the email waits: "parked") or
   Cut, and the person may have unsubscribed since the fan-out. Never trust
   what was true when the job was queued.
2. Claim the message: a guarded UPDATE from its current status to On Stage,
   with a fresh send_token. If two jobs for the same message run at once,
   only one UPDATE matches; the other stops. COMMIT, so "On Stage" is saved
   BEFORE the mail service is called.
3. Call the mailer.
4. Record the answer: Sent, Missed Cue (retry), or Dropped.

If the worker dies between 3 and 4, the message is left On Stage and the job's
lease runs out. When the job runs again it finds "On Stage" and knows a send
was in flight. With an idempotent mailer (the practice server) it sends again
with the SAME send_token, and the server answers "I already have it" instead
of sending twice. A mailer that can't promise that (Brevo) gets the message
marked Unsure instead (stage 5): a missing email is better than a double one.
"""
import secrets
from datetime import timedelta

from sqlalchemy import func, select, update

from . import audience, emails, mailers, queue, shows
from .models import Contact, Message, Performance, Show, Workspace
from .views.public import one_click_url, unsubscribe_url

WINGS, WAITING, ON_STAGE, MISSED_CUE, UNSURE = "wings", "waiting", "on_stage", "missed_cue", "unsure"
SENT, DROPPED, CUT, SKIPPED = "sent", "dropped", "cut", "skipped"
# Still to be handled: a performance isn't finished while any message is in one of these.
OPEN = (WINGS, WAITING, ON_STAGE, MISSED_CUE)
SENDABLE = (WINGS, MISSED_CUE, ON_STAGE)
# Later stages add checks that may say "not now" (rate limits, quiet hours, the Safety Curtain):
# function(s, message, workspace, contact, now) -> raises queue.Defer to wait.
before_send = []
# And things to do with an answer (stage 5: slow down on 429, the Safety Curtain): function(s, message, workspace, result, now)
after_send = []
# Ways to make the email's links and tracking pixel (stage 6): function(message) -> (link_wrapper, pixel_url)
tracking = []
# What to do when a performance finishes (stage 7: a weekly show goes back on Standby): function(s, show, perf, now)
on_finished = []


def enqueue_send(s, message, run_at, round_number=0):
    return queue.enqueue(s, "send_message", {"message_id": message.id}, run_at=run_at,
                         workspace_id=message.workspace_id, dedupe_key=f"send:{message.id}:{round_number}")


def build(message, show, workspace, contact):
    """The Outgoing email for this message."""
    link_wrapper = pixel_url = None
    for maker in tracking:
        link_wrapper, pixel_url = maker(message)
    subject, html, text = emails.render(show, workspace, contact, unsubscribe_url=unsubscribe_url(contact.id, message.id),
                                        link_wrapper=link_wrapper, pixel_url=pixel_url,
                                        subject=subject_for(message, show))
    if message.is_test:
        subject = f"[Test] {subject}"
    headers = {
        # One-click unsubscribe (RFC 8058): Gmail and others show their own Unsubscribe button.
        "List-Unsubscribe": f"<{one_click_url(contact.id, message.id)}>",
        "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
        "X-Oncue-Message": str(message.id),
    }
    return mailers.Outgoing(workspace_id=workspace.id, message_id=message.id, to=message.email,
                            from_name=workspace.sender_name, from_email=workspace.sender_email, subject=subject,
                            html=html, text=text, send_token=message.send_token, headers=headers)


def subject_for(message, show):
    """Stage 7's A/B test gives some messages the B subject."""
    variant_subject = getattr(show, "subject_b", None)
    return variant_subject if getattr(message, "variant", None) == "b" and variant_subject else show.subject


def claim_message(s, message, now):
    """Move the message to On Stage with a new send_token, if nobody else did. Commits."""
    # Reuse the token while it may have been used (a crash, a timeout); a new one only after a
    # clear "not taken" answer cleared it. That is what makes a retry safe with an idempotent mailer.
    token = message.send_token or secrets.token_hex(16)
    result = s.execute(update(Message).where(Message.id == message.id, Message.status == message.status,
                                             Message.updated_at == message.updated_at)
                       .values(status=ON_STAGE, send_token=token, attempts=Message.attempts + 1, updated_at=now)
                       .execution_options(synchronize_session=False))
    s.commit()
    if result.rowcount != 1:
        return False
    s.refresh(message)
    return True


def set_status(message, status, now, error=None):
    message.status, message.updated_at = status, now
    if error is not None:
        message.last_error = error[:300]


@queue.handler("send_message", on_dead=lambda s, job, now: _dropped(s, job, now))
def send_message(s, job, now):
    message = s.get(Message, job.payload["message_id"])
    if message is None or message.status not in SENDABLE:
        return  # already handled (a duplicate job, or a resume after it went)
    show = s.get(Show, message.show_id)
    workspace = s.get(Workspace, message.workspace_id)
    if not message.is_test:
        if show.status == shows.CUT:
            set_status(message, CUT, now)
            return
        if show.status == shows.HOLD:
            return  # parked: Resume queues it again
    contact = s.get(Contact, message.contact_id) if message.contact_id else None
    if contact is None or (not message.is_test and (
            contact.status != audience.SUBSCRIBED or audience.is_suppressed(s, workspace.id, contact.email))):
        set_status(message, SKIPPED, now, "They unsubscribed or are on the do-not-send list." if contact
                   else "The contact was deleted.")
        finish_if_done(s, message, now)
        return
    for check in before_send:
        check(s, message, workspace, contact, now)  # may raise queue.Defer
    mailer = mailers.for_workspace(workspace)
    if message.status == ON_STAGE and not getattr(mailer, "idempotent", False):
        # A send was in flight when a worker stopped, and this mailer can't tell us if it went.
        set_status(message, UNSURE, now, "The worker stopped while sending. Check before you resend it.")
        finish_if_done(s, message, now)
        return
    if not claim_message(s, message, now):
        return  # another job has it
    message.mailer = mailer.name
    outgoing = build(message, show, workspace, contact)
    outgoing.attempt = message.attempts
    in_doubt = False  # True when we can't tell whether the service took it
    try:
        result = mailer.send(s, outgoing, now)
    except mailers.MailerTimeout as error:
        result, in_doubt = mailers.TryLater(f"No answer in time: {error}"), True
    except OSError as error:
        result = mailers.TryLater(f"Couldn't reach the mail service: {error}")
    for hook in after_send:
        hook(s, message, workspace, result, now)
    if isinstance(result, mailers.Accepted):
        message.provider_id, message.sent_at, message.next_try_at = result.provider_id, now, None
        set_status(message, SENT, now, "")
        finish_if_done(s, message, now)
    elif isinstance(result, mailers.Rejected):
        set_status(message, DROPPED, now, result.reason)
        if result.permanent_bounce:
            audience.suppress(s, workspace.id, message.email, "bounced", now, result.reason)
        finish_if_done(s, message, now)
    elif in_doubt and not getattr(mailer, "idempotent", False):
        # It may have gone. Without idempotency a retry could send it twice, so a person decides.
        set_status(message, UNSURE, now, f"{result.reason} It may or may not have gone. Check before you resend it.")
        finish_if_done(s, message, now)
    else:
        if not in_doubt:
            message.send_token = None  # a clear "not taken": the next try gets a new token
        # Save the Missed Cue now: raising Retry rolls back anything not committed.
        wait = result.retry_after if result.retry_after is not None else queue.backoff(job.attempts)
        message.next_try_at = now + _seconds(wait)
        set_status(message, MISSED_CUE, now, result.reason)
        s.commit()
        raise queue.Retry(result.reason, after=wait)


def _seconds(value):
    return timedelta(seconds=value)


def _dropped(s, job, now):
    """The send job used up its tries: the message is Dropped, for good."""
    message = s.get(Message, job.payload.get("message_id"))
    if message is not None and message.status in OPEN:
        set_status(message, DROPPED, now, f"Gave up after {job.attempts} tries. {message.last_error}")
        message.next_try_at = None
        finish_if_done(s, message, now)


# ---------- Finishing a performance ----------

def open_count(s, performance_id):
    return s.scalar(select(func.count()).select_from(Message)
                    .where(Message.performance_id == performance_id, Message.status.in_(OPEN)))


def finish_if_done(s, message, now):
    if message.performance_id:
        finish_performance_if_done(s, s.get(Performance, message.performance_id), now)


def finish_performance_if_done(s, perf, now):
    """Every email handled? Then the performance is done, and the show's curtain comes down."""
    s.flush()
    if perf is None or not perf.fanned_out or perf.status in ("done", "cut") or open_count(s, perf.id):
        return False
    perf.status, perf.finished_at = "done", now
    show = s.get(Show, perf.show_id)
    for hook in on_finished:
        if hook(s, show, perf, now):
            return True  # the hook took care of the show (a weekly show goes back on Standby)
    shows.claim_status(s, show.id, shows.GO, shows.CURTAIN_DOWN, now)
    return True


# ---------- Hold, Resume and Cut ----------

def on_show_move(s, show, old, new, now):
    if new == shows.CUT:
        s.execute(update(Message).where(Message.show_id == show.id, Message.status.in_((WINGS, WAITING, MISSED_CUE)))
                  .values(status=CUT, updated_at=now).execution_options(synchronize_session=False))
        s.execute(update(Performance).where(Performance.show_id == show.id,
                                            Performance.status.in_(("fanning", "sending")))
                  .values(status="cut", finished_at=now).execution_options(synchronize_session=False))
    elif old == shows.HOLD and new == shows.GO:
        # The parked emails go back in the queue (each job checks again before sending).
        resumed = s.scalars(select(Message).where(Message.show_id == show.id,
                                                  Message.status.in_((WINGS, MISSED_CUE)),
                                                  Message.performance_id.is_not(None))).all()
        for message in resumed:
            queue.enqueue(s, "send_message", {"message_id": message.id}, run_at=max(now, message.due_at),
                          workspace_id=message.workspace_id)


def counts(s, show_id, performance_id=None):
    """{status: how many} for a show (or one performance)."""
    query = select(Message.status, func.count()).where(Message.show_id == show_id, Message.is_test.is_(False))
    if performance_id:
        query = query.where(Message.performance_id == performance_id)
    return dict(s.execute(query.group_by(Message.status)).all())


def paused_check(s, message, workspace, contact, now):
    """Backstage's Pause All Sending: wait (without using a try) until it is switched off."""
    if workspace.sending_paused and not message.is_test:
        raise queue.Defer(now + timedelta(minutes=1), "Sending is paused in Backstage.")


def init_app(app):
    if on_show_move not in shows.on_move:
        shows.on_move.append(on_show_move)
    if paused_check not in before_send:
        before_send.insert(0, paused_check)


# ---------- Progress, for the show's page and the Stage ----------

ACCEPTED = (SENT, "delivered", "opened", "clicked")
TROUBLE = (DROPPED, UNSURE, "bounced", "complained")
WAITING_GROUP = (WINGS, WAITING, ON_STAGE)


def meter(counts):
    """Group message counts for the progress bar: gone, retrying, trouble, waiting, not sent."""
    total = sum(counts.values())
    groups = {
        "gone": sum(counts.get(key, 0) for key in ACCEPTED),
        "retrying": counts.get(MISSED_CUE, 0),
        "trouble": sum(counts.get(key, 0) for key in TROUBLE),
        "waiting": sum(counts.get(key, 0) for key in WAITING_GROUP),
        "not_sent": counts.get(SKIPPED, 0) + counts.get(CUT, 0),
    }
    groups["total"] = total
    groups["handled"] = total - groups["waiting"] - groups["retrying"]
    # Widths for the SVG bar, in percent (attributes, not styles, so the page's CSP allows them).
    x = 0.0
    for key in ("gone", "trouble", "retrying", "not_sent"):
        width = 100 * groups[key] / total if total else 0
        groups[f"{key}_x"], groups[f"{key}_w"] = round(x, 2), round(width, 2)
        x += width
    return groups


def meters_for(s, show_ids):
    """{show id: meter} for several shows at once (the Stage)."""
    if not show_ids:
        return {}
    rows = s.execute(select(Message.show_id, Message.status, func.count())
                     .where(Message.show_id.in_(show_ids), Message.is_test.is_(False))
                     .group_by(Message.show_id, Message.status)).all()
    by_show = {}
    for show_id, status, count in rows:
        by_show.setdefault(show_id, {})[status] = count
    return {show_id: meter(by_show.get(show_id, {})) for show_id in show_ids}
