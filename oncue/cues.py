"""The words Right On Cue uses, and what each one means in plain English.

The app talks like a theatre backstage: a campaign is a Show, a scheduled
one is on Standby, sending is Go. Every cue word comes with a plain note,
shown beside it (or under it, or when you point at it), so nobody has to
guess what "Missed Cue" means. Change a word here and it changes everywhere.

tone picks the stamp's colour in style.css (.cue-<tone>).
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class Cue:
    label: str
    note: str
    tone: str


# A show (campaign) goes Rehearsal -> Standby -> Go -> Curtain Down, and can be
# put on Hold or Cut on the way (shows.py has the rules).
SHOW = {
    "rehearsal": Cue("Rehearsal", "Not scheduled yet. You can still change everything.", "rehearsal"),
    "standby": Cue("Standby", "Will go out at the time shown.", "standby"),
    "go": Cue("Go", "Emails are going out now.", "go"),
    "hold": Cue("Hold", "Stopped for now. Nothing more is sent until you resume.", "hold"),
    "curtain_down": Cue("Curtain Down", "Every email has been handled.", "done"),
    "cut": Cue("Cut", "Cancelled. It won't be sent.", "cut"),
}

# One email to one person (messages.status).
MESSAGE = {
    "wings": Cue("In the Wings", "Waiting its turn, or for the sending limit.", "rehearsal"),
    "waiting": Cue("In the Wings", "Waiting for the A/B test to pick a winner.", "rehearsal"),
    "on_stage": Cue("On Stage", "Being handed to the mail service now.", "go"),
    "missed_cue": Cue("Missed Cue", "The mail service said \"try later\". It tries again at the time shown.", "hold"),
    "unsure": Cue("Unsure", "The worker stopped while sending, so we can't tell if it went. "
                            "It isn't sent again by itself (a missing email is better than a double one).", "hold"),
    "sent": Cue("Sent", "The mail service took it. Waiting to hear if it arrived.", "standby"),
    "delivered": Cue("Delivered", "It arrived in the inbox.", "done"),
    "opened": Cue("Opened", "It was opened (a rough guide: some mail apps open every picture).", "done"),
    "clicked": Cue("Clicked", "A link in it was clicked.", "done"),
    "bounced": Cue("Bounced", "The address doesn't take mail. It's on the do-not-send list now.", "bad"),
    "complained": Cue("Marked as Spam", "They marked it as spam. They won't get emails from you again.", "bad"),
    "dropped": Cue("Dropped", "It won't be tried again. The reason is shown.", "bad"),
    "cut": Cue("Cut", "Not sent: the show was cut.", "cut"),
    "skipped": Cue("Skipped", "Not sent: they unsubscribed or are on the do-not-send list.", "cut"),
}

# A job in the queue (jobs.status).
JOB = {
    "queued": Cue("In the Wings", "Waiting for a worker (or for its time).", "rehearsal"),
    "running": Cue("On Stage", "A worker is doing it now.", "go"),
    "done": Cue("Done", "Finished.", "done"),
    "dead": Cue("Dead Letter", "It failed every try. Retry it or discard it.", "bad"),
    "discarded": Cue("Discarded", "Someone chose not to retry it.", "cut"),
}

# Words that aren't statuses.
WORDS = {
    "safety_curtain": Cue("Safety Curtain", "Sending stopped by itself after many failures in a row. "
                                            "It tries again with one email after a short wait.", "bad"),
    "performance": Cue("Performance", "One run of a show. A weekly show has one every week.", "standby"),
}


def show_cue(status):
    return SHOW.get(status) or Cue(status.replace("_", " ").title(), "", "rehearsal")


def message_cue(status):
    return MESSAGE.get(status) or Cue(status.replace("_", " ").title(), "", "rehearsal")


def job_cue(status):
    return JOB.get(status) or Cue(status.title(), "", "rehearsal")


def init_app(app):
    app.jinja_env.globals.update(show_cue=show_cue, message_cue=message_cue, job_cue=job_cue,
                                 SHOW_CUES=SHOW, MESSAGE_CUES=MESSAGE, CUE_WORDS=WORDS)
