"""Smart sending: the right moment for each person.

THEIR OWN TIME ZONE. "9:00 AM their time" means 9:00 in Manila for Ana and
9:00 in London for Ben, seven or eight hours apart. The show starts at the
EARLIEST of those moments (9:00 where the day begins first, UTC+14), the
fan-out gives each email its own due time (a due rule), and each send job
simply waits in the queue until its run_at. People for whom 9:00 has already
passed when the show starts get it straight away.

QUIET HOURS. "Not between 9 PM and 8 AM" in each person's own zone. It may
wrap past midnight (21:00 to 08:00), which is the usual case. An email due
in the quiet window moves to the window's end; a retry that lands in it
waits too (a Defer: it doesn't use up a try).

A/B SUBJECT TEST. A share of the audience (say 20%) is split between
subject A and subject B; everyone else waits ("In the Wings, waiting for the
winner"). Who is in the test is decided by a hash of the show and contact,
so a fan-out batch done twice splits the same way. After the waiting time,
the ab_decide job compares opens (or clicks) per delivered email and sends
the winner to everyone else. A tie goes to A.

WEEKLY SHOWS. "Every Monday and Thursday at 9:00." When a performance
finishes, the show goes back on Standby at its next time, instead of
Curtain Down. If a performance runs past the next time, that time is
skipped, never doubled.
"""
import hashlib
from datetime import timedelta

from sqlalchemy import func, select, update

from . import queue, scheduler, sending, shows
from .dates import at_local, clock_of, minutes_of, parse_clock, to_zone
from .models import Contact, Message, Performance, Show, Workspace

EARLIEST_ZONE = "Etc/GMT-14"  # UTC+14 (the sign is backwards in these names): where each day starts first
LATEST_ZONE = "Etc/GMT+12"  # UTC-12: where each day ends last
WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
AB_PERCENTS = (10, 20, 30, 50)
AB_WAITS = (1, 2, 4, 8, 24)


# ---------- Quiet hours ----------

def in_quiet(minute, start, end):
    """Is this minute of the day (0-1439) inside quiet hours? They may wrap past midnight."""
    if start is None or end is None or start == end:
        return False
    return start <= minute < end if start < end else minute >= start or minute < end


def after_quiet(moment, zone_name, start, end):
    """The moment itself, or the end of the quiet hours it falls in (in that zone)."""
    local = to_zone(moment, zone_name)
    if not in_quiet(local.hour * 60 + local.minute, start, end):
        return moment
    end_day = local.date()
    if start > end and local.hour * 60 + local.minute >= start:
        end_day += timedelta(days=1)  # 22:30 in a 21:00-08:00 window: it ends tomorrow morning
    return at_local(end_day, clock_of(end), zone_name)


def person_zone(contact, workspace):
    return contact.time_zone or workspace.time_zone


def due_rule(s, show, perf, contact, workspace):
    """When this person's email goes (scheduler.due_rules)."""
    due = perf.starts_at
    if show.send_mode == "their_time" and show.local_date and show.local_time:
        theirs = at_local(show.local_date, parse_clock(show.local_time), person_zone(contact, workspace))
        due = max(theirs, perf.starts_at)
    return after_quiet(due, person_zone(contact, workspace), workspace.quiet_start, workspace.quiet_end)


def quiet_check(s, message, workspace, contact, now):
    """Just before sending (sending.before_send): never inside their quiet hours."""
    if message.is_test or workspace.quiet_start is None:
        return
    later = after_quiet(now, person_zone(contact, workspace), workspace.quiet_start, workspace.quiet_end)
    if later > now:
        raise queue.Defer(later, "Quiet hours where they live.")


# ---------- Their own time zone ----------

def their_time_window(local_date, local_clock):
    """(first, last): the UTC moments when it is local_clock on local_date somewhere on Earth."""
    return at_local(local_date, local_clock, EARLIEST_ZONE), at_local(local_date, local_clock, LATEST_ZONE)


def schedule_their_time(s, show, workspace, local_date, local_clock, now):
    first, last = their_time_window(local_date, local_clock)
    if last <= now:
        raise shows.ShowError("That time has passed everywhere on Earth. Choose a later date.")
    if show.is_ab:
        raise shows.ShowError("An A/B test sends everyone at the same moment. Turn the test off, or send at once.")
    show.send_mode, show.local_date, show.local_time = "their_time", local_date, local_clock.strftime("%H:%M")
    show.repeat_days = show.repeat_time = None
    shows.schedule(s, show, workspace, max(first, now.replace(second=0, microsecond=0)), now)


# ---------- Weekly shows ----------

def parse_days(values):
    days = sorted({int(value) for value in values if str(value).isdigit() and 0 <= int(value) <= 6})
    return days


def days_of(show):
    return [int(day) for day in (show.repeat_days or "").split(",") if day.isdigit()]


def next_occurrence(days, local_clock, zone_name, after):
    """The first weekday-at-time (in that zone) strictly after `after` (naive UTC)."""
    start = to_zone(after, zone_name).date()
    for offset in range(8):
        day = start + timedelta(days=offset)
        if day.weekday() in days:
            moment = at_local(day, local_clock, zone_name)
            if moment > after:
                return moment
    return None


def schedule_weekly(s, show, workspace, days, local_clock, now):
    if not days:
        raise shows.ShowError("Choose at least one day of the week.")
    if show.is_ab:
        raise shows.ShowError("A weekly show can't be an A/B test. Turn the test off first.")
    show.repeat_days, show.repeat_time = ",".join(str(day) for day in days), local_clock.strftime("%H:%M")
    show.send_mode, show.local_date, show.local_time = "at_once", None, None
    shows.schedule(s, show, workspace, next_occurrence(days, local_clock, workspace.time_zone, now), now)


def describe_repeat(show):
    days = days_of(show)
    names = "weekday" if days == [0, 1, 2, 3, 4] else ", ".join(WEEKDAYS[day] for day in days)
    clock = parse_clock(show.repeat_time)
    return f"Every {names} at {clock.hour % 12 or 12}:{clock.minute:02d} {'AM' if clock.hour < 12 else 'PM'}"


def stop_repeating(show):
    show.repeat_days = show.repeat_time = None


def back_on_standby(s, show, perf, now):
    """A weekly show's performance finished: Standby at its next time (sending.on_finished)."""
    if not show.repeats:
        return False
    clock = parse_clock(show.repeat_time)
    upcoming = next_occurrence(days_of(show), clock, _zone(s, show), max(now, perf.starts_at))
    s.execute(update(Show).where(Show.id == show.id, Show.status == shows.GO)
              .values(status=shows.STANDBY, send_at=upcoming, updated_at=now, version=Show.version + 1)
              .execution_options(synchronize_session=False))
    return True


def _zone(s, show):
    return s.get(Workspace, show.workspace_id).time_zone


# ---------- The A/B subject test ----------

def in_test(show_id, contact_id, percent):
    """(in the test?, 'a' or 'b'): the same answer every time for the same show and person."""
    number = int.from_bytes(hashlib.sha256(f"{show_id}:{contact_id}".encode()).digest()[:8], "big")
    return number % 100 < percent, "a" if (number // 100) % 2 == 0 else "b"


def ab_start(s, show, perf, now):
    """scheduler.on_start: when the test will be decided."""
    if show.is_ab:
        perf.ab_decide_at = perf.starts_at + timedelta(hours=show.ab_wait_hours)
        queue.enqueue(s, "ab_decide", {"performance_id": perf.id}, run_at=perf.ab_decide_at,
                      workspace_id=show.workspace_id, priority=3, dedupe_key=f"ab:{perf.id}")


def ab_split(s, show, perf, messages):
    """scheduler.batch_rules: a subject for the test group; everyone else waits for the winner."""
    if not show.is_ab:
        return
    for message in messages:
        if perf.ab_winner:
            message.variant = perf.ab_winner  # a late batch, after the decision
            continue
        testing, variant = in_test(show.id, message.contact_id, show.ab_percent)
        if testing:
            message.variant = variant
        else:
            message.status = sending.WAITING


def ab_numbers(s, perf_id):
    """{'a': {...}, 'b': {...}}: test emails, delivered, opened, clicked per subject."""
    rows = s.execute(select(Message.variant, func.count(), func.count(Message.delivered_at),
                            func.count(Message.opened_at), func.count(Message.clicked_at))
                     .where(Message.performance_id == perf_id, Message.variant.in_(("a", "b")))
                     .group_by(Message.variant)).all()
    numbers = {variant: {"emails": 0, "delivered": 0, "opened": 0, "clicked": 0} for variant in ("a", "b")}
    for variant, emails, delivered, opened, clicked in rows:
        numbers[variant] = {"emails": emails, "delivered": delivered, "opened": opened, "clicked": clicked}
    return numbers


def pick_winner(numbers, metric):
    def rate(variant):
        found = numbers[variant]
        return found["clicked" if metric == "clicks" else "opened"] / found["delivered"] if found["delivered"] else 0
    return "b" if rate("b") > rate("a") else "a"


@queue.handler("ab_decide")
def ab_decide(s, job, now):
    perf = s.get(Performance, job.payload["performance_id"])
    if perf is None or perf.ab_winner or perf.status in ("cut", "done"):
        return
    show = s.get(Show, perf.show_id)
    workspace = s.get(Workspace, show.workspace_id)
    perf.ab_winner = pick_winner(ab_numbers(s, perf.id), show.ab_metric)
    waiting = s.scalars(select(Message).where(Message.performance_id == perf.id,
                                              Message.status == sending.WAITING)).all()
    for message in waiting:
        message.variant, message.status, message.updated_at = perf.ab_winner, sending.WINGS, now
        contact = s.get(Contact, message.contact_id) if message.contact_id else None
        if contact is not None:
            message.due_at = after_quiet(now, person_zone(contact, workspace), workspace.quiet_start,
                                         workspace.quiet_end)
    s.flush()
    for message in waiting:
        sending.enqueue_send(s, message, max(now, message.due_at))
    sending.finish_performance_if_done(s, perf, now)


# ---------- Settings: quiet hours ----------

def save_quiet(s, workspace, form):
    if form.get("quiet") != "on":
        workspace.quiet_start = workspace.quiet_end = None
        return {}, "Quiet hours are off: emails go at any hour."
    start, end = parse_clock(form.get("quiet_start")), parse_clock(form.get("quiet_end"))
    if start is None or end is None or start == end:
        return {"quiet": "Choose two different times."}, None
    workspace.quiet_start, workspace.quiet_end = minutes_of(start), minutes_of(end)
    return {}, "Quiet hours are saved."


def week_start(day):
    return day - timedelta(days=day.weekday())


def init_app(app):
    from .views import settings
    if due_rule not in scheduler.due_rules:
        scheduler.due_rules.append(due_rule)
        scheduler.on_start.append(ab_start)
        scheduler.batch_rules.append(ab_split)
        sending.before_send.insert(0, quiet_check)  # before any limit takes a token
        sending.on_finished.append(back_on_standby)
    settings.actions["quiet"] = save_quiet

