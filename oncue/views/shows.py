"""Shows: the list, the builder, a show's page, and the buttons that move it
between cue states (the rules themselves are in shows.py)."""
from datetime import datetime, timedelta

from flask import Blueprint, abort, flash, g, jsonify, redirect, render_template, request, url_for
from sqlalchemy import case, func, select

from .. import audience, buckets, db, emails, limits, mailers, results, sending, shows, smart, tracking
from ..auth import login_required
from ..dates import from_zone, parse_clock, utcnow
from ..models import Contact, Message, Performance, Script, Show
from . import stage
from .scripts import merge_help, preview_choices, preview_contact, preview_response

bp = Blueprint("shows", __name__, url_prefix="/shows")
TABS = [
    ("all", "All", None),
    ("rehearsal", "Rehearsal", (shows.REHEARSAL,)),
    ("standby", "Standby", (shows.STANDBY,)),
    ("live", "Go and Hold", (shows.GO, shows.HOLD)),
    ("done", "Curtain Down", (shows.CURTAIN_DOWN,)),
    ("cut", "Cut", (shows.CUT,)),
]
# Extra things on a show's page (later stages add progress, emails, performances): function(s, show) -> dict
page_extras = []
# Things that happen after "Start now" or a resume (the worker wakes up): function(app)
nudges = []


@bp.before_request
@login_required
def must_log_in():
    pass


def show_or_404(s, show_id):
    found = shows.get(s, g.workspace.id, show_id)
    if found is None:
        abort(404)
    return found


def check_version(found):
    """The page the button was on must show the show as it is now."""
    sent = request.form.get("version", type=int)
    if sent is not None and sent != found.version:
        raise shows.ShowError("This show changed since you opened the page. Here it is as it is now.")


def act(show_id, action):
    """Run one button's change. Errors become a red toast; either way, back to the show."""
    s = db.session()
    found = show_or_404(s, show_id)
    try:
        check_version(found)
        message = action(s, found, utcnow())
        shows.save_or_conflict(s)
        flash(message)
        if found.status in (shows.STANDBY, shows.GO):
            for nudge in nudges:
                nudge()
    except shows.ShowError as error:
        s.rollback()
        flash(str(error), "error")
    return redirect(url_for("shows.page", show_id=show_id))


def parse_local(text):
    """'2026-11-20T09:00' in the workspace's zone -> naive UTC, or None."""
    try:
        return from_zone(datetime.strptime(text or "", "%Y-%m-%dT%H:%M"), g.workspace.time_zone)
    except ValueError:
        return None


# ---------- The list ----------

@bp.get("/")
def index():
    s = db.session()
    tab = request.args.get("tab", "all")
    statuses = dict((key, value) for key, _label, value in TABS).get(tab)
    query = select(Show).where(Show.workspace_id == g.workspace.id)
    if statuses:
        query = query.where(Show.status.in_(statuses))
    # Live shows first, then what's next, then the newest.
    order = case((Show.status.in_((shows.GO, shows.HOLD)), 0), (Show.status == shows.STANDBY, 1), else_=2)
    found = s.scalars(query.order_by(order, Show.send_at.is_(None), Show.send_at, Show.updated_at.desc())).all()
    counts = shows.counts_by_status(s, g.workspace.id)
    tab_counts = {key: (sum(counts.values()) if value is None else sum(counts.get(v, 0) for v in value))
                  for key, _label, value in TABS}
    labels = {row.id: audience.audience_label(s, g.workspace, row.audience_kind, row.audience_ref) for row in found}
    return render_template("shows.html", shows=found, tab=tab, tabs=TABS, tab_counts=tab_counts, labels=labels)


# ---------- The builder ----------

def builder(found):
    s = db.session()
    errors = {}
    choices = audience.audience_choices(s, g.workspace)
    script = None
    if found is None and request.args.get("script", type=int):
        script = s.get(Script, request.args.get("script", type=int))
        if script is None or script.workspace_id != g.workspace.id:
            script = None
    if request.method == "POST":
        form = request.form
        values = {"name": " ".join(form.get("name", "").split()), "subject": " ".join(form.get("subject", "").split()),
                  "preheader": " ".join(form.get("preheader", "").split()),
                  "body": form.get("body", "").replace("\r\n", "\n").strip(), "audience": form.get("audience", "")}
        if not values["name"] or len(values["name"]) > shows.NAME_MAX:
            errors["name"] = f"Give the show a name of up to {shows.NAME_MAX} characters (only you see it)."
        if len(values["subject"]) > shows.SUBJECT_MAX:
            errors["subject"] = f"Use at most {shows.SUBJECT_MAX} characters."
        kind, ref = audience.parse_audience(values["audience"])
        if kind is None or audience.audience_condition(s, g.workspace, kind, ref) is None:
            errors["audience"] = "Choose who gets it."
        # The A/B subject test.
        values["ab_on"] = form.get("ab_on") == "yes"
        values["subject_b"] = " ".join(form.get("subject_b", "").split())
        values["ab_percent"] = form.get("ab_percent", type=int) or 20
        values["ab_wait_hours"] = form.get("ab_wait_hours", type=int) or 4
        values["ab_metric"] = "clicks" if form.get("ab_metric") == "clicks" else "opens"
        if values["ab_on"]:
            if not values["subject_b"] or len(values["subject_b"]) > shows.SUBJECT_MAX:
                errors["subject_b"] = "Write subject B (up to 200 characters), or turn the test off."
            elif values["subject_b"] == values["subject"]:
                errors["subject_b"] = "Subject B must be different from subject A."
            elif values["ab_percent"] not in smart.AB_PERCENTS or values["ab_wait_hours"] not in smart.AB_WAITS:
                errors["subject_b"] = "Choose the test's size and waiting time from the lists."
            elif found is not None and (found.repeats or found.send_mode == "their_time"):
                errors["subject_b"] = ("An A/B test needs everyone sent at the same moment, once. "
                                       "Schedule it once, at one time, first.")
        if found is not None:
            try:
                check_version(found)
            except shows.ShowError as error:
                errors["version"] = str(error)
            if found.status not in shows.EDITABLE:
                abort(409, "The curtain is up: a show that has started can't be changed. Duplicate it instead.")
        if not errors:
            moment = utcnow()
            if found is None:
                found = Show(workspace_id=g.workspace.id, created_at=moment,
                             script_id=form.get("script_id", type=int) or None)
                s.add(found)
            found.name, found.subject, found.preheader, found.body = (values["name"], values["subject"],
                                                                      values["preheader"], values["body"])
            found.audience_kind, found.audience_ref = kind, None if ref is None else str(ref)
            found.subject_b = values["subject_b"] if values["ab_on"] else None
            found.ab_percent, found.ab_wait_hours, found.ab_metric = (values["ab_percent"], values["ab_wait_hours"],
                                                                      values["ab_metric"])
            found.updated_at = moment
            if found.status == shows.STANDBY and (problems := shows.problems(s, found, g.workspace)):
                s.rollback()
                errors["version"] = "It's on Standby, so it must stay ready to send: " + " ".join(problems)
            else:
                try:
                    shows.save_or_conflict(s)
                except shows.ShowError as error:
                    errors["version"] = str(error)
                else:
                    flash(f"{found.name} is saved.")
                    return redirect(url_for("shows.page", show_id=found.id))
    else:
        if found is not None:
            values = {"name": found.name, "subject": found.subject, "preheader": found.preheader, "body": found.body,
                      "audience": found.audience_kind + (f":{found.audience_ref}" if found.audience_ref else ""),
                      "ab_on": found.is_ab, "subject_b": found.subject_b or "", "ab_percent": found.ab_percent,
                      "ab_wait_hours": found.ab_wait_hours, "ab_metric": found.ab_metric}
        elif script is not None:
            values = {"name": script.name, "subject": script.subject, "preheader": script.preheader,
                      "body": script.body, "audience": "all"}
        else:
            values = {"name": "", "subject": "", "preheader": "", "body": "Hi {{first_name|there}},\n\n",
                      "audience": "all"}
    scripts = s.scalars(select(Script).where(Script.workspace_id == g.workspace.id).order_by(Script.name)).all()
    for key, default in (("ab_on", False), ("subject_b", ""), ("ab_percent", 20), ("ab_wait_hours", 4),
                         ("ab_metric", "opens")):
        values.setdefault(key, default)
    return render_template("show_form.html", found=found, values=values, errors=errors, choices=choices,
                           ab_percents=smart.AB_PERCENTS, ab_waits=smart.AB_WAITS,
                           scripts=scripts, script=script, preview_choices=preview_choices(s),
                           **merge_help(s, (values.get("subject", ""), values.get("body", "")))), \
        400 if errors else 200


@bp.route("/new", methods=["GET", "POST"])
def new():
    return builder(None)


@bp.route("/<int:show_id>/edit", methods=["GET", "POST"])
def edit(show_id):
    found = show_or_404(db.session(), show_id)
    if found.status not in shows.EDITABLE:
        flash("The curtain is up: a show that has started can't be changed. Duplicate it to make a new one.",
              "error")
        return redirect(url_for("shows.page", show_id=found.id))
    return builder(found)


# ---------- A show's page ----------

@bp.get("/<int:show_id>")
def page(show_id):
    s = db.session()
    found = show_or_404(s, show_id)
    extras = {}
    for extra in page_extras:
        extras.update(extra(s, found))
    return render_template("show.html", show=found,
                           audience_label=audience.audience_label(s, g.workspace, found.audience_kind,
                                                                  found.audience_ref),
                           audience_count=audience.audience_count(s, g.workspace, found.audience_kind,
                                                                  found.audience_ref),
                           problems=shows.problems(s, found, g.workspace) if found.status in shows.EDITABLE else [],
                           preview_choices=preview_choices(s), now=utcnow(), soon=timedelta(minutes=10),
                           **extras)


@bp.get("/<int:show_id>/frame")
def frame(show_id):
    s = db.session()
    found = show_or_404(s, show_id)
    contact = preview_contact(s)
    _subject, page_html, _text = emails.render(found, g.workspace, contact, sample=contact is None)
    return preview_response(page_html)


@bp.post("/<int:show_id>/schedule")
def schedule(show_id):
    """Three ways: once at a time; once at a time in each person's own zone; every week."""
    def action(s, found, moment):
        mode = request.form.get("mode", "once")
        if mode == "their_time":
            try:
                day = datetime.strptime(request.form.get("local_date", ""), "%Y-%m-%d").date()
            except ValueError:
                raise shows.ShowError("Choose a date.") from None
            clock = parse_clock(request.form.get("local_time"))
            if clock is None:
                raise shows.ShowError("Choose a time.")
            smart.schedule_their_time(s, found, g.workspace, day, clock, moment)
            return f"{found.name} is on Standby: {request.form.get('local_time')} in each person's own time zone."
        if mode == "weekly":
            clock = parse_clock(request.form.get("repeat_time"))
            if clock is None:
                raise shows.ShowError("Choose a time.")
            smart.schedule_weekly(s, found, g.workspace, smart.parse_days(request.form.getlist("repeat_days")),
                                  clock, moment)
            return f"{found.name} is on Standby: {smart.describe_repeat(found).lower()}."
        send_at = parse_local(request.form.get("send_at"))
        if send_at is None:
            raise shows.ShowError("Choose a date and time.")
        found.send_mode, found.local_date, found.local_time = "at_once", None, None
        smart.stop_repeating(found)
        shows.schedule(s, found, g.workspace, send_at, moment)
        return f"{found.name} is on Standby."
    return act(show_id, action)


@bp.post("/<int:show_id>/stop-repeating")
def stop_repeating(show_id):
    def action(s, found, moment):
        if not found.repeats:
            raise shows.ShowError("This show doesn't repeat.")
        smart.stop_repeating(found)
        found.updated_at = moment
        return f"{found.name} won't repeat. Its next time stays as a one-off."
    return act(show_id, action)


@bp.post("/<int:show_id>/start")
def start(show_id):
    def action(s, found, moment):
        shows.start_now(s, found, g.workspace, moment)
        return f"Places, please: {found.name} goes out now."
    return act(show_id, action)


@bp.post("/<int:show_id>/unschedule")
def unschedule(show_id):
    def action(s, found, moment):
        shows.move(s, found, shows.REHEARSAL, moment)
        return f"{found.name} is back in Rehearsal. Nothing will be sent."
    return act(show_id, action)


@bp.post("/<int:show_id>/hold")
def hold(show_id):
    def action(s, found, moment):
        shows.move(s, found, shows.HOLD, moment)
        return f"{found.name} is on Hold. Emails already sent stay sent; the rest wait."
    return act(show_id, action)


@bp.post("/<int:show_id>/resume")
def resume(show_id):
    def action(s, found, moment):
        shows.move(s, found, shows.GO, moment)
        return f"{found.name} is back on Go."
    return act(show_id, action)


@bp.post("/<int:show_id>/cut")
def cut(show_id):
    def action(s, found, moment):
        shows.move(s, found, shows.CUT, moment)
        return f"{found.name} is Cut. Nothing more will be sent."
    return act(show_id, action)


@bp.post("/<int:show_id>/duplicate")
def duplicate(show_id):
    s = db.session()
    found = show_or_404(s, show_id)
    copy = shows.duplicate(s, found, utcnow())
    s.commit()
    flash(f"A copy of {found.name} is in Rehearsal.")
    return redirect(url_for("shows.edit", show_id=copy.id))


@bp.post("/<int:show_id>/delete")
def delete(show_id):
    s = db.session()
    found = show_or_404(s, show_id)
    if found.status != shows.REHEARSAL:
        flash("Only a show in Rehearsal can be deleted. Cut it instead, so its history stays.", "error")
        return redirect(url_for("shows.page", show_id=found.id))
    name = found.name
    s.delete(found)
    s.commit()
    flash(f"{name} is deleted.")
    return redirect(url_for("shows.index"))


# ---------- Progress ----------

def performances_of(s, show):
    return s.scalars(select(Performance).where(Performance.show_id == show.id)
                     .order_by(Performance.number.desc())).all()


def progress(s, show):
    """The show page's progress panel: each performance (newest first) with its meter,
    an estimate of when it finishes while it is sending, and the tests sent."""
    found = performances_of(s, show)
    rows = [(perf, sending.meter(sending.counts(s, show.id, perf.id))) for perf in found[:5]]
    estimate = None
    if rows and show.status == shows.GO:
        waiting = rows[0][1]["waiting"] + rows[0][1]["retrying"]
        mailer = mailers.for_workspace(g.workspace).name
        estimate = buckets.finish_estimate(s, g.workspace, mailer, waiting, utcnow()) if waiting else None
    tests = s.scalars(select(Message).where(Message.show_id == show.id, Message.is_test.is_(True))
                      .order_by(Message.created_at.desc()).limit(5)).all()
    ab = None
    if show.is_ab:
        latest = found[0] if found else None
        ab = {"perf": latest, "numbers": smart.ab_numbers(s, latest.id) if latest else None}
    return {"progress_template": "_show_progress.html", "performances": rows, "performance_count": len(found),
            "estimate": estimate, "tests": tests, "ab": ab, "weekdays": smart.WEEKDAYS,
            "repeat_label": smart.describe_repeat(show) if show.repeats else None,
            "repeat_days": smart.days_of(show),
            "show_sections": (["_show_ab.html"] if show.is_ab else []) + (["_show_results.html"] if found else [])
            + ["_show_test.html"],
            "results": results.for_show(s, show.id) if found else None,
            "link_clicks": results.link_clicks(s, show.id) if found else [],
            "test_address": g.user.email if not g.user.is_demo else "luna@teatro-luna.example"}


TESTS_PER_HOUR = 10


@bp.post("/<int:show_id>/test")
def send_test(show_id):
    """Send this show to one address, through the same queue, limits and mailer as the real thing."""
    s = db.session()
    found = show_or_404(s, show_id)
    email = audience.clean_email(request.form.get("email"))
    if problem := audience.email_problem(email):
        flash(problem, "error")
        return redirect(url_for("shows.page", show_id=found.id))
    contact = preview_contact(s) or s.scalar(select(Contact).where(Contact.workspace_id == g.workspace.id)
                                             .order_by(Contact.id).limit(1))
    if contact is None:
        flash("Add someone to your audience first: a test fills the merge fields with a real contact's details.",
              "error")
        return redirect(url_for("shows.page", show_id=found.id))
    moment = utcnow()
    if not limits.hit(s, f"test:{g.workspace.id}", TESTS_PER_HOUR, 3600, moment):
        s.commit()
        flash(f"That's {TESTS_PER_HOUR} tests this hour. Please wait a little.", "error")
        return redirect(url_for("shows.page", show_id=found.id))
    message = Message(workspace_id=g.workspace.id, show_id=found.id, contact_id=contact.id, email=email,
                      is_test=True, due_at=moment, created_at=moment, updated_at=moment)
    s.add(message)
    s.flush()
    sending.enqueue_send(s, message, moment).priority = 1  # tests go before the crowd
    s.commit()
    for nudge in nudges:
        nudge()
    where = "the practice mail server (see it in Backstage)" if mailers.for_workspace(g.workspace).name == mailers.PRACTICE else email
    flash(f"A test is on its way to {where}, with {contact.name or contact.email}'s details.")
    return redirect(url_for("shows.page", show_id=found.id))


@bp.get("/<int:show_id>/progress.json")
def progress_json(show_id):
    """What the show page asks every few seconds while the show is on Go."""
    s = db.session()
    found = show_or_404(s, show_id)
    latest = performances_of(s, found)[:1]
    meter = sending.meter(sending.counts(s, found.id, latest[0].id)) if latest else sending.meter({})
    return jsonify(status=found.status, meter=meter)


# ---------- Every email of a show ----------

EMAIL_GROUPS = [
    ("all", "All", None),
    ("wings", "In the Wings", (sending.WINGS, sending.WAITING, sending.ON_STAGE)),
    ("retrying", "Missed Cue", (sending.MISSED_CUE,)),
    ("out", "Went Out", sending.ACCEPTED),
    ("trouble", "Trouble", (sending.DROPPED, sending.UNSURE, "bounced", "complained")),
    ("not_sent", "Not Sent", (sending.SKIPPED, sending.CUT)),
]
EMAILS_PAGE = 50


@bp.get("/<int:show_id>/emails")
def emails_page(show_id):
    s = db.session()
    found = show_or_404(s, show_id)
    group = request.args.get("group", "all")
    statuses = dict((key, value) for key, _label, value in EMAIL_GROUPS).get(group)
    q = request.args.get("q", "").strip().lower()[:100]
    page_number = max(request.args.get("page", 1, type=int), 1)
    base = select(Message).where(Message.show_id == found.id, Message.is_test.is_(False))
    counts = sending.counts(s, found.id)
    group_counts = {key: sum(counts.values()) if value is None else sum(counts.get(v, 0) for v in value)
                    for key, _label, value in EMAIL_GROUPS}
    if statuses:
        base = base.where(Message.status.in_(statuses))
    if q:
        base = base.where(func.lower(Message.email).like(f"%{q.replace('%', '').replace('_', '')}%"))
    total = s.scalar(select(func.count()).select_from(base.subquery()))
    rows = s.scalars(base.order_by(Message.updated_at.desc(), Message.id.desc())
                     .limit(EMAILS_PAGE).offset((page_number - 1) * EMAILS_PAGE)).all()
    return render_template("show_emails.html", show=found, rows=rows, group=group, groups=EMAIL_GROUPS,
                           group_counts=group_counts, q=q, total=total, page=page_number, page_size=EMAILS_PAGE)


@bp.get("/<int:show_id>/emails/<int:message_id>")
def email_page(show_id, message_id):
    s = db.session()
    found = show_or_404(s, show_id)
    message = s.get(Message, message_id)
    if message is None or message.show_id != found.id:
        abort(404)
    contact = s.get(Contact, message.contact_id) if message.contact_id else None
    return render_template("show_email.html", show=found, message=message, contact=contact,
                           events=tracking.events_of(s, message.id), labels=tracking.EVENT_LABELS)


# ---------- On the Stage (home page) ----------

def stage_panel(s, workspace):
    def of(statuses, order, limit):
        return s.scalars(select(Show).where(Show.workspace_id == workspace.id, Show.status.in_(statuses))
                         .order_by(*order).limit(limit)).all()
    live = of((shows.GO, shows.HOLD), (Show.started_at.desc(),), 6)
    upcoming = of((shows.STANDBY,), (Show.send_at,), 5)
    finished = of((shows.CURTAIN_DOWN,), (Show.finished_at.desc(),), 5)
    total = s.scalar(select(func.count()).select_from(Show).where(Show.workspace_id == workspace.id))
    labels = {row.id: audience.audience_label(s, workspace, row.audience_kind, row.audience_ref)
              for row in (*live, *upcoming, *finished)}
    return {"live": live, "upcoming": upcoming, "finished": finished, "show_total": total, "labels": labels,
            "meters": sending.meters_for(s, [row.id for row in live]),
            "results": results.for_shows(s, [row.id for row in finished])}


def contact_history(s, contact):
    """The emails a contact got (their page in Audience)."""
    rows = s.execute(select(Message, Show).join(Show, Show.id == Message.show_id)
                     .where(Message.contact_id == contact.id, Message.is_test.is_(False))
                     .order_by(Message.created_at.desc()).limit(20)).all()
    return {"history_template": "_contact_history.html", "history": rows}


def init_app(app):
    if ("shows", stage_panel) not in stage.panels:  # module lists outlive one app (tests make many)
        stage.panels.append(("shows", stage_panel))
    if progress not in page_extras:
        page_extras.append(progress)
    from . import audience as audience_views
    if contact_history not in audience_views.contact_extras:
        audience_views.contact_extras.append(contact_history)
