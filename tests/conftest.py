import html
import os
import re

import pytest
from sqlalchemy import select

from oncue import auth, create_app, db
from oncue.models import User, Workspace

# Tests use a fresh SQLite file per test. To check everything on PostgreSQL
# too, set TEST_DATABASE_URL to an EMPTY Neon branch (its tables are dropped
# and made again for every test, so never point it at the live database).
TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")

CSRF = "test-csrf-token"
PASSWORD = "correct horse"


@pytest.fixture(autouse=True)
def fast_passwords(monkeypatch):
    """The real password hash is slow on purpose (to slow down guessing). Tests make
    many users, so they use a quick setting of the same kind of hash."""
    from functools import partial

    from werkzeug.security import generate_password_hash
    monkeypatch.setattr(auth, "generate_password_hash", partial(generate_password_hash, method="pbkdf2:sha256:1000"))


@pytest.fixture
def app(tmp_path):
    app = create_app({
        "TESTING": True,
        "SECRET_KEY": "test",
        "DATABASE": str(tmp_path / "test.sqlite"),
        "DATABASE_URL": TEST_DATABASE_URL,
        "SERVER_NAME": "localhost",
        "BASE_URL": "http://localhost",
        "RUN_WORKER": False,
        "PRACTICE_REAL_DELAYS": False,
    })
    with app.app_context():
        if TEST_DATABASE_URL:
            db.drop_tables()
        db.create_tables()
    return app


@pytest.fixture
def ctx(app):
    """An app context, for tests that use db.session() directly."""
    with app.app_context():
        yield db.session()


@pytest.fixture
def client(app):
    client = app.test_client()
    # Every POST needs the session's CSRF token. Put a known one in the session.
    with client.session_transaction() as session:
        session["csrf"] = CSRF
    return client


def post(client, url, data=None, **kwargs):
    """POST a form with the right CSRF token, like a real page would."""
    # Logging in or out starts a new session (and a new token), so set it every time.
    with client.session_transaction() as session:
        session["csrf"] = CSRF
    return client.post(url, data={"csrf_token": CSRF, **(data or {})}, **kwargs)


# The practice mail server's dice are seeded by a random send_token, so tests start with every
# failure rate at 0 (deterministic); a test that wants trouble asks for it.
CALM = {"practice_busy": 0, "practice_timeout": 0, "practice_bounce": 0, "practice_slow": 0}


def make_owner(app, name="Maria Santos", email="maria@example.com", **workspace_fields):
    """The owner and their workspace, straight into the database. Returns (user id, workspace id)."""
    workspace_fields = {**CALM, **workspace_fields}
    with app.app_context():
        s = db.session()
        workspace = Workspace(name="Maria's Shows", sender_name=name, sender_email=email, **workspace_fields)
        s.add(workspace)
        user = auth.create_user(s, workspace, name, email, PASSWORD)
        s.commit()
        return user.id, workspace.id


def log_in_as(client, user_id):
    """Skip the login form: put the user straight into the session."""
    with client.application.app_context():
        version = db.session().get(User, user_id).session_version
    with client.session_transaction() as session:
        session["user_id"] = user_id
        session["session_version"] = version
        session["csrf"] = CSRF
    return client


@pytest.fixture
def owner(app, client):
    """A logged-in owner. Returns (client, workspace id)."""
    user_id, workspace_id = make_owner(app)
    log_in_as(client, user_id)
    return client, workspace_id


def browser(app, user_id=None):
    """A fresh client, logged in as this user if given."""
    client = app.test_client()
    with client.session_transaction() as session:
        session["csrf"] = CSRF
    return log_in_as(client, user_id) if user_id else client


def is_logged_in(client):
    client.get("/settings")
    with client.session_transaction() as session:
        return "user_id" in session


def get_user(app, email):
    with app.app_context():
        return db.session().scalar(select(User).where(User.email == email))


def text(response):
    """A page's words the way a person reads them (&#39; back to ', &amp; to &)."""
    return html.unescape(response.get_data(as_text=True))


def words(response):
    """A page as plain words: no tags, single spaces (for sentences split by <strong> and such)."""
    return " ".join(re.sub(r"<[^>]+>", " ", text(response)).split())
