"""Gmail: the owner's real emails, sent from their own Gmail account.

Render's free plan blocks the email ports (SMTP), so this uses the Gmail API,
which is plain HTTPS like Brevo's:

1. Once, from the owner's PC: `flask --app oncue gmail-signin` (below) signs in
   to Google and prints a REFRESH TOKEN, kept on the server as GMAIL_REFRESH_TOKEN
   with GMAIL_CLIENT_ID and GMAIL_CLIENT_SECRET (docs/DEPLOY.md, part 4B).
2. Before sending, the refresh token buys an ACCESS TOKEN (good for about an
   hour; kept in memory and bought again a minute before it runs out).
3. Each email: POST https://gmail.googleapis.com/gmail/v1/users/me/messages/send
   with the whole email (MIME), base64url-encoded, as "raw".

The scope is gmail.send only: the app can send as the owner, never read their mail.

How Gmail's answers map to ours:
- 200 with an id                          -> Accepted
- 400 (a bad address or email)            -> Rejected: it will never work
- 401 (the access token ran out)          -> TryLater; the token is bought again
- 403 / 429 "rate limit" or "daily limit" -> TryLater with status 429 (we slow down)
- other 403 (no permission)               -> TryLater; the Safety Curtain falls after 10
- 5xx, a dropped connection               -> TryLater
- the refresh token refused (invalid_grant)-> TryLater: run gmail-signin again
- no answer in time                       -> MailerTimeout. Gmail has no idempotency key,
                                             so the email becomes Unsure (sending.py).

Gmail doesn't report deliveries or bounces to apps (a bounce comes back as an
email in the owner's inbox), so a Gmail email stays Sent; opens and clicks
still come from our own picture and links (tracking.py). A personal Gmail
sends about 500 emails a day: keep the daily cap at 400.
"""
import base64
import hashlib
import http.server
import json
import os
import secrets
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from email.message import EmailMessage
from email.utils import formataddr, make_msgid

import click

from .mailers import GMAIL, Accepted, MailerTimeout, Rejected, TryLater

TOKEN_URL = "https://oauth2.googleapis.com/token"
SEND_URL = "https://gmail.googleapis.com/gmail/v1/users/me/messages/send"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
SCOPE = "https://www.googleapis.com/auth/gmail.send"
TIMEOUT_SECONDS = 15
RATE_REASONS = ("ratelimitexceeded", "userratelimitexceeded", "dailylimitexceeded", "quotaexceeded")

_tokens = {}  # refresh token -> (access token, expires at, monotonic seconds)
_lock = threading.Lock()


class SignInRefused(Exception):
    """Google wouldn't swap the refresh token for an access token."""


def build_email(outgoing):
    """The whole email, as Gmail wants it: text and HTML parts, our headers, base64url."""
    email = EmailMessage()
    email["From"] = formataddr((outgoing.from_name, outgoing.from_email))
    email["To"] = outgoing.to
    email["Subject"] = outgoing.subject
    email["Message-ID"] = make_msgid(f"oncue-{outgoing.message_id}")
    for name, value in outgoing.headers.items():  # List-Unsubscribe and List-Unsubscribe-Post
        email[name] = value
    email.set_content(outgoing.text)
    email.add_alternative(outgoing.html, subtype="html")
    return base64.urlsafe_b64encode(email.as_bytes()).decode()


def _reason(error):
    """Google's error, as (lowercase reason, message)."""
    try:
        body = json.loads(error.read() or b"{}")
    except (ValueError, OSError):
        return "", str(error.reason)
    if isinstance(body.get("error"), str):  # the token endpoint: {"error": "invalid_grant", ...}
        return body["error"].lower(), body.get("error_description", body["error"])
    detail = body.get("error") or {}
    reasons = [item.get("reason", "") for item in detail.get("errors", [])]
    return (reasons[0] if reasons else detail.get("status", "")).lower(), detail.get("message", "")


class GmailMailer:
    name = GMAIL
    idempotent = False

    def __init__(self, client_id, client_secret, refresh_token, opener=None, clock=time.monotonic):
        self.client_id, self.client_secret, self.refresh_token = client_id, client_secret, refresh_token
        self.opener = opener or urllib.request.urlopen  # tests pass a pretend one
        self.clock = clock

    def access_token(self):
        with _lock:
            token, expires = _tokens.get(self.refresh_token, (None, 0))
            if token and self.clock() < expires - 60:
                return token
            form = urllib.parse.urlencode({"client_id": self.client_id, "client_secret": self.client_secret,
                                           "refresh_token": self.refresh_token,
                                           "grant_type": "refresh_token"}).encode()
            request = urllib.request.Request(TOKEN_URL, data=form, method="POST",
                                             headers={"content-type": "application/x-www-form-urlencoded"})
            try:
                with self.opener(request, timeout=TIMEOUT_SECONDS) as response:
                    body = json.loads(response.read() or b"{}")
            except urllib.error.HTTPError as error:
                reason, message = _reason(error)
                raise SignInRefused(f"{reason or error.code}: {message}") from error
            _tokens[self.refresh_token] = (body["access_token"], self.clock() + int(body.get("expires_in", 3600)))
            return body["access_token"]

    def forget_token(self):
        with _lock:
            _tokens.pop(self.refresh_token, None)

    def send(self, s, outgoing, now):
        try:
            token = self.access_token()
        except SignInRefused as error:
            return TryLater(f"Google refused the Gmail sign-in ({error}). Run `flask --app oncue gmail-signin` "
                            "again and update GMAIL_REFRESH_TOKEN.", status=401)
        except (TimeoutError, socket.timeout, urllib.error.URLError) as error:
            return TryLater(f"Couldn't reach Google to sign in: {getattr(error, 'reason', error)}")
        request = urllib.request.Request(SEND_URL, data=json.dumps({"raw": build_email(outgoing)}).encode(),
                                         method="POST", headers={"authorization": f"Bearer {token}",
                                                                 "content-type": "application/json"})
        try:
            with self.opener(request, timeout=TIMEOUT_SECONDS) as response:
                body = json.loads(response.read() or b"{}")
                return Accepted(body.get("id") or "")
        except urllib.error.HTTPError as error:
            reason, message = _reason(error)
            if error.code == 400:
                return Rejected(f"Gmail refused it: {message or reason}")
            if error.code == 401:
                self.forget_token()  # bought again on the next try
                return TryLater("Gmail's access token ran out; trying again with a new one.", status=401)
            if error.code == 429 or (error.code == 403 and reason in RATE_REASONS):
                wait = error.headers.get("Retry-After") if error.headers else None
                return TryLater(f"Gmail says too many: {message or reason}",
                                float(wait) if wait and str(wait).isdigit() else None, status=429)
            if error.code == 403:
                return TryLater(f"Gmail refused to send ({reason or 403}): {message}", status=403)
            return TryLater(f"Gmail answered {error.code}: {message or reason}", status=error.code)
        except (TimeoutError, socket.timeout) as error:
            raise MailerTimeout(str(error) or "Gmail didn't answer in time") from error
        except urllib.error.URLError as error:
            if isinstance(error.reason, (TimeoutError, socket.timeout)):
                raise MailerTimeout(str(error.reason)) from error
            return TryLater(f"Couldn't reach Gmail: {error.reason}")


def ready(config):
    """All three Gmail settings are on the server."""
    return all(config.get(name) for name in ("GMAIL_CLIENT_ID", "GMAIL_CLIENT_SECRET", "GMAIL_REFRESH_TOKEN"))


def for_config(config):
    return GmailMailer(config["GMAIL_CLIENT_ID"], config["GMAIL_CLIENT_SECRET"], config["GMAIL_REFRESH_TOKEN"])


# ---------- The one-time sign-in, from the owner's PC ----------

def sign_in_address(client_id, redirect_uri, state, challenge):
    return AUTH_URL + "?" + urllib.parse.urlencode({
        "client_id": client_id, "redirect_uri": redirect_uri, "response_type": "code", "scope": SCOPE,
        "access_type": "offline",  # ask for a refresh token...
        "prompt": "consent",  # ...every time, even if this Google account said yes before
        "state": state, "code_challenge": challenge, "code_challenge_method": "S256"})


def wait_for_google(server, state, seconds=300):
    """Google sends the browser back to http://127.0.0.1:<port>/?code=...&state=...; catch it once."""
    answer = {}

    class Catch(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 (the name http.server wants)
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if query.get("state", [""])[0] != state:
                self.send_response(400)
                self.end_headers()
                return
            answer["code"] = query.get("code", [""])[0]
            answer["error"] = query.get("error", [""])[0]
            self.send_response(200)
            self.send_header("content-type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write("Done. You can close this tab and go back to the terminal.".encode())

        def log_message(self, *args):  # keep the terminal quiet
            pass

    server.RequestHandlerClass = Catch
    server.timeout = 5
    deadline = time.monotonic() + seconds
    while "code" not in answer and time.monotonic() < deadline:
        server.handle_request()
    return answer


@click.command("gmail-signin")
def gmail_signin_command():
    """Sign in to Gmail once and print the refresh token for the server (docs/DEPLOY.md, part 4B)."""
    client_id = os.environ.get("GMAIL_CLIENT_ID") or click.prompt("Client ID (from Google Cloud)").strip()
    client_secret = (os.environ.get("GMAIL_CLIENT_SECRET")
                     or click.prompt("Client secret (it won't show as you type)", hide_input=True).strip())
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    state = secrets.token_urlsafe(16)
    server = http.server.HTTPServer(("127.0.0.1", 0), http.server.BaseHTTPRequestHandler)
    redirect_uri = f"http://127.0.0.1:{server.server_port}/"
    address = sign_in_address(client_id, redirect_uri, state, challenge)
    click.echo("Your browser opens Google's sign-in. Choose the Gmail account to send from.")
    click.echo("If it says \"Google hasn't verified this app\": Advanced > Go to ... (it's your own app).")
    click.echo(f"If no browser opens, copy this address into one:\n{address}\n")
    webbrowser.open(address)
    try:
        answer = wait_for_google(server, state)
    finally:
        server.server_close()
    if not answer.get("code"):
        raise click.ClickException(f"No sign-in came back ({answer.get('error') or 'it took more than 5 minutes'}).")
    form = urllib.parse.urlencode({"code": answer["code"], "client_id": client_id, "client_secret": client_secret,
                                   "redirect_uri": redirect_uri, "grant_type": "authorization_code",
                                   "code_verifier": verifier}).encode()
    request = urllib.request.Request(TOKEN_URL, data=form, method="POST",
                                     headers={"content-type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            body = json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as error:
        reason, message = _reason(error)
        raise click.ClickException(f"Google refused the sign-in: {reason} {message}") from error
    if not body.get("refresh_token"):
        raise click.ClickException("Google gave no refresh token. Run this again (it asks for consent each time).")
    click.echo("\nSigned in. Put this on the server as GMAIL_REFRESH_TOKEN (Render > Environment).")
    click.echo("Keep it secret, like a password: don't paste it in chats or commit it.\n")
    click.echo(body["refresh_token"])


def init_app(app):
    app.cli.add_command(gmail_signin_command)