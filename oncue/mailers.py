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
- the owner uses Brevo once BREVO_API_KEY is set and Settings say so,
  otherwise the practice mail server too.
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


PRACTICE, BREVO = "practice", "brevo"
LABELS = {PRACTICE: "The practice mail server", BREVO: "Brevo (real email)"}


def for_workspace(workspace, app=None):
    from flask import current_app

    from . import practice_server
    config = (app or current_app).config
    if not workspace.is_demo and getattr(workspace, "mailer", PRACTICE) == BREVO and config.get("BREVO_API_KEY"):
        from .brevo import BrevoMailer
        return BrevoMailer(config["BREVO_API_KEY"])
    return practice_server.PracticeMailer()
