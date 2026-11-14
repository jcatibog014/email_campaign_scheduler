"""Scripts: saved emails with merge fields, and the preview frame.

The preview shows the email exactly as a mail app would, inside an <iframe>.
The email uses inline styles (mail apps ignore style sheets), and our pages
forbid inline styles (CSP). So the frame is its own little page with its own,
even stricter rules: no scripts at all, styles only inline, pictures only from
https, and it can only be shown inside this site.
"""
from flask import Blueprint, Response, abort, flash, g, redirect, render_template, request, url_for
from sqlalchemy import select

from .. import audience, db, emails
from ..auth import login_required
from ..dates import utcnow
from ..models import Contact, Script

bp = Blueprint("scripts", __name__)
NAME_MAX, SUBJECT_MAX, BODY_MAX = 80, 200, 50_000
FRAME_POLICY = ("default-src 'none'; style-src 'unsafe-inline'; img-src https: data:; frame-ancestors 'self'; "
                "form-action 'none'")


@bp.before_request
@login_required
def must_log_in():
    pass


def preview_response(page_html):
    # Links in the preview open in a new tab (the frame itself is sandboxed in the page).
    page_html = page_html.replace("<head>", '<head><base target="_blank">', 1)
    return Response(page_html, headers={"Content-Security-Policy": FRAME_POLICY, "X-Frame-Options": "SAMEORIGIN",
                                        "Cache-Control": "no-store"})


def preview_contact(s):
    """The contact chosen in "Preview as", or None (then Ana Santos, a made-up sample)."""
    contact_id = request.values.get("contact", type=int)
    return audience.get(s, g.workspace.id, contact_id) if contact_id else None


def preview_choices(s, limit=40):
    contacts = s.scalars(select(Contact).where(Contact.workspace_id == g.workspace.id)
                         .order_by(Contact.first_name, Contact.email).limit(limit)).all()
    return [(contact.id, f"{contact.name or contact.email}") for contact in contacts]


def check(form):
    values = {key: " ".join(form.get(key, "").split()) for key in ("name", "subject", "preheader")}
    values["body"] = form.get("body", "").replace("\r\n", "\n").strip()
    errors = {}
    if not values["name"] or len(values["name"]) > NAME_MAX:
        errors["name"] = f"Give it a name of up to {NAME_MAX} characters."
    if len(values["subject"]) > SUBJECT_MAX:
        errors["subject"] = f"Use at most {SUBJECT_MAX} characters."
    if len(values["preheader"]) > SUBJECT_MAX:
        errors["preheader"] = f"Use at most {SUBJECT_MAX} characters."
    if len(values["body"]) > BODY_MAX:
        errors["body"] = f"Use at most {BODY_MAX:,} characters."
    return values, errors


def merge_help(s, texts=()):
    known = audience.field_names(s, g.workspace.id)
    return {"custom_fields": known, "unknown": emails.unknown_fields(texts, known)}


@bp.get("/scripts")
def index():
    s = db.session()
    found = s.scalars(select(Script).where(Script.workspace_id == g.workspace.id)
                      .order_by(Script.updated_at.desc())).all()
    return render_template("scripts.html", scripts=found)


def editor(found):
    s = db.session()
    errors = {}
    values = request.form if request.method == "POST" else None
    if request.method == "POST":
        values, errors = check(request.form)
        clash = s.scalar(select(Script).where(Script.workspace_id == g.workspace.id, Script.name == values["name"],
                                              Script.id != (found.id if found else 0)))
        if clash:
            errors["name"] = "You already have a script with that name."
        if not errors:
            moment = utcnow()
            if found is None:
                found = Script(workspace_id=g.workspace.id, created_at=moment)
                s.add(found)
            for key, value in values.items():
                setattr(found, key, value)
            found.updated_at = moment
            s.commit()
            flash(f"The script {found.name} is saved.")
            return redirect(url_for("scripts.edit", script_id=found.id))
    current = values or (found and {"name": found.name, "subject": found.subject, "preheader": found.preheader,
                                    "body": found.body}) or {"name": "", "subject": "", "preheader": "",
                                                             "body": "Hi {{first_name|there}},\n\n"}
    return render_template("script_form.html", found=found, values=current, errors=errors,
                           preview_choices=preview_choices(s),
                           **merge_help(s, (current.get("subject", ""), current.get("body", "")))), \
        400 if errors else 200


@bp.route("/scripts/new", methods=["GET", "POST"])
def new():
    return editor(None)


@bp.route("/scripts/<int:script_id>", methods=["GET", "POST"])
def edit(script_id):
    s = db.session()
    found = s.get(Script, script_id)
    if found is None or found.workspace_id != g.workspace.id:
        abort(404)
    return editor(found)


@bp.post("/scripts/<int:script_id>/delete")
def delete(script_id):
    s = db.session()
    found = s.get(Script, script_id)
    if found is None or found.workspace_id != g.workspace.id:
        abort(404)
    name = found.name
    s.delete(found)
    s.commit()
    flash(f"The script {name} is deleted. Shows made from it keep their own copy.")
    return redirect(url_for("scripts.index"))


@bp.get("/scripts/<int:script_id>/frame")
def frame(script_id):
    s = db.session()
    found = s.get(Script, script_id)
    if found is None or found.workspace_id != g.workspace.id:
        abort(404)
    contact = preview_contact(s)
    _subject, page, _text = emails.render(found, g.workspace, contact, sample=contact is None)
    return preview_response(page)


class Draft:
    """What is in the editor right now, not saved yet (for the live preview)."""
    def __init__(self, form):
        self.subject = form.get("subject", "")[:SUBJECT_MAX]
        self.preheader = form.get("preheader", "")[:SUBJECT_MAX]
        self.body = form.get("body", "")[:BODY_MAX]


@bp.post("/preview-frame")
def draft_frame():
    """The editor's Preview button (and, with JavaScript, every pause in typing)
    posts here with target="preview": the frame shows the unsaved email."""
    s = db.session()
    contact = preview_contact(s)
    _subject, page, _text = emails.render(Draft(request.form), g.workspace, contact, sample=contact is None)
    return preview_response(page)
