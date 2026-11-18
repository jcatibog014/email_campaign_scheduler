"""Backstage: the job console. Where the crew is, what is in the queue, what
is waiting to retry, what failed for good, and the switches for when
something goes wrong (Pause All Sending, Raise the Safety Curtain)."""
from datetime import timedelta

from flask import Blueprint, abort, flash, g, redirect, render_template, request, url_for
from sqlalchemy import func, or_, select

from .. import buckets, db, mailers, queue, sending, worker
from ..auth import login_required
from ..dates import utcnow
from ..models import Job, Message, PracticeMail, Show, Worker
from .scripts import preview_response

bp = Blueprint("backstage", __name__, url_prefix="/backstage")
PAGE = 50
KIND_LABELS = {"send_message": "Send an email", "start_performance": "Fan-out", "practice_event": "Practice news",
               "ab_decide": "Pick the A/B winner"}


@bp.before_request
@login_required
def must_log_in():
    pass


def trouble(workspace):
    """Things that need a person: dead letters and Unsure emails (the menu's badge)."""
    if workspace is None:
        return 0
    s = db.session()
    dead = s.scalar(select(func.count()).select_from(Job).where(Job.workspace_id == workspace.id,
                                                                Job.status == queue.DEAD))
    unsure = s.scalar(select(func.count()).select_from(Message).where(Message.workspace_id == workspace.id,
                                                                      Message.status == sending.UNSURE))
    return dead + unsure


def describe(s, job):
    """A job in words: what it is about, and a link to it."""
    payload = job.payload or {}
    if job.kind == "send_message":
        message = s.get(Message, payload.get("message_id"))
        if message is not None:
            return (f"To {message.email}", url_for("shows.email_page", show_id=message.show_id, message_id=message.id))
    if job.kind in ("start_performance", "ab_decide"):
        from ..models import Performance
        perf = s.get(Performance, payload.get("performance_id"))
        show = s.get(Show, perf.show_id) if perf else None
        if show is not None:
            extra = f", from contact {payload['after_id']}" if payload.get("after_id") else ""
            return (f"{show.name}, performance {perf.number}{extra}", url_for("shows.page", show_id=show.id))
    if job.kind == "practice_event":
        mail = s.get(PracticeMail, payload.get("mail_id"))
        if mail is not None:
            return (f"{payload.get('kind', '').replace('_', ' ').capitalize()} for {mail.to}",
                    url_for("backstage.inbox_mail", mail_id=mail.id))
    return ("", None)


def crew_label(row):
    """A worker in words: what kind it is (its id is shown small, for the curious)."""
    if row.id.startswith("replay-"):
        return "Demo replay"
    return {"in the web app": "Web app worker", "own process": "Worker in its own window",
            "rehearsal": "Practice run"}.get(row.kind, row.kind.capitalize())


def crew(s, now):
    rows = []
    # Workers seen in the last hour. Older ones (a restart, a deploy) only clutter the list:
    # their jobs come back by themselves, and the timer forgets them after a day.
    recent = select(Worker).where(Worker.last_seen > now - timedelta(hours=1))
    for row in s.scalars(recent.order_by(Worker.last_seen.desc()).limit(12)):
        if row.stopped_at:
            state = "off"
        elif now - row.last_seen <= worker.QUIET_AFTER:
            state = "on"
        else:
            state = "quiet"
        rows.append((row, state, crew_label(row)))
    return rows


def tabs(s):
    workspace_id = g.workspace.id
    def jobs(*statuses):
        return s.scalar(select(func.count()).select_from(Job).where(Job.workspace_id == workspace_id,
                                                                    Job.status.in_(statuses)))
    def messages(status):
        return s.scalar(select(func.count()).select_from(Message).where(Message.workspace_id == workspace_id,
                                                                        Message.status == status))
    return {"queue": jobs(queue.QUEUED, queue.RUNNING), "retries": messages(sending.MISSED_CUE),
            "dead": jobs(queue.DEAD), "unsure": messages(sending.UNSURE),
            "inbox": s.scalar(select(func.count()).select_from(PracticeMail)
                              .where(PracticeMail.workspace_id == workspace_id))}


def render(template, **context):
    s = db.session()
    return render_template(template, tab_counts=tabs(s), kind_labels=KIND_LABELS, **context)


@bp.get("/")
def index():
    s = db.session()
    now = utcnow()
    workspace = g.workspace
    mailer = mailers.for_workspace(workspace).name
    jobs = dict(s.execute(select(Job.status, func.count()).where(Job.workspace_id == workspace.id)
                          .group_by(Job.status)).all())
    due_now = s.scalar(select(func.count()).select_from(Job).where(Job.workspace_id == workspace.id,
                                                                   Job.status == queue.QUEUED, Job.run_at <= now))
    tokens, slow = buckets.level(s, f"rate:{workspace.id}:{mailer}", max(1.0, workspace.send_rate),
                                 workspace.send_rate, now)
    rows = crew(s, now)
    return render("backstage.html", crew=rows, on_duty=any(state == "on" for _row, state, _label in rows), jobs=jobs, due_now=due_now, now=now,
                  curtain=buckets.breaker(s, workspace.id, mailer), mailer=mailer,
                  mailer_label=mailers.LABELS[mailer], tokens=tokens, capacity=max(1.0, workspace.send_rate),
                  slow=slow, sent_today=buckets.sent_today(s, workspace.id, mailer, now),
                  trip_after=buckets.TRIP_AFTER)


def job_list(statuses, template, order):
    s = db.session()
    page = max(request.args.get("page", 1, type=int), 1)
    query = select(Job).where(Job.workspace_id == g.workspace.id, Job.status.in_(statuses))
    total = s.scalar(select(func.count()).select_from(query.subquery()))
    rows = s.scalars(query.order_by(*order).limit(PAGE).offset((page - 1) * PAGE)).all()
    return render(template, rows=[(job, *describe(s, job)) for job in rows], total=total, page=page,
                  page_size=PAGE, now=utcnow())


@bp.get("/queue")
def queue_page():
    return job_list((queue.QUEUED, queue.RUNNING), "backstage_queue.html", (Job.status.desc(), Job.run_at, Job.id))


@bp.get("/dead-letters")
def dead_letters():
    return job_list((queue.DEAD,), "backstage_dead.html", (Job.finished_at.desc(),))


def message_list(status, template):
    s = db.session()
    rows = s.execute(select(Message, Show).join(Show, Show.id == Message.show_id)
                     .where(Message.workspace_id == g.workspace.id, Message.status == status)
                     .order_by(Message.next_try_at, Message.updated_at.desc()).limit(200)).all()
    return render(template, rows=rows, now=utcnow())


@bp.get("/retries")
def retries():
    return message_list(sending.MISSED_CUE, "backstage_retries.html")


@bp.get("/unsure")
def unsure():
    return message_list(sending.UNSURE, "backstage_unsure.html")


@bp.get("/inbox")
def inbox():
    s = db.session()
    page = max(request.args.get("page", 1, type=int), 1)
    q = request.args.get("q", "").strip()[:100]
    query = select(PracticeMail).where(PracticeMail.workspace_id == g.workspace.id)
    if q:
        like = f"%{q.lower().replace('%', '').replace('_', '')}%"
        query = query.where(or_(func.lower(PracticeMail.to).like(like), func.lower(PracticeMail.subject).like(like)))
    total = s.scalar(select(func.count()).select_from(query.subquery()))
    rows = s.scalars(query.order_by(PracticeMail.accepted_at.desc(), PracticeMail.id.desc())
                     .limit(PAGE).offset((page - 1) * PAGE)).all()
    return render("backstage_inbox.html", rows=rows, total=total, page=page, page_size=PAGE, q=q)


def mail_or_404(s, mail_id):
    mail = s.get(PracticeMail, mail_id)
    if mail is None or mail.workspace_id != g.workspace.id:
        abort(404)
    return mail


@bp.get("/inbox/<int:mail_id>")
def inbox_mail(mail_id):
    s = db.session()
    mail = mail_or_404(s, mail_id)
    message = s.scalar(select(Message).where(Message.provider_id == mail.provider_id))
    return render("backstage_mail.html", mail=mail, message=message)


@bp.get("/inbox/<int:mail_id>/frame")
def inbox_frame(mail_id):
    return preview_response(mail_or_404(db.session(), mail_id).html)


# ---------- Buttons ----------

def job_or_404(s, job_id):
    job = s.get(Job, job_id)
    if job is None or job.workspace_id != g.workspace.id:
        abort(404)
    return job


@bp.post("/jobs/<int:job_id>/retry")
def retry_job(job_id):
    s = db.session()
    job = job_or_404(s, job_id)
    if job.status != queue.DEAD:
        abort(409, "Only a dead letter can be retried.")
    moment = utcnow()
    if job.kind == "send_message":
        message = s.get(Message, job.payload.get("message_id"))
        if message is not None and message.status == sending.DROPPED:
            message.status, message.last_error, message.updated_at = sending.WINGS, "Retried by hand.", moment
    queue.retry_now(s, job, moment)
    s.commit()
    worker.nudge()
    flash("It's back in the queue, with a fresh set of tries.")
    return redirect(url_for("backstage.dead_letters"))


@bp.post("/jobs/<int:job_id>/discard")
def discard_job(job_id):
    s = db.session()
    job = job_or_404(s, job_id)
    if job.status != queue.DEAD:
        abort(409, "Only a dead letter can be discarded.")
    queue.discard(s, job, utcnow())
    s.commit()
    flash("Discarded. It won't be tried again.")
    return redirect(url_for("backstage.dead_letters"))


def message_or_404(s, message_id):
    message = s.get(Message, message_id)
    if message is None or message.workspace_id != g.workspace.id:
        abort(404)
    return message


@bp.post("/messages/<int:message_id>/resend")
def resend(message_id):
    """An Unsure email: the person checked, it didn't arrive. Send it again (a new token)."""
    s = db.session()
    message = message_or_404(s, message_id)
    if message.status != sending.UNSURE:
        abort(409, "Only an Unsure email can be resent.")
    moment = utcnow()
    message.status, message.send_token, message.last_error, message.updated_at = (
        sending.WINGS, None, "Resent by hand after checking.", moment)
    s.flush()
    queue.enqueue(s, "send_message", {"message_id": message.id}, run_at=moment, workspace_id=message.workspace_id,
                  priority=2)
    s.commit()
    worker.nudge()
    flash(f"The email to {message.email} is back in the wings.")
    return redirect(url_for("backstage.unsure"))


@bp.post("/messages/<int:message_id>/went")
def mark_went(message_id):
    """An Unsure email: the person checked, it did arrive."""
    s = db.session()
    message = message_or_404(s, message_id)
    if message.status != sending.UNSURE:
        abort(409, "Only an Unsure email can be marked.")
    moment = utcnow()
    message.status, message.sent_at = sending.SENT, message.sent_at or moment
    message.last_error, message.updated_at = "Marked as sent by hand after checking.", moment
    s.commit()
    flash(f"The email to {message.email} is marked as sent.")
    return redirect(url_for("backstage.unsure"))


@bp.post("/pause")
def pause():
    s = db.session()
    g.workspace.sending_paused = request.form.get("paused") == "yes"
    s.commit()
    if not g.workspace.sending_paused:
        worker.nudge()
    flash("Sending is paused. Nothing goes out until you resume." if g.workspace.sending_paused
          else "Sending is back on.")
    return redirect(url_for("backstage.index"))


@bp.post("/crew/start")
def start_crew():
    """Nobody on duty: start the worker inside this web app again (if its thread had stopped).
    A worker in its own window can only be started from that window."""
    from flask import current_app
    if worker.in_app_running():
        flash("A worker is already on duty in this web app.")
    else:
        worker.start_in_app(current_app._get_current_object())
        flash("A worker is on duty again. Jobs left by a stopped worker come back when their lease runs out.")
    return redirect(url_for("backstage.index"))


@bp.post("/curtain")
def raise_curtain():
    s = db.session()
    buckets.raise_curtain(s, g.workspace.id, mailers.for_workspace(g.workspace).name)
    s.commit()
    worker.nudge()
    flash("Auto-pause is off: sending starts again now.")
    return redirect(request.form.get("back") or url_for("backstage.index"))


def init_app(app):
    app.jinja_env.globals["backstage_trouble"] = trouble


def stage_banner(s, workspace):
    """The Stage warns when the Safety Curtain is down or sending is paused."""
    mailer = mailers.for_workspace(workspace).name
    curtain = buckets.breaker(s, workspace.id, mailer)
    down = curtain is not None and curtain.state != "closed"
    banners = []
    if down or workspace.sending_paused or workspace.replaying:
        banners.append("_stage_banner.html")
    return {"stage_banners": banners, "curtain_down": down, "curtain": curtain, "now": utcnow(),
            "retry_soon": timedelta(minutes=1)}
