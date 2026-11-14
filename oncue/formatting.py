"""Filters for the templates: dates, times and numbers the way people read them.

Times are shown in the workspace's time zone (Settings), so the owner in
Manila sees "9:00 AM" for a show that goes out at 9:00 AM Manila time.
"""
from datetime import date, datetime

from flask import g, has_request_context

from .dates import DEFAULT_ZONE, to_zone, utcnow


def workspace_zone():
    if has_request_context() and g.get("workspace") is not None:
        return g.workspace.time_zone
    return DEFAULT_ZONE


def local(moment, zone_name=None):
    return to_zone(moment, zone_name or workspace_zone())


def nice_date(moment, zone_name=None):
    """Oct 30, 2026 (written by hand: %-d doesn't work on Windows).
    Takes a date or a moment from the database (naive UTC)."""
    if isinstance(moment, datetime):
        moment = local(moment, zone_name)
    return f"{moment:%b} {moment.day}, {moment.year}"


def short_date(day, zone_name=None):
    """Mon, Oct 30 (this year) or Oct 30, 2027."""
    if isinstance(day, datetime):
        day = local(day, zone_name)
    this_year = local(utcnow(), zone_name).year
    return f"{day:%a}, {day:%b} {day.day}" if day.year == this_year else nice_date(day)


def clock(moment, zone_name=None):
    """2:30 PM"""
    stamp = local(moment, zone_name)
    return f"{stamp.hour % 12 or 12}:{stamp:%M %p}"


def nice_datetime(moment, zone_name=None):
    """Mon, Oct 30, 2:30 PM"""
    if moment is None:
        return ""
    return f"{short_date(moment, zone_name)}, {clock(moment, zone_name)}"


def ago(moment):
    """How long ago: just now, 5 min ago, 3 h ago, Oct 30."""
    if moment is None:
        return ""
    seconds = (utcnow() - moment).total_seconds()
    if seconds < 0:
        return from_now(moment)
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{int(seconds // 60)} min ago"
    if seconds < 86400:
        return f"{int(seconds // 3600)} h ago"
    if seconds < 7 * 86400:
        days = int(seconds // 86400)
        return "yesterday" if days == 1 else f"{days} days ago"
    return short_date(moment)


def from_now(moment):
    """How long until: in 40 s, in 5 min, in 3 h, Mon, Oct 30."""
    if moment is None:
        return ""
    seconds = (moment - utcnow()).total_seconds()
    if seconds <= 0:
        return ago(moment) if seconds < -60 else "now"
    if seconds < 60:
        return f"in {int(seconds)} s"
    if seconds < 3600:
        return f"in {int(seconds // 60)} min"
    if seconds < 86400:
        hours, rest = divmod(int(seconds // 60), 60)
        return f"in {hours} h {rest} min" if rest else f"in {hours} h"
    return nice_datetime(moment)


def duration(seconds):
    """40 s, 5 min, 3 h 20 min, 2 d 4 h."""
    seconds = int(round(seconds or 0))
    if seconds < 60:
        return f"{seconds} s"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes} min"
    hours, rest = divmod(minutes, 60)
    if hours < 24:
        return f"{hours} h {rest} min" if rest else f"{hours} h"
    days, hours = divmod(hours, 24)
    return f"{days} d {hours} h" if hours else f"{days} d"


def plural(count, word, many=None):
    """1 email, 3 emails (with thousands commas)."""
    return f"{count:,} {word if count == 1 else many or word + 's'}"


def number(value):
    return f"{int(value or 0):,}"


def percent(part, whole, digits=0):
    """'42%' (or an en dash when there is nothing to divide by)."""
    if not whole:
        return "–"
    value = 100 * (part or 0) / whole
    return f"{value:.{digits}f}%"


def iso_local(moment, zone_name=None):
    """2026-12-31T09:00: a moment for a datetime-local box."""
    return local(moment, zone_name).strftime("%Y-%m-%dT%H:%M") if moment else ""


def day_iso(day):
    return day.isoformat() if isinstance(day, date) else ""


def init_app(app):
    for function in (nice_date, short_date, clock, nice_datetime, ago, from_now, duration, number, iso_local,
                     day_iso):
        app.jinja_env.filters[function.__name__] = function
    app.jinja_env.globals.update(plural=plural, percent=percent)
