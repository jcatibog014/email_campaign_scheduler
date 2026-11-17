"""The scheduler and the fan-out: from "a show on Standby" to "one send job per person".

run_due() is called by every worker round (and by the timer). It looks for
shows on Standby whose time has come. For each one it:
1. moves the show Standby -> Go with claim_status(): a guarded UPDATE, so if
   two workers look at the same moment, only one starts it;
2. makes a Performance (one run of the show) and queues a start_performance
   job for it, with a dedupe_key so it exists once.

start_performance (the fan-out) works through the audience in batches of 500
contacts, in id order. For each batch it makes the Messages and queues their
send jobs, then queues ITSELF again for the next batch ("after_id"). Small jobs
like this keep each one short (a lease is 60 seconds), and if a worker dies
half-way, only the current batch is done again. That is safe: messages are
UNIQUE per performance and contact, so a batch done twice adds nobody twice.
"""
from sqlalchemy import func, select

from . import audience, queue, sending, shows
from .models import Contact, Message, Performance, Show, Workspace

BATCH = 500
DUE_PER_ROUND = 20
# Ways to choose when one person's email goes (stage 7: their own time zone, quiet hours):
# function(s, show, performance, contact, workspace) -> naive UTC moment, or None for "no opinion".
due_rules = []
# Ways to set up a performance before its fan-out: function(s, show, perf, now)
on_start = []
# Changes to a batch of new messages before they are queued (stage 7: the A/B test gives each a
# subject, and holds most back as "waiting"): function(s, show, perf, messages)
batch_rules = []


def run_due(s, now, workspace_id=None):
    """Start every show whose time has come. Returns how many started."""
    from .queue import in_scope
    due = s.scalars(select(Show).where(Show.status == shows.STANDBY, Show.send_at <= now,
                                       in_scope(Show.workspace_id, workspace_id))
                    .order_by(Show.send_at).limit(DUE_PER_ROUND)).all()
    started = 0
    for show in due:
        if not shows.claim_status(s, show.id, shows.STANDBY, shows.GO, now):
            s.rollback()
            continue  # another worker started it
        number = (s.scalar(select(func.max(Performance.number)).where(Performance.show_id == show.id)) or 0) + 1
        perf = Performance(workspace_id=show.workspace_id, show_id=show.id, number=number,
                           starts_at=show.send_at, created_at=now)
        s.add(perf)
        s.flush()
        s.refresh(show)
        for hook in on_start:
            hook(s, show, perf, now)
        queue.enqueue(s, "start_performance", {"performance_id": perf.id, "after_id": 0}, run_at=now,
                      workspace_id=show.workspace_id, priority=3, dedupe_key=f"fan:{perf.id}:0")
        s.commit()
        started += 1
    return started


def due_at(s, show, perf, contact, workspace):
    """When this person's email should go: the performance's start, unless a rule says otherwise."""
    for rule in due_rules:
        moment = rule(s, show, perf, contact, workspace)
        if moment is not None:
            return moment
    return perf.starts_at


@queue.handler("start_performance")
def start_performance(s, job, now):
    perf = s.get(Performance, job.payload["performance_id"])
    if perf is None or perf.status in ("done", "cut"):
        return
    show = s.get(Show, perf.show_id)
    workspace = s.get(Workspace, show.workspace_id)
    if show.status == shows.CUT:
        return
    ids = audience.audience_ids(s, workspace, show.audience_kind, show.audience_ref,
                                after_id=job.payload.get("after_id", 0), limit=BATCH)
    have = set(s.scalars(select(Message.contact_id).where(Message.performance_id == perf.id,
                                                          Message.contact_id.in_(ids)))) if ids else set()
    made = []
    for contact in s.scalars(select(Contact).where(Contact.id.in_([i for i in ids if i not in have]))
                             .order_by(Contact.id)) if ids else []:
        message = Message(workspace_id=workspace.id, show_id=show.id, performance_id=perf.id, contact_id=contact.id,
                          email=contact.email, status=sending.WINGS, created_at=now, updated_at=now,
                          due_at=due_at(s, show, perf, contact, workspace))
        s.add(message)
        made.append(message)
    s.flush()
    for rule in batch_rules:
        rule(s, show, perf, made)
    for message in made:
        if message.status == sending.WINGS:  # an A/B test may hold some back ("waiting")
            sending.enqueue_send(s, message, max(message.due_at, now))
    perf.audience_size += len(made)
    if len(ids) == BATCH:
        queue.enqueue(s, "start_performance", {"performance_id": perf.id, "after_id": ids[-1]}, run_at=now,
                      workspace_id=workspace.id, priority=3, dedupe_key=f"fan:{perf.id}:{ids[-1]}")
    else:
        perf.fanned_out = True
        perf.status = "sending"
        sending.finish_performance_if_done(s, perf, now)  # an empty audience finishes at once
