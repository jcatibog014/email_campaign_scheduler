"""The practice mail server: a pretend mail service that lives in this app.

It behaves like a real one, so the crew has something to deal with: it keeps
every email it takes in its own inbox (Backstage shows them), and it honours
an Idempotency-Key: the same send_token sent twice is taken ONCE, and the
second answer says so. That is what makes a retry after a crash safe.

Stage 5 teaches it to be difficult (slow, busy, bounces) on purpose.
"""
import secrets

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from .mailers import PRACTICE, Accepted
from .models import PracticeMail


class PracticeMailer:
    name = PRACTICE
    idempotent = True  # see the module comment

    def send(self, s, outgoing, now):
        existing = s.scalar(select(PracticeMail).where(PracticeMail.idempotency_key == outgoing.send_token))
        if existing is not None:
            return Accepted(existing.provider_id, duplicate=True)
        mail = PracticeMail(workspace_id=outgoing.workspace_id, idempotency_key=outgoing.send_token,
                            provider_id=f"<practice-{secrets.token_hex(8)}@oncue.invalid>", to=outgoing.to,
                            subject=outgoing.subject[:300], html=outgoing.html, text=outgoing.text,
                            headers=dict(outgoing.headers), accepted_at=now)
        try:
            with s.begin_nested():
                s.add(mail)
                s.flush()
        except IntegrityError:  # another worker sent the same token a moment ago
            existing = s.scalar(select(PracticeMail).where(PracticeMail.idempotency_key == outgoing.send_token))
            return Accepted(existing.provider_id, duplicate=True)
        return Accepted(mail.provider_id)
