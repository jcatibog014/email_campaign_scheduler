"""Reviews: how the shows did over a period (the last 7, 30, 90 or 365 days).

Everything counts emails SENT in the period (taken by the mail service), and
what then happened to them, from the first-time stamps (tracking.py):
- the delivery funnel: sent, delivered, opened, clicked (plus bounced, spam, unsubscribed);
- first opens per day (per week for long periods), by the workspace's day;
- why emails bounced, and the most clicked links;
- every show with its own rates.
"""
from collections import Counter
from datetime import timedelta

from sqlalchemy import func, select

from . import results
from .dates import to_zone
from .models import Link, Message, MessageEvent, Show

PERIODS = (7, 30, 90, 365)


def sent_in(workspace_id, start, end):
    return (Message.workspace_id == workspace_id, Message.is_test.is_(False), Message.sent_at >= start,
            Message.sent_at < end)


def funnel_numbers(s, workspace_id, start, end):
    row = s.execute(select(func.count(), func.count(Message.delivered_at), func.count(Message.opened_at),
                           func.count(Message.clicked_at), func.count(Message.bounced_at),
                           func.count(Message.complained_at), func.count(Message.unsubscribed_at))
                    .where(*sent_in(workspace_id, start, end))).one()
    keys = ("sent", "delivered", "opened", "clicked", "bounced", "complained", "unsubscribed")
    return dict(zip(keys, (int(value or 0) for value in row)))


def opens_over_time(s, workspace, start, end):
    """[(label, opens, hover title)] per day (or per week when the period is long)."""
    stamps = s.scalars(select(Message.opened_at).where(*sent_in(workspace.id, start, end),
                                                       Message.opened_at.is_not(None))).all()
    first_day = to_zone(start, workspace.time_zone).date()
    last_day = to_zone(end - timedelta(seconds=1), workspace.time_zone).date()
    weekly = (last_day - first_day).days > 45
    def bucket(day):
        return day - timedelta(days=day.weekday()) if weekly else day
    counts = Counter(bucket(to_zone(stamp, workspace.time_zone).date()) for stamp in stamps)
    points, day = [], bucket(first_day)
    step = timedelta(days=7 if weekly else 1)
    while day <= last_day:
        label = f"{day:%b} {day.day}"
        title = f"{'Week of ' if weekly else ''}{day:%a}, {day:%b} {day.day}: {counts.get(day, 0):,} first opens"
        points.append((label, counts.get(day, 0), title))
        day += step
    return points, weekly


def bounce_reasons(s, workspace_id, start, end, limit=6):
    rows = s.execute(select(MessageEvent.detail, func.count()).where(
        MessageEvent.workspace_id == workspace_id, MessageEvent.kind.in_(("bounced", "soft_bounce")),
        MessageEvent.occurred_at >= start, MessageEvent.occurred_at < end)
        .group_by(MessageEvent.detail).order_by(func.count().desc()).limit(limit))
    return [(detail or "No reason given", count) for detail, count in rows]


def top_links(s, workspace_id, start, end, limit=5):
    rows = s.execute(select(Link.url, Show.name, func.count(func.distinct(MessageEvent.message_id)))
                     .join(MessageEvent, MessageEvent.link_id == Link.id).join(Show, Show.id == Link.show_id)
                     .where(MessageEvent.workspace_id == workspace_id, MessageEvent.kind == "clicked",
                            MessageEvent.occurred_at >= start, MessageEvent.occurred_at < end)
                     .group_by(Link.url, Show.name)
                     .order_by(func.count(func.distinct(MessageEvent.message_id)).desc()).limit(limit))
    return rows.all()


def show_rows(s, workspace_id, start, end):
    """Each show with emails sent in the period, newest first, with its numbers."""
    found = s.execute(select(Show, func.max(Message.sent_at)).join(Message, Message.show_id == Show.id)
                      .where(*sent_in(workspace_id, start, end)).group_by(Show.id)
                      .order_by(func.max(Message.sent_at).desc())).all()
    numbers = results.for_shows(s, [show.id for show, _last in found])
    return [(show, last, numbers[show.id]) for show, last in found]
