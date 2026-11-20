"""Try Demo: every visitor gets a sandbox workspace of their own.

The demo is "Teatro Luna", a made-up community theatre that emails its
audience. Its contacts have made-up addresses, and a demo workspace can only
send to the practice mail server (mailers.py), so nothing a demo does ever
reaches a real inbox. A demo is deleted 24 hours after it starts.

Building it has two parts:
1. build(), during the Try Demo click: the audience, scripts, and the shows
   still to come. Quick.
2. The demo's PAST (shows sent over the last six weeks, with real retries,
   bounces, opens and clicks) is not written by hand: a demo_history job
   REPLAYS it through the real scheduler, queue, sender and practice server,
   on a fast clock (worker.simulate, scoped to this one workspace). While it
   replays, workspace.replaying keeps the live crew away from it, so nothing
   runs at the wrong time. The visitor watches Curtain Calls and Reviews fill
   in: the background-job system demonstrating itself.
"""
import random
import secrets
from datetime import timedelta

from sqlalchemy import delete, func, select

from . import auth, queue
from .dates import utcnow
from .models import Job, User, Worker, Workspace

DEMO_LIFETIME = timedelta(hours=24)
MAX_DEMOS = 100
DEMO_NAME = "Luna Reyes"


def count(s):
    return s.scalar(select(func.count()).select_from(Workspace).where(Workspace.is_demo.is_(True)))


def purge(s, moment):
    """Delete demos whose time is up. Everything inside goes with them (ON DELETE CASCADE)."""
    gone = s.scalars(select(Workspace.id).where(Workspace.is_demo.is_(True),
                                                Workspace.demo_expires_at <= moment)).all()
    if gone:
        s.execute(delete(Workspace).where(Workspace.id.in_(gone)))
    # A replay job isn't tied to its demo (the live crew must run it while the demo is replaying),
    # so it doesn't go with the demo: finished ones older than any demo are cleared here.
    s.execute(delete(Job).where(Job.kind == "demo_history", Job.status.in_((queue.DONE, queue.DEAD, queue.DISCARDED)),
                                Job.created_at <= moment - DEMO_LIFETIME))
    return len(gone)


FIRST = ["Ana", "Ben", "Carla", "Dante", "Elena", "Franco", "Gemma", "Hector", "Isabel", "Jomar", "Kristine",
         "Leo", "Mika", "Nestor", "Olivia", "Paolo", "Queenie", "Ramon", "Sofia", "Tomas", "Ursula", "Vince",
         "Wena", "Xander", "Yasmin", "Zeny", "Andres", "Bea", "Carlo", "Divina"]
LAST = ["Santos", "Reyes", "Cruz", "Bautista", "Garcia", "Mendoza", "Torres", "Villanueva", "Ramos", "Aquino",
        "Castillo", "Navarro", "Dela Cruz", "Flores", "Lim", "Tan", "Ocampo", "Rivera", "Salazar", "Pascual"]
DOMAINS = ["gmail.example", "gmail.example", "yahoo.example", "outlook.example", "icloud.example"]
CITIES = ["Quezon City", "Quezon City", "Manila", "Makati", "Pasig", "Cebu", "Davao", "Baguio", "Iloilo"]
# Fans abroad (many Filipinos work overseas): they get shows at 9:00 AM their own time.
ABROAD = ["Asia/Tokyo", "Asia/Singapore", "Asia/Dubai", "Europe/London", "America/Los_Angeles",
          "Australia/Sydney", "America/Toronto"]
CONTACTS = 120


def seed_audience(s, workspace, moment):
    """Teatro Luna's audience: made-up people on made-up domains (.example never
    receives mail), so even a mistake could never email a real person."""
    from . import audience
    from .models import Contact, ContactList, Segment
    rng = random.Random(2026)  # the same audience in every demo (and in the README's pictures)
    people = []
    for index in range(CONTACTS):
        # Shift the surname every round of first names, so no name comes twice.
        first, last = FIRST[index % len(FIRST)], LAST[(index * 7 + index // len(FIRST)) % len(LAST)]
        local = f"{first}.{last}".lower().replace(" ", "")
        if index in (11, 47):
            local = f"flaky.{local}"  # the practice mail server says "try later" twice first
        if index == 29:
            local = f"slow.{local}"
        if index == 84:
            local = f"busy.{local}"  # always "too many": the Backstage dead letter
        if index == 90:
            local = f"bounce.{local}"  # taken, then bounces: the do-not-send list grows
        email = f"{local}{index if index >= len(FIRST) else ''}@{DOMAINS[index % len(DOMAINS)]}"
        days_ago = rng.randint(0, 400) if index > 6 else rng.randint(0, 20)
        plan = rng.choice(["Season pass", "Season pass", "Single tickets", "Single tickets", "Student pass"])
        contact = Contact(workspace_id=workspace.id, email=email, first_name=first, last_name=last,
                          fields={"city": rng.choice(CITIES), "plan": plan},
                          time_zone=rng.choice(ABROAD) if index % 9 == 4 else None,
                          consent_at=moment - timedelta(days=days_ago),
                          consent_note="Signed up at the box office" if index % 3 else "Website sign-up form",
                          created_at=moment - timedelta(days=days_ago), updated_at=moment)
        s.add(contact)
        people.append((contact, plan))
    s.flush()
    tags = {"Season pass": ["member"], "Student pass": ["student"], "Single tickets": []}
    for index, (contact, plan) in enumerate(people):
        extra = (["donor"] if index % 11 == 3 else []) + (["volunteer"] if index % 13 == 5 else []) + \
                (["press"] if index in (8, 64) else [])
        audience.set_tags(contact, tags[plan] + extra)
    lists = {
        "Season Subscribers": ("Everyone with a season pass: opening nights, schedules, member perks.", True),
        "Workshop News": ("Acting and playwriting workshops for all ages.", True),
        "Volunteers": ("Ushers, ticket-takers and set builders.", False),
        "Press and Partners": ("Reviewers, schools and sponsors.", False),
    }
    made = {}
    for name, (description, topic) in lists.items():
        made[name] = ContactList(workspace_id=workspace.id, name=name, description=description, is_topic=topic,
                                 created_at=moment - timedelta(days=400))
        s.add(made[name])
    s.flush()
    members = {
        "Season Subscribers": [c.id for c, plan in people if plan == "Season pass"],
        "Workshop News": [c.id for i, (c, plan) in enumerate(people) if plan == "Student pass" or i % 4 == 1],
        "Volunteers": [c.id for c in people_with(people, "volunteer")],
        "Press and Partners": [c.id for c in people_with(people, "press")] + [people[3][0].id],
    }
    for name, ids in members.items():
        audience.add_to_list(s, made[name].id, ids, moment - timedelta(days=30))
    s.add_all([
        Segment(workspace_id=workspace.id, name="Fans Abroad", match="all",
                rules=[{"field": "time_zone", "op": "is_set", "value": ""}], created_at=moment),
        Segment(workspace_id=workspace.id, name="Cebu and Davao", match="any",
                rules=[{"field": "field:city", "op": "is", "value": "Cebu"},
                       {"field": "field:city", "op": "is", "value": "Davao"}], created_at=moment),
        Segment(workspace_id=workspace.id, name="New This Month", match="all",
                rules=[{"field": "added", "op": "within", "value": "30"}], created_at=moment),
        Segment(workspace_id=workspace.id, name="Students in Metro Manila", match="all",
                rules=[{"field": "tag", "op": "has", "value": "student"},
                       {"field": "field:city", "op": "is_not", "value": "Cebu"},
                       {"field": "field:city", "op": "is_not", "value": "Davao"}], created_at=moment),
    ])
    # Three who unsubscribed, and the do-not-send list.
    for contact, _plan in people[100:103]:
        audience.unsubscribe(s, contact, moment - timedelta(days=12))
    audience.suppress(s, workspace.id, people[37][0].email, "bounced", moment - timedelta(days=20),
                      "The mailbox doesn't exist (550 5.1.1).")
    audience.suppress(s, workspace.id, people[58][0].email, "complained", moment - timedelta(days=9),
                      "Marked the March newsletter as spam.")
    audience.suppress(s, workspace.id, "old.boxoffice@teatro-luna.example", "manual", moment - timedelta(days=60),
                      "Our old box office address. Nobody reads it.")
    return people


SCRIPTS = {
    "Welcome to the Company": (
        "Welcome to Teatro Luna, {{first_name|friend}}",
        "Your seat in the house is saved.",
        "Hi {{first_name|there}},\n\nThank you for joining **Teatro Luna**. Every month we'll tell you what's "
        "on stage, when tickets open, and how to join a workshop.\n\n"
        "Our next play is *Ang Huling Ilaw* (The Last Light). [See the season](https://teatro-luna.example/season).\n\n"
        "See you at the theatre,\n\nLuna and the company"),
    "Show Reminder": (
        "Tonight: doors open at 7, {{first_name|friend}}",
        "Bring your ticket on your phone.",
        "Hi {{first_name|there}},\n\nA reminder that **tonight** is the night. Doors open at **7:00 PM** and the "
        "curtain goes up at 7:30.\n\n- Show your ticket on your phone at the door\n- Parking is on Mabini Street\n\n"
        "[Get directions](https://teatro-luna.example/visit)\n\nBreak a leg (that's theatre for good luck)!"),
    "Monthly Programme": (
        "What's on in {{city|the city}} this month",
        "Three plays, two workshops and a reading.",
        "# This Month at Teatro Luna\n\nHi {{first_name|there}}, here's what's coming up:\n\n"
        "1. **Ang Huling Ilaw** (The Last Light), Fridays and Saturdays\n"
        "2. **Playwriting for Beginners**, a four-week workshop\n"
        "3. **A reading of new plays** by students\n\n"
        "As a {{plan|friend of the theatre}} holder you get first pick of seats. "
        "[Book now](https://teatro-luna.example/book)."),
}


def seed_scripts_and_shows(s, workspace, moment):
    from .models import ContactList, Script, Show
    made = {}
    for name, (subject, preheader, body) in SCRIPTS.items():
        made[name] = Script(workspace_id=workspace.id, name=name, subject=subject, preheader=preheader, body=body,
                            created_at=moment - timedelta(days=90), updated_at=moment - timedelta(days=5))
        s.add(made[name])
    s.flush()
    season = s.scalar(select(ContactList.id).where(ContactList.workspace_id == workspace.id,
                                                   ContactList.name == "Season Subscribers"))
    reminder = made["Show Reminder"]
    programme = made["Monthly Programme"]
    # One on Standby the day after tomorrow at 9:00 AM Manila time, and one still in rehearsal.
    nine = (moment + timedelta(days=2)).replace(hour=1, minute=0, second=0, microsecond=0)
    s.add_all([
        Show(workspace_id=workspace.id, name="December Programme", subject=programme.subject,
             preheader=programme.preheader, body=programme.body, script_id=programme.id, audience_kind="all",
             status="standby", send_at=nine, created_at=moment - timedelta(days=3), updated_at=moment),
        Show(workspace_id=workspace.id, name="Closing Night Reminder", subject=reminder.subject,
             preheader=reminder.preheader, body=reminder.body, script_id=reminder.id, audience_kind="list",
             audience_ref=str(season), created_at=moment - timedelta(days=1), updated_at=moment),
    ])
    add_smart_examples(s, workspace, moment, made)
    return made


def add_smart_examples(s, workspace, moment, made):
    """Smart sending in the demo: a weekly show, and one at 7 PM in each fan's own time zone. (Quiet hours
    stay off, so Start now sends at any hour; they are one switch away in Settings.)"""
    from . import smart
    from .dates import local_today, parse_clock
    from .models import ContactList, Segment, Show
    volunteers = s.scalar(select(ContactList.id).where(ContactList.workspace_id == workspace.id,
                                                       ContactList.name == "Volunteers"))
    abroad = s.scalar(select(Segment.id).where(Segment.workspace_id == workspace.id, Segment.name == "Fans Abroad"))
    weekly = Show(workspace_id=workspace.id, name="Volunteer Call Sheet", subject="This week's call sheet, {{first_name|friend}}",
                  preheader="Who's ushering, who's on tickets.",
                  body="Hi {{first_name|there}},\n\nHere's this week's **call sheet**. Reply if you can't make your shift.\n\n"
                       "[See the schedule](https://teatro-luna.example/volunteers)",
                  audience_kind="list", audience_ref=str(volunteers), status="standby", repeat_days="0,3",
                  repeat_time="10:00", created_at=moment - timedelta(days=40), updated_at=moment)
    weekly.send_at = smart.next_occurrence([0, 3], parse_clock("10:00"), workspace.time_zone,
                                           moment - timedelta(days=15))
    day = local_today(workspace.time_zone, moment) + timedelta(days=3)
    livestream = Show(workspace_id=workspace.id, name="Livestream for Fans Abroad",
                      subject="Watch opening night from {{city|wherever you are}}",
                      preheader="Live at 7 PM your time, with English subtitles.",
                      body="Hi {{first_name|there}},\n\nYou can't be in the theatre, so the theatre comes to you. "
                           "**Opening night is streamed live**, at 7:00 PM your time.\n\n"
                           "[Get your stream ticket](https://teatro-luna.example/stream)",
                      audience_kind="segment", audience_ref=str(abroad), status="standby", send_mode="their_time",
                      local_date=day, local_time="19:00", created_at=moment - timedelta(days=2), updated_at=moment)
    livestream.send_at = max(smart.their_time_window(day, parse_clock("19:00"))[0], moment)
    s.add_all([weekly, livestream])


HISTORY_DAYS = 46


def add_live_example(s, workspace, moment):
    """A short reminder that goes on Go one minute after the visitor arrives, so they can watch it send."""
    from .models import Segment, Show
    cebu = s.scalar(select(Segment.id).where(Segment.workspace_id == workspace.id, Segment.name == "Cebu and Davao"))
    s.add(Show(workspace_id=workspace.id, name="Box Office Opens in Cebu and Davao",
               subject="Tickets for our tour open today, {{first_name|friend}}",
               preheader="Two nights in Cebu, one in Davao.",
               body="Hi {{first_name|there}},\n\nTeatro Luna is **on tour**! Tickets for Cebu and Davao open today.\n\n"
                    "[Get tickets](https://teatro-luna.example/tour)",
               audience_kind="segment", audience_ref=str(cebu), status="standby",
               send_at=moment + timedelta(minutes=1), created_at=moment, updated_at=moment))


def add_past_shows(s, workspace, moment):
    """The shows of the last six weeks, on Standby at their old times: the replay sends them."""
    from .models import ContactList, Script, Show
    def list_id(name):
        return str(s.scalar(select(ContactList.id).where(ContactList.workspace_id == workspace.id,
                                                         ContactList.name == name)))
    programme = s.scalar(select(Script).where(Script.workspace_id == workspace.id, Script.name == "Monthly Programme"))
    welcome = s.scalar(select(Script).where(Script.workspace_id == workspace.id, Script.name == "Welcome to the Company"))
    def at(days, hour_manila):
        day = (moment - timedelta(days=days)).replace(minute=0, second=0, microsecond=0)
        return day.replace(hour=(hour_manila - 8) % 24)
    s.add_all([
        Show(workspace_id=workspace.id, name="October Programme", subject=programme.subject,
             preheader=programme.preheader, body=programme.body, script_id=programme.id, audience_kind="list",
             audience_ref=list_id("Season Subscribers"), status="standby", send_at=at(44, 9),
             created_at=moment - timedelta(days=50)),
        Show(workspace_id=workspace.id, name="Season Opener: Ang Huling Ilaw",
             subject="Opening night is here, {{first_name|friend}}", preheader="The Last Light, live on our stage.",
             body=welcome.body.replace("Thank you for joining", "Opening night is almost here at"),
             audience_kind="all", status="standby", send_at=at(25, 10), created_at=moment - timedelta(days=30)),
        Show(workspace_id=workspace.id, name="Workshop Sign-ups", subject="Write your first play this month",
             subject_b="{{first_name|Friend}}, a seat is saved for you", ab_percent=30, ab_wait_hours=4,
             ab_metric="opens", preheader="Four Saturdays, beginners welcome.",
             body="Hi {{first_name|there}},\n\nOur **Playwriting for Beginners** workshop starts this month. "
                  "Four Saturdays, small groups, no experience needed.\n\n[Save your seat](https://teatro-luna.example/workshops)",
             audience_kind="list", audience_ref=list_id("Workshop News"), status="standby", send_at=at(18, 18),
             created_at=moment - timedelta(days=20)),
        Show(workspace_id=workspace.id, name="Thank You, Donors", subject="Because of you, the lights stay on",
             preheader="A note from the whole company.",
             body="Dear {{first_name|friend}},\n\nThis season happened **because of you**. Thank you.\n\n"
                  "[See what your gift built](https://teatro-luna.example/thanks)",
             audience_kind="tag", audience_ref="donor", status="standby", send_at=at(6, 11),
             created_at=moment - timedelta(days=8)),
        Show(workspace_id=workspace.id, name="Rain Delay Notice", subject="Tonight's show moves to Sunday",
             body="Hi {{first_name|there}},\n\nThe typhoon closed the roads, so tonight's show moves to Sunday.",
             audience_kind="all", status="cut", send_at=at(10, 15), finished_at=moment - timedelta(days=10, hours=8),
             created_at=moment - timedelta(days=10, hours=9)),
    ])


def add_now_playing(s, workspace, moment):
    """A small show that started a minute before the visitor arrived. One of its people is a
    busy. address, so when the visitor lands it is still on Go, with a Missed Cue retrying in
    Backstage; they can watch it end as a dead letter, and the curtain come down."""
    from . import audience
    from .models import Contact, ContactList, Show
    holds = ContactList(workspace_id=workspace.id, name="Ticket Holds", is_topic=False,
                        description="People holding tickets they haven't paid for yet.", created_at=moment)
    s.add(holds)
    s.flush()
    people = s.scalars(select(Contact.id).where(Contact.workspace_id == workspace.id).order_by(Contact.id)
                       .offset(78).limit(7)).all()  # includes the busy. address (contact 84)
    audience.add_to_list(s, holds.id, people, moment)
    s.add(Show(workspace_id=workspace.id, name="Ticket Holds Expire Tonight",
               subject="Your held seats are waiting, {{first_name|friend}}",
               preheader="Pay by 6 PM to keep them.",
               body="Hi {{first_name|there}},\n\nYour seats for Saturday are **held until 6 PM tonight**.\n\n"
                    "[Pay for your tickets](https://teatro-luna.example/holds)",
               audience_kind="list", audience_ref=str(holds.id), status="standby",
               send_at=moment - timedelta(minutes=1), created_at=moment - timedelta(hours=2)))


def stage_unsure_example(s, workspace, moment):
    """One Unsure email for Backstage. The practice server remembers every send_token, so a real crash
    can't leave one (see sending.py); this example is set by hand, as Brevo would leave it."""
    from .models import Message, MessageEvent, Show
    message = s.scalar(select(Message).join(Show, Show.id == Message.show_id)
                       .where(Message.workspace_id == workspace.id, Show.name == "Thank You, Donors",
                              Message.status == "delivered").order_by(Message.id).limit(1))
    if message is None:
        return
    s.execute(delete(MessageEvent).where(MessageEvent.message_id == message.id))
    message.status, message.delivered_at, message.opened_at, message.clicked_at = "unsure", None, None, None
    message.last_error = ("The worker stopped while sending, and the mail service can't tell us whether it took the "
                          "email. Check before you resend it.")


def _replay_gave_up(s, job, now):
    """The replay died (its worker kept stopping): let the visitor in anyway, with what was played."""
    workspace = s.get(Workspace, job.payload.get("workspace_id"))
    if workspace is not None and workspace.replaying:
        workspace.replaying = False


@queue.handler("demo_history", on_dead=_replay_gave_up)
def replay_history(s, job, now):
    """Replay a new demo's last six weeks through the real crew, on a fast clock."""
    from . import worker
    workspace = s.get(Workspace, job.payload["workspace_id"])
    if workspace is None or not workspace.replaying:
        return
    moment = workspace.created_at
    add_past_shows(s, workspace, moment)
    add_now_playing(s, workspace, moment)
    s.commit()
    worker_id, workspace_id, job_id = job.locked_by, workspace.id, job.id

    def keep_lease():
        queue.heartbeat(s, s.get(Job, job_id), worker_id, utcnow())

    worker.simulate(s, moment - timedelta(days=HISTORY_DAYS), utcnow(), worker_id=f"replay-{workspace_id}",
                    workspace_id=workspace_id, on_round=keep_lease)
    # The replay's pretend worker leaves no row behind: The Crew lists real workers.
    s.execute(delete(Worker).where(Worker.id == f"replay-{workspace_id}"))
    workspace = s.get(Workspace, workspace_id)
    stage_unsure_example(s, workspace, moment)
    workspace.replaying = False  # the live crew takes over from here
    s.get(Job, job_id)  # the job is reloaded after the simulation's commits


def people_with(people, tag):
    return [contact for contact, _plan in people if tag in contact.tag_names]


def build(s, moment):
    """A new demo workspace with its sample content. Returns the demo user."""
    workspace = Workspace(name="Teatro Luna", is_demo=True, demo_expires_at=moment + DEMO_LIFETIME,
                          sender_name="Teatro Luna", sender_email="hello@teatro-luna.example",
                          postal_address="12 Mabini Street, Quezon City, Philippines", created_at=moment)
    s.add(workspace)
    s.flush()
    # A random session_version: even if an id were ever reused, an old cookie can't match.
    user = auth.create_user(s, workspace, DEMO_NAME, f"demo-{workspace.id}-{secrets.token_hex(4)}@demo.invalid",
                            secrets.token_urlsafe(16), is_demo=True,
                            session_version=secrets.randbelow(10**9) + 2, created_at=moment)
    seed_audience(s, workspace, moment)
    seed_scripts_and_shows(s, workspace, moment)
    add_live_example(s, workspace, moment)
    workspace.replaying = True
    # The key names THIS demo (its number and start), not just its number: on SQLite a deleted
    # demo's number could come back, and its old job's key must never stop the new demo's replay.
    queue.enqueue(s, "demo_history", {"workspace_id": workspace.id}, run_at=moment, priority=1,
                  dedupe_key=f"demo-history:{workspace.id}:{moment.isoformat()}")
    return user


def init_app(app):
    pass


def demo_user(s, workspace_id):
    return s.scalar(select(User).where(User.workspace_id == workspace_id))
