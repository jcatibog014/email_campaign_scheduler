"""Delivery tracking: what happened to an email after the mail service took it.

News comes from three places, through ONE function, record():
- Brevo's webhook (hooks.py): delivered, opened, clicked, bounced, spam...;
- the practice mail server, which sends its own news a little later, like a
  real service (practice_events below);
- our own open pixel (/o/<token>.gif) and click links (/c/<token>).

Three rules make the numbers right:

1. ONCE. Every piece of news has a source and an id; UNIQUE(source, event_id)
   means a webhook delivered twice (services do that) is counted once.

2. NEVER BACKWARDS. News arrives late and out of order: an "opened" can
   come before its "delivered". So news only fills in the FIRST time each
   thing happened (delivered_at, opened_at...), and the status is worked out
   from all of them, by rank:
       Sent < Delivered < Opened < Clicked,   and Bounced / Marked as Spam above those.
   An opened email was obviously delivered, so opened_at also fills delivered_at.

3. CONSEQUENCES. A hard bounce or a spam complaint puts the address on the
   do-not-send list: emailing it again would hurt every future show.

Opens are a rough guide: some mail apps (Apple Mail) load every picture
before anyone looks, and others never load pictures at all.
"""
import base64
import random
from datetime import timedelta

from flask import Blueprint, abort, current_app, redirect, request
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import object_session

from . import audience, db, practice_server, queue, sending, tokens
from .dates import utcnow
from .models import Link, Message, MessageEvent, PracticeMail
from .views import public

bp = Blueprint("tracking", __name__)

DELIVERED, OPENED, CLICKED, BOUNCED, COMPLAINED = "delivered", "opened", "clicked", "bounced", "complained"
SOFT_BOUNCE, DEFERRED, UNSUBSCRIBED, ERROR = "soft_bounce", "deferred", "unsubscribed", "error"
STAMPS = {DELIVERED: "delivered_at", OPENED: "opened_at", CLICKED: "clicked_at", BOUNCED: "bounced_at",
          COMPLAINED: "complained_at", UNSUBSCRIBED: "unsubscribed_at"}
# News that means the email really went out (so an Unsure email can be cleared up).
PROOF = (DELIVERED, OPENED, CLICKED, BOUNCED, COMPLAINED, SOFT_BOUNCE, DEFERRED)
EVENT_LABELS = {DELIVERED: "Delivered", OPENED: "Opened", CLICKED: "Clicked", BOUNCED: "Bounced",
                COMPLAINED: "Marked as spam", SOFT_BOUNCE: "Soft bounce (they'll try again)",
                DEFERRED: "Delayed by the receiving server", UNSUBSCRIBED: "Unsubscribed", ERROR: "Error",
                "sent": "Taken by the mail service"}
PIXEL = base64.b64decode("R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7")  # a 1x1 clear GIF


def status_of(message):
    """The status from the stamps (rule 2)."""
    for kind, stamp in ((COMPLAINED, "complained_at"), (BOUNCED, "bounced_at"), (CLICKED, "clicked_at"),
                        (OPENED, "opened_at"), (DELIVERED, "delivered_at")):
        if getattr(message, stamp):
            return kind
    return sending.SENT


def record(s, message, kind, source, event_id, occurred_at, detail="", link_id=None, now=None):
    """Record one piece of news about a message. Returns False if it was already recorded."""
    event = MessageEvent(workspace_id=message.workspace_id, message_id=message.id, kind=kind, source=source,
                         event_id=event_id[:200], detail=(detail or "")[:300], link_id=link_id,
                         occurred_at=occurred_at, received_at=now or utcnow())
    try:
        with s.begin_nested():
            s.add(event)
            s.flush()
    except IntegrityError:
        return False  # rule 1: already counted
    stamp = STAMPS.get(kind)
    if stamp and (getattr(message, stamp) is None or occurred_at < getattr(message, stamp)):
        setattr(message, stamp, occurred_at)
    # An open implies a delivery, a click implies an open (rule 2).
    if kind in (OPENED, CLICKED) and (message.delivered_at is None or occurred_at < message.delivered_at):
        message.delivered_at = occurred_at
    if kind == CLICKED and (message.opened_at is None or occurred_at < message.opened_at):
        message.opened_at = occurred_at
    if message.status == sending.UNSURE and kind in PROOF:
        message.last_error = "News arrived after all: it did go out."
        message.sent_at = message.sent_at or occurred_at
    if message.status in (sending.SENT, *sending.ACCEPTED, BOUNCED, COMPLAINED, sending.UNSURE) and kind in PROOF:
        message.status = status_of(message)
    if kind == BOUNCED:
        audience.suppress(s, message.workspace_id, message.email, "bounced", occurred_at, detail or "It bounced.")
    if kind == COMPLAINED:
        audience.suppress(s, message.workspace_id, message.email, "complained", occurred_at,
                          detail or "They marked an email as spam.")
    message.updated_at = now or utcnow()
    return True


def events_of(s, message_id):
    return s.scalars(select(MessageEvent).where(MessageEvent.message_id == message_id)
                     .order_by(MessageEvent.occurred_at, MessageEvent.id)).all()


# ---------- Our own open pixel and click links ----------

def link_for(s, show_id, workspace_id, url):
    """The Link row for a URL in a show (made the first time)."""
    found = s.scalar(select(Link).where(Link.show_id == show_id, Link.url == url))
    if found is None:
        try:
            with s.begin_nested():
                found = Link(show_id=show_id, workspace_id=workspace_id, url=url)
                s.add(found)
                s.flush()
        except IntegrityError:  # another worker made it first
            found = s.scalar(select(Link).where(Link.show_id == show_id, Link.url == url))
    return found


def make_tracking(message):
    """(link_wrapper, pixel_url) for one message: sending.tracking calls this while building it."""
    if message.is_test:
        return None, None
    s = object_session(message)  # the worker's session that is building this email
    base = current_app.config["BASE_URL"]

    def wrap(url):
        if not url.startswith(("http://", "https://")):
            return url  # mailto: links stay as they are
        link = link_for(s, message.show_id, message.workspace_id, url[:2000])
        return f"{base}/c/{tokens.make('click', message.id, link.id)}"

    return wrap, f"{base}/o/{tokens.make('open', message.id)}.gif"


@bp.get("/o/<token>.gif")
def open_pixel(token):
    parts = tokens.read_ints("open", token, 1)
    if parts:
        s = db.session()
        message = s.get(Message, parts[0])
        if message is not None:
            moment = utcnow()
            record(s, message, OPENED, "pixel", f"open:{message.id}", moment, now=moment)
            s.commit()
    # Always the picture, even for a bad token: a broken image helps nobody.
    return PIXEL, 200, {"Content-Type": "image/gif", "Cache-Control": "no-store, max-age=0"}


@bp.get("/c/<token>")
def click(token):
    """Count the click, then send the person on. Only to a link the show really has:
    the address holds the link's id, never the URL (so it can't be an open redirect)."""
    parts = tokens.read_ints("click", token, 2)
    s = db.session()
    link = s.get(Link, parts[1]) if parts else None
    message = s.get(Message, parts[0]) if parts else None
    if link is None or message is None or link.show_id != message.show_id:
        abort(404)
    moment = utcnow()
    record(s, message, CLICKED, "click", f"click:{message.id}:{link.id}", moment, detail=link.url,
           link_id=link.id, now=moment)
    s.commit()
    return redirect(link.url, code=302)


def on_unsubscribe(s, contact, message_id, moment):
    message = s.get(Message, message_id) if message_id else None
    if message is not None and message.contact_id == contact.id:
        record(s, message, UNSUBSCRIBED, "page", f"unsubscribe:{message.id}", moment, now=moment)


# ---------- The practice server's news ----------

def practice_news(s, mail, slow, now):
    """Plan the news a real service would send later: delivered, then maybe opened and clicked,
    or a bounce, or a complaint. Each is a job that arrives at its time."""
    dice = random.Random(mail.provider_id)
    plan = []
    if mail.fate == "bounce":
        plan.append((BOUNCED, dice.uniform(30, 90), "550 5.1.1 The mailbox doesn't exist."))
    else:
        delivered = dice.uniform(120, 300) if slow else dice.uniform(2, 20)
        if dice.random() < 0.05:
            plan.append((DEFERRED, delivered / 2, "421 The receiving server asked us to wait."))
        plan.append((DELIVERED, delivered, ""))
        if mail.fate == "complain":
            plan += [(OPENED, delivered + 300, ""), (COMPLAINED, delivered + 900, "The person pressed Report Spam.")]
        elif dice.random() < 0.48:
            opened = delivered + dice.uniform(60, 45 * 60)
            plan.append((OPENED, opened, ""))
            if dice.random() < 0.32:
                plan.append((CLICKED, opened + dice.uniform(10, 600), ""))
    from .models import Workspace
    if s.get(Workspace, mail.workspace_id).replaying:
        # A demo replaying its past (seed.py): this news is already history, so it is recorded as soon
        # as the sender has saved the email as Sent (news_after_sent), through the same record(),
        # instead of as jobs for later. That saves two thirds of the replay's jobs.
        s.info.setdefault("planned_news", {})[mail.provider_id] = (mail, plan, now)
        return
    for kind, delay, detail in plan:
        queue.enqueue(s, "practice_event", {"mail_id": mail.id, "kind": kind, "detail": detail},
                      run_at=now + timedelta(seconds=delay), workspace_id=mail.workspace_id, priority=7,
                      dedupe_key=f"pev:{mail.id}:{kind}")


@queue.handler("practice_event")
def practice_event(s, job, now):
    mail = s.get(PracticeMail, job.payload["mail_id"])
    if mail is None:
        return
    message = s.scalar(select(Message).where(Message.provider_id == mail.provider_id))
    if message is None:
        return
    deliver_news(s, mail, message, job.payload["kind"], job.payload.get("detail", ""), job.run_at, now)


def deliver_news(s, mail, message, kind, detail, occurred_at, now):
    """One piece of the practice server's news, recorded like any other."""
    link_id = None
    if kind == CLICKED:
        links = s.scalars(select(Link).where(Link.show_id == message.show_id).order_by(Link.id)).all()
        if not links:
            kind = OPENED  # nothing to click: they just read it
        else:
            link = random.Random(mail.provider_id).choice(links)
            link_id, detail = link.id, link.url
    record(s, message, kind, "practice", f"{mail.provider_id}:{kind}", occurred_at, detail, link_id, now)
    sending.finish_if_done(s, message, now)


def news_after_sent(s, message, now):
    planned = s.info.get("planned_news", {}).pop(message.provider_id, None)
    if planned is not None:
        mail, plan, accepted = planned
        for kind, delay, detail in plan:
            deliver_news(s, mail, message, kind, detail, accepted + timedelta(seconds=delay), now)


def init_app(app):
    if news_after_sent not in sending.on_sent:
        sending.on_sent.append(news_after_sent)
    if make_tracking not in sending.tracking:
        sending.tracking.append(make_tracking)
    if practice_news not in practice_server.after_accept:
        practice_server.after_accept.append(practice_news)
    if on_unsubscribe not in public.on_unsubscribe:
        public.on_unsubscribe.append(on_unsubscribe)
