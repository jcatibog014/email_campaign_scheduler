"""The Call Board: what's on, pinned to a cork board as cue cards, by day.

Week view (the default): seven columns, Monday to Sunday, in the workspace's
time zone. Month view: the month's weeks, a few titles a day. Each card is a
show at a time: shows on Standby, EVERY coming time of a weekly show (not
just the next one), what is on Go now, and what finished.
"""
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from flask import Blueprint, g, render_template, request
from sqlalchemy import select

from .. import audience, db, shows, smart
from ..auth import login_required
from ..dates import at_local, parse_clock, to_zone, utcnow
from ..models import Performance, Show

bp = Blueprint("callboard", __name__, url_prefix="/call-board")


@dataclass
class Card:
    show: Show
    at: datetime  # naive UTC
    status: str
    label: str
    repeat: bool = False


@bp.before_request
@login_required
def must_log_in():
    pass


def cards_between(s, workspace, first_day, last_day):
    """Every card from first_day to last_day (dates in the workspace's zone), by local day."""
    zone_name = workspace.time_zone
    start = at_local(first_day, datetime.min.time(), zone_name)
    end = at_local(last_day + timedelta(days=1), datetime.min.time(), zone_name)
    labels = {}

    def label(show):
        if show.id not in labels:
            labels[show.id] = audience.audience_label(s, workspace, show.audience_kind, show.audience_ref)
        return labels[show.id]

    found = []
    for show in s.scalars(select(Show).where(Show.workspace_id == workspace.id, Show.status == shows.STANDBY,
                                             Show.send_at < end)):
        if show.send_at >= start:
            found.append(Card(show, show.send_at, show.status, label(show), show.repeats))
        if show.repeats:  # the times after the next one
            moment, clock = show.send_at, parse_clock(show.repeat_time)
            for _ in range(62):
                moment = smart.next_occurrence(smart.days_of(show), clock, zone_name, moment)
                if moment is None or moment >= end:
                    break
                if moment >= start:
                    found.append(Card(show, moment, shows.STANDBY, label(show), True))
    now = utcnow()
    for show in s.scalars(select(Show).where(Show.workspace_id == workspace.id,
                                             Show.status.in_((shows.GO, shows.HOLD)))):
        moment = show.started_at or now
        if start <= moment < end:
            found.append(Card(show, moment, show.status, label(show), show.repeats))
    finished = s.execute(select(Performance, Show).join(Show, Show.id == Performance.show_id)
                         .where(Performance.workspace_id == workspace.id, Performance.status == "done",
                                Performance.starts_at >= start, Performance.starts_at < end)).all()
    for perf, show in finished:
        found.append(Card(show, perf.starts_at, shows.CURTAIN_DOWN, label(show), show.repeats))
    by_day = {}
    for card in sorted(found, key=lambda card: card.at):
        by_day.setdefault(to_zone(card.at, zone_name).date(), []).append(card)
    return by_day


@bp.get("/")
def board():
    s = db.session()
    zone_name = g.workspace.time_zone
    today = to_zone(utcnow(), zone_name).date()
    if request.args.get("view") == "month":
        offset = request.args.get("month", 0, type=int)
        year, month = today.year, today.month + offset
        year, month = year + (month - 1) // 12, (month - 1) % 12 + 1
        first = date(year, month, 1)
        grid_start = smart.week_start(first)
        next_month = date(year + month // 12, month % 12 + 1, 1)
        weeks = []
        day = grid_start
        while day < next_month or day.weekday() != 0:
            if day.weekday() == 0:
                weeks.append([])
            weeks[-1].append(day)
            day += timedelta(days=1)
        by_day = cards_between(s, g.workspace, grid_start, weeks[-1][-1])
        return render_template("callboard_month.html", weeks=weeks, by_day=by_day, first=first, today=today,
                               offset=offset, this_monday=smart.week_start(today))
    offset = request.args.get("week", 0, type=int)
    monday = smart.week_start(today) + timedelta(weeks=offset)
    days = [monday + timedelta(days=index) for index in range(7)]
    return render_template("callboard.html", days=days, by_day=cards_between(s, g.workspace, days[0], days[-1]),
                           today=today, offset=offset)
