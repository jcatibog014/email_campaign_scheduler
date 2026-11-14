"""Brevo's webhook: delivery news for the owner's real emails.

In Brevo (Transactional > Settings > Webhook) the owner adds
    https://<site>/hooks/brevo/<BREVO_WEBHOOK_SECRET>
for the events below. The secret in the address is the proof that a post
comes from Brevo (anyone else gets a 404). Brevo posts one event (or a
list) as JSON:

    {"event": "delivered", "email": "ana@example.com", "message-id": "<...>",
     "ts_event": 1763251200, "tag": "oncue-message-41", "reason": "...", "link": "https://..."}

We find the email by its message-id (saved when Brevo took it), or by the
tag we sent, and pass the news to tracking.record(), which counts each event
once and never moves a status backwards. Brevo retries a webhook that fails,
so this answers 200 for anything it understood, even news about an email it
can't find (a deleted demo), and 400 only for a body that isn't JSON.
"""
import hmac
from datetime import UTC, datetime

from flask import Blueprint, abort, current_app, jsonify, request
from sqlalchemy import select

from . import audience, db, sending, tracking
from .dates import utcnow
from .models import Contact, Message

bp = Blueprint("hooks", __name__)
EVENTS = {
    "delivered": tracking.DELIVERED,
    "opened": tracking.OPENED, "unique_opened": tracking.OPENED, "proxy_open": tracking.OPENED,
    "click": tracking.CLICKED,
    "hard_bounce": tracking.BOUNCED, "blocked": tracking.BOUNCED, "invalid_email": tracking.BOUNCED,
    "soft_bounce": tracking.SOFT_BOUNCE,
    "deferred": tracking.DEFERRED,
    "spam": tracking.COMPLAINED,
    "unsubscribed": tracking.UNSUBSCRIBED,
    "error": tracking.ERROR,
}


def when(event, now):
    stamp = event.get("ts_event") or event.get("ts")
    try:
        return datetime.fromtimestamp(int(stamp), UTC).replace(tzinfo=None)
    except (TypeError, ValueError, OverflowError, OSError):
        return now


def find_message(s, event):
    message_id = event.get("message-id") or event.get("message_id")
    if message_id:
        found = s.scalar(select(Message).where(Message.provider_id == message_id))
        if found is not None:
            return found
    tags = event.get("tags") or ([event["tag"]] if event.get("tag") else [])
    for tag in tags:
        if str(tag).startswith("oncue-message-") and str(tag)[14:].isdigit():
            return s.get(Message, int(str(tag)[14:]))
    return None


def handle(s, event, now):
    kind = EVENTS.get(str(event.get("event", "")).lower())
    message = find_message(s, event) if kind else None
    if message is None:
        return "ignored"
    occurred = when(event, now)
    event_id = f"{event.get('message-id', message.id)}:{event.get('event')}:{event.get('ts_event', '')}:{event.get('link', '')}"
    link_id, detail = None, event.get("reason") or ""
    if kind == tracking.CLICKED and event.get("link"):
        link = tracking.link_for(s, message.show_id, message.workspace_id, str(event["link"])[:2000])
        link_id, detail = link.id, link.url
    if not tracking.record(s, message, kind, "brevo", event_id, occurred, detail, link_id, now):
        return "duplicate"
    if kind == tracking.UNSUBSCRIBED and message.contact_id:
        contact = s.get(Contact, message.contact_id)
        if contact is not None:
            audience.unsubscribe(s, contact, occurred)
    sending.finish_if_done(s, message, now)
    return "recorded"


@bp.post("/hooks/brevo/<secret>")
def brevo(secret):
    expected = current_app.config.get("BREVO_WEBHOOK_SECRET")
    if not expected or not hmac.compare_digest(secret, expected):
        abort(404)
    body = request.get_json(silent=True)
    if body is None:
        abort(400, "Send JSON.")
    events = body if isinstance(body, list) else [body]
    s = db.session()
    now = utcnow()
    results = [handle(s, event, now) for event in events if isinstance(event, dict)]
    s.commit()
    return jsonify(results=results)


def init_app(app):
    exempt = set(app.config.get("CSRF_EXEMPT", ()))
    exempt.add("hooks.brevo")  # Brevo posts with the secret in the address, not a form token
    app.config["CSRF_EXEMPT"] = exempt
