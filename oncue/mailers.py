"""Mailers: who actually carries an email to an inbox.

Every mailer has one method, send(outgoing, now), and answers with one of:

- Accepted(provider_id): the mail service took it. (Taking it isn't arriving:
  delivery news comes later, through tracking.py.)
- TryLater(reason, retry_after): busy, down, slow, or "too many, wait". The
  job will retry.
- Rejected(reason): it will never work (a bad address, a hard bounce). No retry.

A mailer may also raise an error (a timeout, a dropped connection); the
sender treats that like TryLater.

Which mailer a workspace uses (for_workspace):
- a demo ALWAYS uses the practice mail server, so a demo can never email a
  real person;
- the owner uses Brevo (brevo.py) or their own Gmail (gmail.py) once that
  one's settings are on the server and Settings say so, otherwise the
  practice mail server too.
"""
from dataclasses import dataclass, field


@dataclass
class Outgoing:
    """One email, ready to go."""
    workspace_id: int
    message_id: int
    to: str
    from_name: str
    from_email: str
    subject: str
    html: str
    text: str
    send_token: str
    headers: dict = field(default_factory=dict)
    attempt: int = 1  # which try this is (the practice server's "flaky" addresses use it)


@dataclass
class Accepted:
    provider_id: str
    duplicate: bool = False  # the mail service already had this send_token: it was NOT sent twice


@dataclass
class TryLater:
    reason: str
    retry_after: float | None = None  # seconds, when the service says how long to wait
    status: int | None = None  # the HTTP status, if there was one (429 slows our sending down)


@dataclass
class Rejected:
    reason: str
    permanent_bounce: bool = False  # True: the address goes on the do-not-send list


class MailerTimeout(Exception):
    """The mail service didn't answer in time. We don't know if it took the email."""


PRACTICE, BREVO, GMAIL = "practice", "brevo", "gmail"
LABELS = {PRACTICE: "The practice mail server", BREVO: "Brevo (real email)", GMAIL: "Gmail (real email)"}
# How a page names the mailer an email went through ("Sent (Gmail)").
SHORT_NAMES = {PRACTICE: "the practice mail server", BREVO: "Brevo", GMAIL: "Gmail"}


def ready(config, mailer):
    """Are the server's settings there for this real mailer?"""
    from . import gmail
    if mailer == BREVO:
        return bool(config.get("BREVO_API_KEY"))
    if mailer == GMAIL:
        return gmail.ready(config)
    return mailer == PRACTICE


def for_workspace(workspace, app=None):
    from flask import current_app

    from . import practice_server
    config = (app or current_app).config
    chosen = getattr(workspace, "mailer", PRACTICE)
    if workspace.is_demo or not ready(config, chosen):
        return practice_server.PracticeMailer()
    if chosen == BREVO:
        from .brevo import BrevoMailer
        return BrevoMailer(config["BREVO_API_KEY"])
    if chosen == GMAIL:
        from . import gmail
        return gmail.for_config(config)
    return practice_server.PracticeMailer()
