"""Smart sending: their own time zone, quiet hours, the A/B test, weekly shows, the Call Board."""
import re
from datetime import date, datetime, time, timedelta

import pytest
from sqlalchemy import func, select

from oncue import db, sending, shows, smart, tracking, worker
from oncue.dates import from_zone, to_zone, utcnow
from oncue.models import Contact, Message, Performance, PracticeMail, Show, Workspace

from .conftest import CALM, post, text, words

MONDAY = date(2026, 11, 16)
T0 = datetime(2026, 11, 15, 12, 0)  # Sunday 8:00 PM in Manila


def setup(ctx, people=(("ana@example.com", None),), **settings):
    workspace = Workspace(name="Teatro", sender_name="Teatro", sender_email="hi@teatro.example",
                          **{**CALM, "send_rate": 50, **settings})
    ctx.add(workspace)
    ctx.flush()
    for email, zone_name in people:
        ctx.add(Contact(workspace_id=workspace.id, email=email, first_name="X", time_zone=zone_name, fields={}))
    show = Show(workspace_id=workspace.id, name="Opening", subject="Subject A", body="[Book](https://t.example/b)")
    ctx.add(show)
    ctx.commit()
    return workspace, show


def sent_at(ctx, email):
    ctx.expire_all()
    return ctx.scalar(select(Message.sent_at).where(Message.email == email))


# ---------- Quiet hours ----------

@pytest.mark.parametrize("minute, quiet", [(22 * 60 + 30, True), (7 * 60 + 59, True), (8 * 60, False),
                                           (12 * 60, False), (21 * 60, True), (0, True)])
def test_quiet_hours_wrap_past_midnight(minute, quiet):
    assert smart.in_quiet(minute, 21 * 60, 8 * 60) is quiet


def test_quiet_hours_in_the_day_and_off():
    assert smart.in_quiet(13 * 60, 12 * 60, 14 * 60) and not smart.in_quiet(14 * 60, 12 * 60, 14 * 60)
    assert not smart.in_quiet(3 * 60, None, None)


def test_an_email_due_at_night_waits_for_morning(ctx):
    # 10:30 PM in Manila (UTC+8) -> 8:00 AM the next morning there.
    late = datetime(2026, 11, 16, 14, 30)
    assert smart.after_quiet(late, "Asia/Manila", 21 * 60, 8 * 60) == datetime(2026, 11, 17, 0, 0)
    # 2:00 AM: the window began yesterday evening and ends this morning.
    assert smart.after_quiet(datetime(2026, 11, 16, 18, 0), "Asia/Manila", 21 * 60, 8 * 60) == datetime(2026, 11, 17, 0, 0)


def test_a_show_at_night_respects_quiet_hours(ctx):
    _workspace, show = setup(ctx, quiet_start=21 * 60, quiet_end=8 * 60)
    show.status, show.send_at = "standby", datetime(2026, 11, 16, 14, 0)  # 10:00 PM in Manila
    ctx.commit()
    worker.simulate(ctx, show.send_at, show.send_at + timedelta(hours=12))
    assert sent_at(ctx, "ana@example.com") == datetime(2026, 11, 17, 0, 0)  # 8:00 AM Manila


# ---------- Their own time zone ----------

def test_nine_am_in_each_persons_own_time_zone(ctx):
    workspace, show = setup(ctx, [("manila@example.com", "Asia/Manila"), ("london@example.com", "Europe/London"),
                                  ("la@example.com", "America/Los_Angeles"), ("home@example.com", None)])
    smart.schedule_their_time(ctx, show, workspace, MONDAY, time(9, 0), T0)
    ctx.commit()
    assert show.send_at == datetime(2026, 11, 15, 19, 0)  # 9:00 AM Monday in UTC+14
    worker.simulate(ctx, T0, datetime(2026, 11, 17, 0, 0))
    assert sent_at(ctx, "manila@example.com") == datetime(2026, 11, 16, 1, 0)
    assert sent_at(ctx, "home@example.com") == datetime(2026, 11, 16, 1, 0)  # the workspace's zone
    assert sent_at(ctx, "london@example.com") == datetime(2026, 11, 16, 9, 0)
    assert sent_at(ctx, "la@example.com") == datetime(2026, 11, 16, 17, 0)
    assert ctx.get(Show, show.id).status == "curtain_down"


def test_where_nine_am_has_passed_it_goes_at_once(ctx):
    workspace, show = setup(ctx, [("manila@example.com", "Asia/Manila"), ("la@example.com", "America/Los_Angeles")])
    late = datetime(2026, 11, 16, 5, 0)  # 1:00 PM in Manila, 9:00 PM Sunday in LA
    smart.schedule_their_time(ctx, show, workspace, MONDAY, time(9, 0), late)
    ctx.commit()
    worker.simulate(ctx, late, datetime(2026, 11, 17, 0, 0))
    assert sent_at(ctx, "manila@example.com") == late
    assert sent_at(ctx, "la@example.com") == datetime(2026, 11, 16, 17, 0)


def test_a_time_that_has_passed_everywhere_is_refused(ctx):
    workspace, show = setup(ctx)
    with pytest.raises(shows.ShowError, match="passed everywhere"):
        smart.schedule_their_time(ctx, show, workspace, date(2026, 11, 1), time(9, 0), T0)


def test_daylight_saving_days():
    # New York springs forward at 2:00 AM on March 8, 2026: 2:30 AM doesn't exist, so it means 3:30 EDT.
    assert from_zone(datetime(2026, 3, 8, 2, 30), "America/New_York") == datetime(2026, 3, 8, 7, 30)
    # 9:00 AM on either side of the change is still 9:00 AM local.
    assert from_zone(datetime(2026, 3, 7, 9, 0), "America/New_York") == datetime(2026, 3, 7, 14, 0)
    assert from_zone(datetime(2026, 3, 9, 9, 0), "America/New_York") == datetime(2026, 3, 9, 13, 0)
    # London falls back on October 25, 2026: 1:30 AM happens twice; the first (BST) is taken.
    assert from_zone(datetime(2026, 10, 25, 1, 30), "Europe/London") == datetime(2026, 10, 25, 0, 30)


# ---------- The A/B test ----------

def test_the_split_is_the_same_every_time():
    assert smart.in_test(7, 41, 20) == smart.in_test(7, 41, 20)
    share = sum(smart.in_test(7, contact_id, 20)[0] for contact_id in range(2000)) / 2000
    assert 0.17 < share < 0.23


def test_the_ab_winner_goes_to_everyone_else(ctx):
    workspace, show = setup(ctx, [(f"p{i}@example.com", None) for i in range(60)])
    show.subject_b, show.ab_percent, show.ab_wait_hours, show.ab_metric = "Subject B", 50, 4, "opens"
    show.status, show.send_at = "standby", T0
    ctx.commit()
    worker.simulate(ctx, T0, T0 + timedelta(minutes=10))  # the test group goes; the rest wait
    ctx.expire_all()
    counts = sending.counts(ctx, show.id)
    tested = ctx.scalars(select(Message).where(Message.variant.in_(("a", "b")))).all()
    assert counts.get("waiting", 0) == 60 - len(tested) and 15 < len(tested) < 45
    # Everyone who got B opens it; nobody opens A.
    for message in tested:
        message.opened_at = None
        if message.variant == "b":
            tracking.record(ctx, message, "opened", "brevo", f"o{message.id}", T0 + timedelta(minutes=20))
    ctx.commit()
    worker.simulate(ctx, T0 + timedelta(minutes=10), T0 + timedelta(hours=5))
    perf = ctx.scalar(select(Performance))
    assert perf.ab_winner == "b" and perf.ab_decide_at == T0 + timedelta(hours=4)
    subjects = ctx.scalars(select(PracticeMail.subject)).all()
    assert len(subjects) == 60 and subjects.count("Subject A") == sum(m.variant == "a" for m in tested)
    assert ctx.get(Show, show.id).status == "curtain_down"


def test_a_tie_goes_to_a():
    even = {"a": {"delivered": 10, "opened": 3, "clicked": 0, "emails": 10},
            "b": {"delivered": 10, "opened": 3, "clicked": 0, "emails": 10}}
    assert smart.pick_winner(even, "opens") == "a"


# ---------- Weekly shows ----------

def test_a_weekly_show_comes_back_on_standby(ctx):
    workspace, show = setup(ctx, [("ana@example.com", None), ("ben@example.com", None)])
    smart.schedule_weekly(ctx, show, workspace, [0, 3], time(9, 0), T0)  # Mondays and Thursdays at 9:00 AM
    ctx.commit()
    assert show.send_at == datetime(2026, 11, 16, 1, 0)
    worker.simulate(ctx, T0, datetime(2026, 11, 22, 0, 0))  # a week: Monday and Thursday
    ctx.expire_all()
    show = ctx.get(Show, show.id)
    starts = ctx.scalars(select(Performance.starts_at).order_by(Performance.number)).all()
    assert starts == [datetime(2026, 11, 16, 1, 0), datetime(2026, 11, 19, 1, 0)]
    assert show.status == "standby" and show.send_at == datetime(2026, 11, 23, 1, 0)
    assert ctx.scalar(select(func.count()).select_from(Message)) == 4  # two people, twice
    assert smart.describe_repeat(show) == "Every Monday, Thursday at 9:00 AM"


def test_next_occurrence_skips_to_the_next_day():
    clock = time(9, 0)
    after = datetime(2026, 11, 16, 1, 0)  # exactly Monday 9:00 Manila: the NEXT one is Thursday
    assert smart.next_occurrence([0, 3], clock, "Asia/Manila", after) == datetime(2026, 11, 19, 1, 0)


# ---------- The pages ----------

def make_show(app, workspace_id, **fields):
    with app.app_context():
        s = db.session()
        s.add(Contact(workspace_id=workspace_id, email="ana@example.com", fields={}))
        show = Show(workspace_id=workspace_id, name="Weekly Notes", subject="Hi", body="x", **fields)
        s.add(show)
        s.commit()
        return show.id


def get_show(app, show_id):
    with app.app_context():
        return db.session().get(Show, show_id)


def test_schedule_weekly_and_stop_from_the_page(app, owner):
    client, workspace_id = owner
    show_id = make_show(app, workspace_id)
    response = post(client, f"/shows/{show_id}/schedule", {"mode": "weekly", "repeat_days": ["1", "4"],
                                                           "repeat_time": "18:30"}, follow_redirects=True)
    assert "every tuesday, friday at 6:30 pm" in text(response)
    page = words(client.get(f"/shows/{show_id}"))
    assert "Every Tuesday, Friday at 6:30 PM" in page and "Stop repeating" in page
    post(client, f"/shows/{show_id}/stop-repeating")
    show = get_show(app, show_id)
    assert show.repeat_days is None and show.status == "standby"
    nobody = post(client, f"/shows/{show_id}/schedule", {"mode": "weekly", "repeat_time": "09:00"}, follow_redirects=True)
    assert "Choose at least one day" in text(nobody)


def test_schedule_their_time_from_the_page(app, owner):
    client, workspace_id = owner
    show_id = make_show(app, workspace_id)
    tomorrow = (datetime.now() + timedelta(days=2)).date().isoformat()
    post(client, f"/shows/{show_id}/schedule", {"mode": "their_time", "local_date": tomorrow, "local_time": "09:00"})
    show = get_show(app, show_id)
    assert show.send_mode == "their_time" and show.local_time == "09:00" and show.status == "standby"
    assert "in each person's own time zone" in text(client.get(f"/shows/{show_id}"))


def test_the_ab_fields_in_the_builder(app, owner):
    client, workspace_id = owner
    show_id = make_show(app, workspace_id)
    base = {"name": "Weekly Notes", "audience": "all", "subject": "Hi", "body": "x", "ab_on": "yes",
            "ab_percent": "20", "ab_wait_hours": "4", "ab_metric": "clicks"}
    same = post(client, f"/shows/{show_id}/edit", {**base, "subject_b": "Hi"})
    assert same.status_code == 400 and "must be different" in text(same)
    post(client, f"/shows/{show_id}/edit", {**base, "subject_b": "Hello"})
    show = get_show(app, show_id)
    assert (show.subject_b, show.ab_metric, show.is_ab) == ("Hello", "clicks", True)
    page = words(client.get(f"/shows/{show_id}"))
    assert "A/B Test" in page and "Subject B Hello" in page
    assert "can't use their own time zones or repeat" in page


def test_quiet_hours_in_settings(app, owner):
    client, workspace_id = owner
    post(client, "/settings", {"action": "quiet", "quiet": "on", "quiet_start": "21:30", "quiet_end": "07:00"})
    with app.app_context():
        workspace = db.session().get(Workspace, workspace_id)
        assert (workspace.quiet_start, workspace.quiet_end) == (21 * 60 + 30, 7 * 60)
    post(client, "/settings", {"action": "quiet"})
    with app.app_context():
        assert db.session().get(Workspace, workspace_id).quiet_start is None


def test_the_call_board(app, owner):
    client, workspace_id = owner
    weekly = make_show(app, workspace_id)
    with app.app_context():
        s = db.session()
        workspace = s.get(Workspace, workspace_id)
        show = s.get(Show, weekly)
        smart.schedule_weekly(s, show, workspace, [0, 1, 2, 3, 4, 5, 6], time(10, 0), datetime.now())
        s.commit()
    # A weekly show is one card in the Every Week lane, not a card on every day.
    page = text(client.get("/call-board/"))
    assert page.count("Weekly Notes") == 1 and "Every day at 10:00 AM" in page and "Every Week" in page
    assert 'class="week-board"' in page
    month = text(client.get("/call-board/?view=month"))
    assert month.count("Weekly Notes") == 1 and "month-board" in month
    assert client.get("/call-board/?week=-1").status_code == 200


def test_a_weekly_card_opens_its_own_performance(app, owner):
    """A weekly show's past runs are separate cards. Each opens that run (its date, its meter,
    marked in the list), not the show's next time, which is a different day."""
    client, workspace_id = owner
    weekly = make_show(app, workspace_id)
    with app.app_context():
        s = db.session()
        show = s.get(Show, weekly)
        smart.schedule_weekly(s, show, s.get(Workspace, workspace_id), [0, 1, 2, 3, 4, 5, 6], time(10, 0), utcnow())
        today = smart.week_start(to_zone(utcnow(), "Asia/Manila").date())
        for number, days in ((1, 0), (2, 1)):  # Sunday and Monday of this week, 10:00 AM Manila
            moment = from_zone(datetime.combine(today + timedelta(days=days), time(10, 0)), "Asia/Manila")
            s.add(Performance(workspace_id=workspace_id, show_id=weekly, number=number, status="done",
                              starts_at=moment, finished_at=moment, fanned_out=True))
        s.commit()
    board = text(client.get("/call-board/"))
    links = re.findall(rf'href="/shows/{weekly}\?run=(\d)"', board)
    assert sorted(links) == ["1", "2"]
    sunday = f"{today:%a}, {today:%b} {today.day}"
    assert sunday in board  # each card says its date
    page = text(client.get(f"/shows/{weekly}?run=1"))
    assert f"Showing performance 1 of 2, which went out <strong>{sunday}, 10:00 AM" in page
    assert re.search(r'<li class="is-chosen" aria-current="true"><h3><a href="[^"]*run=1">Performance 1', page)
    assert "All 2 performances together." in page
    newest = text(client.get(f"/shows/{weekly}"))
    assert "Showing performance" not in newest and "is-chosen" not in newest


def test_weeks_run_sunday_to_saturday(app, owner):
    """Like a wall calendar: Sun Mon Tue Wed Thu Fri Sat, on the board and on the show page."""
    for day in (date(2026, 11, 15), date(2026, 11, 18), date(2026, 11, 21)):  # Sunday, Wednesday, Saturday
        assert smart.week_start(day) == date(2026, 11, 15)
    assert smart.week_start(date(2026, 11, 22)) == date(2026, 11, 22)  # the next Sunday starts a new week
    client, workspace_id = owner
    page = text(client.get("/call-board/"))
    heads = re.findall(r'class="board-day-head"><span class="tape[^"]*">(\w{3}) ', page)
    assert heads == ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]
    month = text(client.get("/call-board/?view=month"))
    assert re.findall(r'<span class="tape">(\w{3})</span>', month.split('class="month-head"')[1])[:7] == \
        ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]
    weekly = make_show(app, workspace_id)
    with app.app_context():
        s = db.session()
        show = s.get(Show, weekly)
        smart.schedule_weekly(s, show, s.get(Workspace, workspace_id), [0, 6], time(9, 0), datetime.now())
        s.commit()
        assert smart.describe_repeat(show) == "Every Sunday, Monday at 9:00 AM"
    boxes = re.findall(r'name="repeat_days" value="(\d)"', text(client.get(f"/shows/{weekly}")))
    assert boxes == ["6", "0", "1", "2", "3", "4", "5"]  # Sun first; the stored numbers don't change
