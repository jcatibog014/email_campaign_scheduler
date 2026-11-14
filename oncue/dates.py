"""Times. The database always holds naive UTC. People see times in a time zone:
the workspace's own (Settings, Asia/Manila at first), or a contact's own when a
show goes out "at 9:00 AM their time".

Nothing here reads the clock except utcnow(). Everything that decides
something about time (the scheduler, retries, rate limits) is handed the
moment as an argument, so tests can say "it is now 9:00 on Monday" and the
demo can play a week of shows in a second.
"""
from datetime import UTC, datetime, time, timedelta
from functools import lru_cache
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError, available_timezones

DEFAULT_ZONE = "Asia/Manila"


def utcnow():
    return datetime.now(UTC).replace(tzinfo=None)


@lru_cache(maxsize=256)
def zone(name):
    """A ZoneInfo, or Manila's when the name is empty or unknown."""
    try:
        return ZoneInfo(name or DEFAULT_ZONE)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo(DEFAULT_ZONE)


def is_zone(name):
    return bool(name) and name in zone_names()


@lru_cache(maxsize=1)
def zone_names():
    return frozenset(available_timezones())


def to_zone(moment, name):
    """Naive UTC from the database -> an aware datetime in that zone."""
    return moment.replace(tzinfo=UTC).astimezone(zone(name))


def from_zone(local, name):
    """A naive wall-clock time in that zone -> naive UTC (for the database).

    On the night clocks go back, 1:30 AM happens twice; fold=0 takes the first.
    On the night they go forward, 2:30 AM doesn't exist; ZoneInfo moves it on
    by the gap, which is what people expect ("as soon as it is 2:30 or later").
    """
    aware = local.replace(tzinfo=zone(name))
    # Round trip through UTC: a time in the spring-forward gap comes back an hour later.
    return aware.astimezone(UTC).replace(tzinfo=None)


def at_local(day, clock, name):
    """The UTC moment of a wall-clock time (a time) on a day (a date) in a zone."""
    return from_zone(datetime.combine(day, clock), name)


def local_today(name, moment=None):
    return to_zone(moment or utcnow(), name).date()


def parse_clock(text):
    """'09:00' or '9:00' -> time(9, 0); anything else -> None."""
    try:
        hours, minutes = (int(part) for part in (text or "").strip().split(":"))
    except ValueError:
        return None
    if 0 <= hours < 24 and 0 <= minutes < 60:
        return time(hours, minutes)
    return None


def minutes_of(clock):
    return clock.hour * 60 + clock.minute


def clock_of(minutes):
    return time(minutes // 60 % 24, minutes % 60)


def seconds_between(earlier, later):
    return (later - earlier).total_seconds()


def plus(moment, seconds=0, minutes=0, hours=0, days=0):
    return moment + timedelta(seconds=seconds, minutes=minutes, hours=hours, days=days)
