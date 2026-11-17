"""Counting what happened: the numbers on a show's page, the Stage and Reviews.

Every count comes from the first-time stamps on messages (tracking.py), so an
email that was opened twice, or whose news arrived twice, counts once.
Rates use the right base:
- delivered out of SENT (taken by the mail service);
- opened, clicked and unsubscribed out of DELIVERED (you can't open what never arrived);
- bounced out of SENT.
"""
from sqlalchemy import case, func, select

from . import sending
from .models import Link, Message, MessageEvent

WENT_OUT = (*sending.ACCEPTED, "bounced", "complained")


def _counts(show_ids=None, workspace_id=None):
    def stamped(column):
        return func.count(column)
    query = select(
        Message.show_id,
        func.count().label("total"),
        func.sum(case((Message.status.in_(WENT_OUT), 1), else_=0)).label("sent"),
        stamped(Message.delivered_at).label("delivered"),
        stamped(Message.opened_at).label("opened"),
        stamped(Message.clicked_at).label("clicked"),
        stamped(Message.bounced_at).label("bounced"),
        stamped(Message.complained_at).label("complained"),
        stamped(Message.unsubscribed_at).label("unsubscribed"),
        func.sum(case((Message.status == sending.DROPPED, 1), else_=0)).label("dropped"),
    ).where(Message.is_test.is_(False))
    if show_ids is not None:
        query = query.where(Message.show_id.in_(show_ids))
    if workspace_id is not None:
        query = query.where(Message.workspace_id == workspace_id)
    return query.group_by(Message.show_id)


EMPTY = dict.fromkeys(("total", "sent", "delivered", "opened", "clicked", "bounced", "complained", "unsubscribed",
                       "dropped"), 0)


def for_shows(s, show_ids):
    """{show id: {total, sent, delivered, opened, clicked, bounced, complained, unsubscribed, dropped}}"""
    if not show_ids:
        return {}
    found = {row.show_id: {key: int(getattr(row, key) or 0) for key in EMPTY} for row in s.execute(_counts(show_ids))}
    return {show_id: found.get(show_id, dict(EMPTY)) for show_id in show_ids}


def for_show(s, show_id):
    return for_shows(s, [show_id])[show_id]


def link_clicks(s, show_id):
    """[(url, people who clicked it)] most clicked first."""
    rows = s.execute(select(Link.url, func.count(func.distinct(MessageEvent.message_id)))
                     .join(MessageEvent, MessageEvent.link_id == Link.id)
                     .where(Link.show_id == show_id, MessageEvent.kind == "clicked")
                     .group_by(Link.url).order_by(func.count(func.distinct(MessageEvent.message_id)).desc()))
    return rows.all()
