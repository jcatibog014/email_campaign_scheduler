"""Brevo: the real mail service for the owner's workspace.

Brevo's transactional email API takes one email per call:
POST https://api.brevo.com/v3/smtp/email with the api-key header.
Render's free plan blocks the usual email ports (SMTP), but an HTTPS call
like this one goes through. The free plan takes 300 emails a day.

How its answers map to ours:
- 201 Created with a messageId          -> Accepted (delivery news comes later, hooks.py)
- 400 Bad Request (a bad address...)    -> Rejected: it will never work
- 429 Too Many Requests                 -> TryLater, with its Retry-After (and we slow down)
- 401/403 (a wrong or blocked key)      -> TryLater: nothing will work until the key is fixed;
                                           the Safety Curtain comes down after 10 of these
- 5xx, a dropped connection             -> TryLater
- no answer in time                     -> MailerTimeout. Brevo has no idempotency key, so
                                           we can't ask "did you take it?": the sender marks
                                           the email Unsure instead of risking a double.
"""
import json
import socket
import urllib.error
import urllib.request

from .mailers import BREVO, Accepted, MailerTimeout, Rejected, TryLater

URL = "https://api.brevo.com/v3/smtp/email"
TIMEOUT_SECONDS = 15


def retry_after(headers):
    for name in ("Retry-After", "x-sib-ratelimit-reset"):
        value = headers.get(name) if headers else None
        if value and str(value).strip().isdigit():
            return float(value)
    return None


class BrevoMailer:
    name = BREVO
    idempotent = False

    def __init__(self, api_key, opener=None):
        self.api_key = api_key
        self.opener = opener or urllib.request.urlopen  # tests pass a pretend one

    def payload(self, outgoing):
        return {
            "sender": {"name": outgoing.from_name, "email": outgoing.from_email},
            "to": [{"email": outgoing.to}],
            "subject": outgoing.subject,
            "htmlContent": outgoing.html,
            "textContent": outgoing.text,
            "headers": dict(outgoing.headers),
            # Comes back in every webhook for this email (hooks.py), with the message-id.
            "tags": [f"oncue-message-{outgoing.message_id}"],
        }

    def send(self, s, outgoing, now):
        request = urllib.request.Request(URL, data=json.dumps(self.payload(outgoing)).encode(), method="POST",
                                         headers={"api-key": self.api_key, "content-type": "application/json",
                                                  "accept": "application/json"})
        try:
            with self.opener(request, timeout=TIMEOUT_SECONDS) as response:
                body = json.loads(response.read() or b"{}")
                return Accepted(body.get("messageId") or "")
        except urllib.error.HTTPError as error:
            detail = _message(error)
            if error.code == 400:
                return Rejected(f"Brevo refused it: {detail}")
            if error.code == 429:
                return TryLater(f"Brevo says too many requests: {detail}", retry_after(error.headers), status=429)
            if error.code in (401, 403):
                return TryLater(f"Brevo refused the API key ({error.code}). Check BREVO_API_KEY.", status=error.code)
            return TryLater(f"Brevo answered {error.code}: {detail}", retry_after(error.headers), status=error.code)
        except (TimeoutError, socket.timeout) as error:
            raise MailerTimeout(str(error) or "Brevo didn't answer in time") from error
        except urllib.error.URLError as error:
            if isinstance(error.reason, (TimeoutError, socket.timeout)):
                raise MailerTimeout(str(error.reason)) from error
            return TryLater(f"Couldn't reach Brevo: {error.reason}")


def _message(error):
    try:
        return json.loads(error.read() or b"{}").get("message", "") or error.reason
    except (ValueError, OSError):
        return str(error.reason)
