"""The practice mail server: a pretend mail service that lives in this app.

It behaves like a real one, so the crew has something to deal with:

- it keeps every email it takes in its own inbox (Backstage shows them);
- it honours an idempotency key: the same send_token sent twice is taken
  ONCE, and the second answer says so. That makes a retry after a crash or a
  timeout safe;
- it is difficult on purpose, at the rates in Settings (percent of emails):
  busy (429 "too many requests", with Retry-After), a timeout (it may or may
  not have taken the email: it takes it half the time, which is exactly the
  case idempotency is for), slow (a second or so), and bounces;
- special addresses always do the same thing, whatever the rates:
    reject.*@...    refused at once: "550 the address doesn't exist" (Dropped, do-not-send)
    bounce.*@...    taken, then bounces a minute later (stage 6 tracks that)
    complain.*@...  taken, delivered, then marked as spam (stage 6)
    flaky.*@...     "503, try later" on the first two tries, then fine
    busy.*@...      always "429, too many": it ends as a dead letter
    slow.*@...      always slow
  The demo's audience has a few of these, so its Backstage has something in it.

The dice are seeded from the send_token and the try number, so a test (or the
demo) gets the same story every time.
"""
import random
import secrets
import time

from flask import current_app
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from .mailers import PRACTICE, Accepted, MailerTimeout, Rejected, TryLater
from .models import PracticeMail, Workspace

# Things that happen after an email was taken (stage 6 turns these into delivery news).
after_accept = []


def special(address):
    local = address.partition("@")[0].lower()
    for word in ("reject", "bounce", "complain", "flaky", "busy", "slow"):
        if local.startswith(word + "."):
            return word
    return None


class PracticeMailer:
    name = PRACTICE
    idempotent = True  # see the module comment

    def send(self, s, outgoing, now):
        existing = s.scalar(select(PracticeMail).where(PracticeMail.idempotency_key == outgoing.send_token))
        if existing is not None:
            return Accepted(existing.provider_id, duplicate=True)
        workspace = s.get(Workspace, outgoing.workspace_id)
        kind = special(outgoing.to)
        dice = random.Random(f"{outgoing.send_token}:{outgoing.attempt}")
        # One roll of a 100-sided die picks what happens to an ordinary address:
        # [0, busy) busy | [busy, +timeout) timeout | [.., +bounce) bounces later | the rest: fine.
        roll = dice.random() * 100
        busy_end = workspace.practice_busy
        timeout_end = busy_end + workspace.practice_timeout
        bounce_end = timeout_end + workspace.practice_bounce
        if kind == "reject":
            return Rejected("550 5.1.1 The address doesn't exist.", permanent_bounce=True)
        if kind == "busy" or (kind is None and roll < busy_end):
            return TryLater("429 Too many requests. Slow down.", retry_after=dice.choice((20, 30, 45)), status=429)
        if kind == "flaky" and outgoing.attempt <= 2:
            return TryLater("503 Service unavailable. Try again later.", status=503)
        if kind is None and roll < timeout_end:
            if dice.random() < 0.5:
                self._take(s, outgoing, now, "deliver")  # it took the email, but its answer never came back
            raise MailerTimeout("the practice server took more than 15 seconds")
        if kind in ("bounce", "complain"):
            fate = kind
        else:
            fate = "bounce" if kind is None and roll < bounce_end else "deliver"
        # A second roll: slow or not (it still works, just late).
        slow = kind == "slow" or dice.random() * 100 < workspace.practice_slow
        if slow and current_app.config.get("PRACTICE_REAL_DELAYS"):
            time.sleep(dice.uniform(0.4, 1.2))
        return self._take(s, outgoing, now, fate, slow=slow)

    def _take(self, s, outgoing, now, fate, slow=False):
        mail = PracticeMail(workspace_id=outgoing.workspace_id, idempotency_key=outgoing.send_token,
                            provider_id=f"<practice-{secrets.token_hex(8)}@oncue.invalid>", to=outgoing.to,
                            subject=outgoing.subject[:300], html=outgoing.html, text=outgoing.text,
                            headers=dict(outgoing.headers), fate=fate, accepted_at=now)
        try:
            with s.begin_nested():
                s.add(mail)
                s.flush()
        except IntegrityError:  # another worker sent the same token a moment ago
            existing = s.scalar(select(PracticeMail).where(PracticeMail.idempotency_key == outgoing.send_token))
            return Accepted(existing.provider_id, duplicate=True)
        for hook in after_accept:
            hook(s, mail, slow, now)
        return Accepted(mail.provider_id)
