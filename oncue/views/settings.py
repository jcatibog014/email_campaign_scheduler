"""Settings: your account, the workspace (who emails come from, its time zone),
and the look. Later stages add sending limits and the practice mail server.

Settings holds every control and preference (the theme, Log out); the menus
only take you to pages.
"""
from flask import Blueprint, abort, flash, g, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash

from .. import db
from ..auth import email_problem, login_required, name_problem, not_for_demo, password_problem, set_password
from ..dates import is_zone, zone_names

bp = Blueprint("settings", __name__)
POSTAL_MAX_LENGTH = 300
# Sections that later modules add: action name -> function(s, workspace, form) -> (errors, message)
actions = {}
# Extra things the page shows: function(s, workspace) -> dict
extras = []


def zone_choices():
    """Every time zone, as "Asia/Manila", sorted (the ones with a "/" are the real places)."""
    return sorted(name for name in zone_names() if "/" in name and not name.startswith(("Etc/", "SystemV/")))


def save_workspace(s, workspace, form):
    errors = {}
    name = form.get("name", "").strip()
    sender_name = form.get("sender_name", "").strip()
    sender_email = form.get("sender_email", "").strip().lower()
    postal = form.get("postal_address", "").strip()
    zone_name = form.get("time_zone", "").strip()
    if problem := name_problem(name, "a name for the workspace"):
        errors["name"] = problem
    if problem := name_problem(sender_name, "who the emails are from"):
        errors["sender_name"] = problem
    if problem := email_problem(sender_email):
        errors["sender_email"] = problem
    if len(postal) > POSTAL_MAX_LENGTH:
        errors["postal_address"] = f"Use at most {POSTAL_MAX_LENGTH} characters."
    if not is_zone(zone_name):
        errors["time_zone"] = "Choose a time zone from the list."
    if errors:
        return errors, None
    if workspace.is_demo and sender_email != workspace.sender_email:
        # The demo only sends to the practice mail server, but keep its sender made-up too.
        errors["sender_email"] = "The demo keeps its made-up sender address."
        return errors, None
    workspace.name, workspace.sender_name, workspace.sender_email = name, sender_name, sender_email
    workspace.postal_address, workspace.time_zone = postal, zone_name
    return {}, "The workspace details are saved."


actions["workspace"] = save_workspace


@bp.route("/settings", methods=["GET", "POST"])
@login_required
def page():
    s = db.session()
    errors = {}
    if request.method == "POST":
        action = request.form.get("action")
        if action == "name":
            not_for_demo("The demo can't change its account.")
            name = request.form.get("user_name", "").strip()
            if problem := name_problem(name):
                errors["user_name"] = problem
            else:
                g.user.name = name
                s.commit()
                flash("Your name has been changed.")
                return redirect(url_for("settings.page"))
        elif action == "password":
            not_for_demo("The demo can't change its account.")
            if not check_password_hash(g.user.password_hash, request.form.get("current_password", "")):
                errors["current_password"] = "That isn't your current password."
            new = request.form.get("new_password", "")
            if problem := password_problem(new, request.form.get("confirm_password", "")):
                errors["new_password"] = problem
            if not errors:
                set_password(g.user, new)
                s.commit()
                session["session_version"] = g.user.session_version  # this device stays logged in
                flash("Your password has been changed. Your other devices are logged out.")
                return redirect(url_for("settings.page"))
        elif action in actions:
            errors, message = actions[action](s, g.workspace, request.form)
            if not errors:
                s.commit()
                flash(message)
                return redirect(url_for("settings.page", _anchor=request.form.get("anchor") or None))
            s.rollback()
        else:
            abort(400, "Choose something to change.")
    data = {}
    for extra in extras:
        data.update(extra(s, g.workspace))
    return render_template("settings.html", errors=errors, form=request.form, zones=zone_choices(), **data), \
        400 if errors else 200


def init_app(app):
    pass
