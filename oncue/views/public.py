"""Pages for the people who GET the emails (no login): the unsubscribe page.

Every email has a link to /u/<token>. The token is signed (tokens.py), so
nobody can unsubscribe someone else by changing the number in the address.

There are two ways in:
- the page, where people can leave some topic lists or stop everything (and
  subscribe again if they change their mind);
- one-click unsubscribe (RFC 8058): Gmail and others show their own
  "Unsubscribe" button and POST "List-Unsubscribe=One-Click" to the address in
  the email's List-Unsubscribe header. No page, no form token: the signed
  address is the proof, so that route is exempt from CSRF.
"""
from flask import Blueprint, abort, current_app, flash, redirect, render_template, request, url_for
from sqlalchemy import select

from .. import audience, db, tokens
from ..dates import utcnow
from ..models import Contact, ContactList, ListMember, Workspace

bp = Blueprint("public", __name__)
PURPOSE = "unsubscribe"
# Called after someone unsubscribes: function(s, contact, message_id or None, moment). tracking.py
# adds one that records an "unsubscribed" event on the email they came from.
on_unsubscribe = []


def unsubscribe_token(contact_id, message_id=0):
    return tokens.make(PURPOSE, contact_id, message_id or 0)


def unsubscribe_url(contact_id, message_id=0):
    return f"{current_app.config['BASE_URL']}/u/{unsubscribe_token(contact_id, message_id)}"


def one_click_url(contact_id, message_id=0):
    return f"{unsubscribe_url(contact_id, message_id)}/one-click"


def contact_from(s, token):
    parts = tokens.read_ints(PURPOSE, token, 2)
    if parts is None:
        abort(404)
    contact = s.get(Contact, parts[0])
    if contact is None:
        abort(404)  # deleted since
    return contact, parts[1] or None


def masked(email):
    """an•••@gmail.com: enough for people to know it's theirs, not enough to read off a screen."""
    name, _, domain = email.partition("@")
    return f"{name[:2]}{'•' * 3}@{domain}"


def stop_everything(s, contact, message_id):
    moment = utcnow()
    audience.unsubscribe(s, contact, moment)
    for hook in on_unsubscribe:
        hook(s, contact, message_id, moment)


@bp.route("/u/<token>", methods=["GET", "POST"])
def unsubscribe(token):
    s = db.session()
    contact, message_id = contact_from(s, token)
    workspace = s.get(Workspace, contact.workspace_id)
    topics = s.scalars(select(ContactList).join(ListMember, ListMember.list_id == ContactList.id)
                       .where(ListMember.contact_id == contact.id, ContactList.is_topic.is_(True))
                       .order_by(ContactList.name)).all()
    if request.method == "POST":
        action = request.form.get("action")
        if action == "all":
            stop_everything(s, contact, message_id)
            s.commit()
            flash(f"You're unsubscribed. {workspace.sender_name} won't email you again.")
        elif action == "topics" and contact.status == audience.SUBSCRIBED:
            keep = set(request.form.getlist("keep", type=int))
            leave = [topic.id for topic in topics if topic.id not in keep]
            audience.unsubscribe(s, contact, utcnow(), from_list_ids=leave)
            s.commit()
            flash("Your choices are saved." if leave else "Nothing changed: you still get every topic.")
        elif action == "resubscribe":
            audience.resubscribe(s, contact, utcnow())
            s.commit()
            flash(f"Welcome back. You'll get emails from {workspace.sender_name} again.")
        else:
            abort(400, "Choose what to do.")
        return redirect(url_for("public.unsubscribe", token=token))
    return render_template("unsubscribe.html", contact=contact, workspace=workspace, topics=topics,
                           email=masked(contact.email), token=token)


@bp.post("/u/<token>/one-click")
def one_click(token):
    s = db.session()
    contact, message_id = contact_from(s, token)
    stop_everything(s, contact, message_id)
    s.commit()
    return "You're unsubscribed.", 200, {"Content-Type": "text/plain; charset=utf-8"}


def init_app(app):
    exempt = set(app.config.get("CSRF_EXEMPT", ()))
    exempt.add("public.one_click")  # mail services post here with no form token: the signed address is the proof
    app.config["CSRF_EXEMPT"] = exempt
