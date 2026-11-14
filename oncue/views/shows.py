"""Shows: the list, the builder, a show's page, and the buttons that move it
between cue states (the rules themselves are in shows.py)."""
from datetime import datetime

from flask import Blueprint, abort, flash, g, redirect, render_template, request, url_for
from sqlalchemy import case, func, select

from .. import audience, db, emails, shows
from ..auth import login_required
from ..dates import from_zone, utcnow
from ..models import Script, Show
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
                      "audience": found.audience_kind + (f":{found.audience_ref}" if found.audience_ref else "")}
        elif script is not None:
            values = {"name": script.name, "subject": script.subject, "preheader": script.preheader,
                      "body": script.body, "audience": "all"}
        else:
            values = {"name": "", "subject": "", "preheader": "", "body": "Hi {{first_name|there}},\n\n",
                      "audience": "all"}
    scripts = s.scalars(select(Script).where(Script.workspace_id == g.workspace.id).order_by(Script.name)).all()
    return render_template("show_form.html", found=found, values=values, errors=errors, choices=choices,
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
                           preview_choices=preview_choices(s), now=utcnow(), **extras)


@bp.get("/<int:show_id>/frame")
def frame(show_id):
    s = db.session()
    found = show_or_404(s, show_id)
    contact = preview_contact(s)
    _subject, page_html, _text = emails.render(found, g.workspace, contact, sample=contact is None)
    return preview_response(page_html)


@bp.post("/<int:show_id>/schedule")
def schedule(show_id):
    def action(s, found, moment):
        send_at = parse_local(request.form.get("send_at"))
        if send_at is None:
            raise shows.ShowError("Choose a date and time.")
        shows.schedule(s, found, g.workspace, send_at, moment)
        return f"{found.name} is on Standby."
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
    return {"live": live, "upcoming": upcoming, "finished": finished, "show_total": total, "labels": labels}


def init_app(app):
    stage.panels.append(("shows", stage_panel))
