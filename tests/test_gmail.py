"""Sending from the owner's own Gmail (gmail.py): the email it builds, the access token it
buys and keeps, Gmail's answers turned into ours, the Settings choice, and the sign-in."""
import base64
import email
import email.policy
import http.server
import io
import json
import threading
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta

import pytest
from sqlalchemy import select

from oncue import db, gmail, mailers, worker
from oncue.gmail import GmailMailer
from oncue.models import Job, Workspace

from .conftest import post, text
from .test_limits import FakeResponse, message_for, outgoing, setup

T0 = datetime(2026, 11, 16, 1, 0)
SETTINGS = {"GMAIL_CLIENT_ID": "id-1", "GMAIL_CLIENT_SECRET": "secret-1", "GMAIL_REFRESH_TOKEN": "refresh-1"}


@pytest.fixture(autouse=True)
def fresh_tokens():
    gmail._tokens.clear()
    yield
    gmail._tokens.clear()


def google_error(code, reason="", message="nope", headers=None):
    body = json.dumps({"error": {"code": code, "message": message, "errors": [{"reason": reason}]}}).encode()
    return urllib.error.HTTPError("https://gmail.googleapis.com", code, "error", headers or {}, io.BytesIO(body))


class Google:
    """A pretend Google: hands out access tokens and takes emails (or answers with an error)."""

    def __init__(self, send_error=None, token_error=None):
        self.token_calls, self.sent = [], []
        self.send_error, self.token_error = send_error, token_error

    def __call__(self, request, timeout):
        if request.full_url == gmail.TOKEN_URL:
            self.token_calls.append(urllib.parse.parse_qs(request.data.decode()))
            if self.token_error:
                raise self.token_error
            return FakeResponse(json.dumps({"access_token": f"access-{len(self.token_calls)}",
                                            "expires_in": 3600}).encode())
        assert request.full_url == gmail.SEND_URL
        if self.send_error:
            raise self.send_error
        self.sent.append((request.headers["Authorization"], json.loads(request.data)["raw"]))
        return FakeResponse(b'{"id": "18c0ffee", "threadId": "18c0ffee"}')


def mailer(google, clock=lambda: 1000.0):
    return GmailMailer("id-1", "secret-1", "refresh-1", opener=google, clock=clock)


def test_the_email_gmail_gets():
    raw = gmail.build_email(outgoing())
    parsed = email.message_from_bytes(base64.urlsafe_b64decode(raw), policy=email.policy.default)
    assert parsed["From"] == "Teatro <hi@teatro.example>" and parsed["To"] == "ana@example.com"
    assert parsed["Subject"] == "Hi" and parsed["List-Unsubscribe"] == "<https://x/u>"
    assert "oncue-9" in parsed["Message-ID"]
    assert parsed.get_body(("plain",)).get_content().strip() == "Hi"
    assert parsed.get_body(("html",)).get_content().strip() == "<p>Hi</p>"


def test_the_access_token_is_bought_once_and_again_when_it_runs_out():
    google, now = Google(), [1000.0]
    sender = mailer(google, clock=lambda: now[0])
    assert sender.send(None, outgoing(), T0) == mailers.Accepted("18c0ffee")
    sender.send(None, outgoing(), T0)
    assert len(google.token_calls) == 1 and google.sent[1][0] == "Bearer access-1"
    assert google.token_calls[0]["grant_type"] == ["refresh_token"]
    assert google.token_calls[0]["refresh_token"] == ["refresh-1"]
    now[0] += 3600 - 30  # less than a minute left: buy a new one first
    sender.send(None, outgoing(), T0)
    assert len(google.token_calls) == 2 and google.sent[2][0] == "Bearer access-2"


def test_gmails_answers_become_our_answers():
    def answer(error):
        return mailer(Google(send_error=error)).send(None, outgoing(), T0)

    assert isinstance(answer(google_error(400, "invalidArgument", "Invalid To header")), mailers.Rejected)
    busy = answer(google_error(403, "userRateLimitExceeded"))
    assert isinstance(busy, mailers.TryLater) and busy.status == 429  # we slow down
    assert answer(google_error(429, "rateLimitExceeded", headers={"Retry-After": "20"})).retry_after == 20
    assert answer(google_error(403, "insufficientPermissions")).status == 403
    assert isinstance(answer(google_error(503, "backendError")), mailers.TryLater)
    expired = answer(google_error(401, "authError"))
    assert expired.status == 401 and "refresh-1" not in gmail._tokens  # bought again next time

    def silent(request, timeout):
        if request.full_url == gmail.TOKEN_URL:
            return FakeResponse(b'{"access_token": "a", "expires_in": 3600}')
        raise TimeoutError("timed out")
    with pytest.raises(mailers.MailerTimeout):
        mailer(silent).send(None, outgoing(), T0)


def test_a_refused_sign_in_says_what_to_do():
    refused = urllib.error.HTTPError(gmail.TOKEN_URL, 400, "error", {}, io.BytesIO(
        b'{"error": "invalid_grant", "error_description": "Token has been expired or revoked."}'))
    result = mailer(Google(token_error=refused)).send(None, outgoing(), T0)
    assert isinstance(result, mailers.TryLater) and result.status == 401
    assert "invalid_grant" in result.reason and "gmail-signin" in result.reason


def test_which_mailer_a_workspace_gets(app):
    owner = Workspace(name="Teatro", mailer="gmail")
    assert mailers.for_workspace(owner, app).name == "practice"  # not signed in yet
    app.config.update(SETTINGS)
    assert mailers.for_workspace(owner, app).name == "gmail"
    assert mailers.for_workspace(Workspace(name="Demo", is_demo=True, mailer="gmail"), app).name == "practice"


def test_a_gmail_timeout_makes_the_email_unsure_not_double(app, ctx, monkeypatch):
    workspace, show = setup(ctx, ["ana@example.com"], mailer="gmail")
    app.config.update(SETTINGS)

    def silent(self, s, outgoing, now):
        raise mailers.MailerTimeout("no answer")

    monkeypatch.setattr(GmailMailer, "send", silent)
    worker.simulate(ctx, T0, T0 + timedelta(hours=1))
    message = message_for(ctx, "ana@example.com")
    assert message.status == "unsure" and message.mailer == "gmail"
    assert ctx.scalar(select(Job.status).where(Job.kind == "send_message")) == "done"


def test_choosing_gmail_in_settings(app, owner):
    client, workspace_id = owner
    form = {"action": "sending", "send_rate": "1", "daily_cap": "400", "mailer": "gmail"}
    refused = post(client, "/settings", form)
    assert refused.status_code == 400 and "GMAIL_REFRESH_TOKEN" in text(refused)
    page = text(client.get("/settings"))
    assert 'value="gmail" disabled' in page and "Sign in to Gmail first" in page
    app.config.update(SETTINGS)
    post(client, "/settings", form)
    with app.app_context():
        assert db.session().get(Workspace, workspace_id).mailer == "gmail"
    page = text(client.get("/settings"))
    assert 'value="gmail" checked' in page and "keep Emails a day at 400" in page


def test_the_sign_in_asks_for_send_only_and_a_refresh_token():
    address = urllib.parse.urlparse(gmail.sign_in_address("id-1", "http://127.0.0.1:5555/", "st", "ch"))
    query = urllib.parse.parse_qs(address.query)
    assert query["scope"] == ["https://www.googleapis.com/auth/gmail.send"]
    assert query["access_type"] == ["offline"] and query["prompt"] == ["consent"]
    assert query["code_challenge_method"] == ["S256"] and query["state"] == ["st"]


def test_the_sign_in_catches_googles_answer_on_this_pc(app):
    server = http.server.HTTPServer(("127.0.0.1", 0), http.server.BaseHTTPRequestHandler)
    port, caught = server.server_port, {}
    thread = threading.Thread(target=lambda: caught.update(gmail.wait_for_google(server, "st", seconds=10)))
    thread.start()
    with pytest.raises(urllib.error.HTTPError):  # someone else's answer (wrong state) is turned away
        urllib.request.urlopen(f"http://127.0.0.1:{port}/?code=evil&state=other", timeout=5)
    page = urllib.request.urlopen(f"http://127.0.0.1:{port}/?code=good&state=st", timeout=5).read().decode()
    thread.join(10)
    server.server_close()
    assert caught["code"] == "good" and "close this tab" in page
    assert "gmail-signin" in app.cli.commands
