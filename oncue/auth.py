"""Logging in and out, and the protection every form gets.

Right On Cue has no sign-up: sending real email to people's inboxes can be
misused for spam, so only the OWNER (made from the PC with
`flask --app oncue create-owner`) has a real workspace. Everyone else uses
Try Demo, a sandbox workspace that only ever sends to the practice mail server.

Passwords are stored only as hashes. The login cookie holds the user's id and
their session_version; load_user() checks both on every request, so an old
cookie (after a password change) stops working straight away.
"""
import functools
import hmac
import re
import secrets
from datetime import timedelta

import click
from flask import Blueprint, abort, current_app, flash, g, redirect, render_template, request, session, url_for
from flask.cli import with_appcontext
from sqlalchemy import select
from werkzeug.security import check_password_hash, generate_password_hash

from . import db, limits
from .dates import utcnow
from .models import User, Workspace

bp = Blueprint("auth", __name__)

NAME_MAX_LENGTH = 80
EMAIL_MAX_LENGTH = 254
PASSWORD_MIN_LENGTH = 8
EMAIL_PATTERN = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")
# After 5 wrong passwords in a row, the account waits 15 minutes.
MAX_FAILED_LOGINS = 5
LOCK_TIME = timedelta(minutes=15)
# And one address may try at most this many times in 15 minutes, whatever the account.
LOGINS_PER_IP = 30

WRONG_LOGIN = "Wrong email or password."
LOCKED = "Too many failed attempts. Please wait 15 minutes and try again."
TOO_MANY = "Too many tries from your network. Please wait 15 minutes and try again."


# ---------- Checking what people type ----------

def clean_email(text):
    return (text or "").strip().lower()[:EMAIL_MAX_LENGTH + 1]


def email_problem(email):
    if not email:
        return "Enter an email address."
    if len(email) > EMAIL_MAX_LENGTH or not EMAIL_PATTERN.fullmatch(email):
        return "That doesn't look like an email address."
    return None


def name_problem(name, what="your name"):
    if not name:
        return f"Enter {what}."
    if len(name) > NAME_MAX_LENGTH:
        return f"Use at most {NAME_MAX_LENGTH} characters."
    return None


def password_problem(password, confirm):
    if len(password) < PASSWORD_MIN_LENGTH:
        return f"Use at least {PASSWORD_MIN_LENGTH} characters."
    if password != confirm:
        return "The two passwords are not the same."
    return None


# ---------- Users in the database ----------

def find_user(s, email):
    return s.scalar(select(User).where(User.email == clean_email(email)))


def create_user(s, workspace, name, email, password, **fields):
    """Add a user to a workspace. The caller commits."""
    user = User(workspace=workspace, name=name, email=clean_email(email),
                password_hash=generate_password_hash(password), **fields)
    s.add(user)
    s.flush()
    return user


def owner(s):
    """The one real (not demo) user, or None before create-owner has run."""
    return s.scalar(select(User).where(User.is_demo.is_(False)).order_by(User.id).limit(1))


def set_password(user, password):
    user.password_hash = generate_password_hash(password)
    user.failed_logins = 0
    user.locked_until = None
    user.session_version += 1  # every other device is logged out


def _failed(s, user, moment):
    user.failed_logins += 1
    if user.failed_logins >= MAX_FAILED_LOGINS:
        user.failed_logins, user.locked_until = 0, moment + LOCK_TIME
    s.commit()


def check_login(s, email, password, moment):
    """Return (user, None) if the password is right, else (None, message).
    The message never says whether the email has an account."""
    if not limits.hit(s, f"login-ip:{limits.client_ip()}", LOGINS_PER_IP, 900, moment):
        s.commit()
        return None, TOO_MANY
    user = find_user(s, email)
    if user is None or user.is_demo:
        # Same work as a real check, so the answer takes as long either way.
        check_password_hash(_dummy_hash(), password or "")
        s.commit()
        return None, WRONG_LOGIN
    if user.locked_until and user.locked_until > moment:
        s.commit()
        return None, LOCKED
    if not check_password_hash(user.password_hash, password or ""):
        _failed(s, user, moment)
        return None, WRONG_LOGIN
    user.failed_logins, user.locked_until = 0, None
    s.commit()
    return user, None


@functools.cache
def _dummy_hash():
    return generate_password_hash(secrets.token_urlsafe(16))


def safe_next(target, default=None):
    """Only go back to a page on this site after logging in.

    "/shows/7" is fine. "https://evil.example" and "//evil.example" are not:
    a fake link could send people there after a real login. Browsers drop tabs
    and newlines and read a backslash as a slash, so those are out too.
    """
    if (target and target.startswith("/") and not target.startswith("//")
            and "\\" not in target and not any(ord(c) < 32 for c in target)):
        return target
    return default or url_for("stage.home")


def log_in(user):
    csrf = session.get("csrf")
    session.clear()  # a new session for the new user (old cookies can't be reused)
    if csrf and user.is_demo:
        session["csrf"] = csrf  # demo pages that are open keep working
    session.permanent = True  # stay logged in for PERMANENT_SESSION_LIFETIME
    session["user_id"] = user.id
    session["session_version"] = user.session_version


# ---------- Every request ----------

@bp.before_app_request
def load_user():
    g.user = g.workspace = None
    user_id = session.get("user_id")
    if user_id is not None:
        s = db.session()
        user = s.get(User, user_id)
        if (user is None or session.get("session_version") != user.session_version
                or (user.is_demo and (user.workspace.demo_expires_at or utcnow()) <= utcnow())):
            # Keep the CSRF token: only the login part of this cookie is out of date.
            session.pop("user_id", None)
            session.pop("session_version", None)
        else:
            g.user, g.workspace = user, user.workspace


def csrf_token():
    """A secret per session, put into every form as a hidden field.

    Another website could make your browser send a form to this site (your
    login cookie goes with it). It can't read this secret, so its form fails.
    """
    if "csrf" not in session:
        session["csrf"] = secrets.token_urlsafe(32)
    return session["csrf"]


@bp.before_app_request
def check_csrf():
    if request.method not in ("POST", "DELETE") or request.endpoint in current_app.config.get("CSRF_EXEMPT", ()):
        return
    sent = request.form.get("csrf_token", "") or request.headers.get("X-CSRF-Token", "")
    expected = session.get("csrf", "")
    # compare_digest takes the same time for every wrong guess.
    if not expected or not hmac.compare_digest(sent, expected):
        abort(400, "This form has expired. Please go back, reload the page and try again.")


def login_required(view):
    @functools.wraps(view)
    def wrapped(*args, **kwargs):
        if g.user is None:
            # Only a GET can be repeated after logging in.
            next_page = request.full_path.rstrip("?") if request.method == "GET" else None
            return redirect(url_for("auth.login", next=next_page))
        return view(*args, **kwargs)
    return wrapped


def not_for_demo(message="The demo can't change this."):
    if g.user is not None and g.user.is_demo:
        abort(403, message)


# ---------- Pages ----------

@bp.get("/")
def index():
    return redirect(url_for("stage.home") if g.user else url_for("auth.login"))


@bp.route("/login", methods=["GET", "POST"])
def login():
    if g.user is not None:
        return redirect(url_for("stage.home"))
    next_page = request.values.get("next", "")
    error = None
    email = ""
    if request.method == "POST":
        s = db.session()
        email = clean_email(request.form.get("email", ""))
        user, error = check_login(s, email, request.form.get("password", ""), utcnow())
        if user:
            log_in(user)
            return redirect(safe_next(next_page))
    status = 401 if error else 200
    return render_template("login.html", error=error, email=email, next=next_page,
                           has_owner=owner(db.session()) is not None), status


@bp.post("/logout")
def logout():
    session.clear()
    flash("You have logged out.")
    return redirect(url_for("auth.login"))


# ---------- Command line ----------

@click.command("create-owner")
@with_appcontext
@click.option("--name", prompt="Your name")
@click.option("--email", prompt="Your email (you log in with it)")
def create_owner_command(name, email):
    """Make the owner's login and workspace (once, from your PC)."""
    s = db.session()
    if owner(s) is not None:
        raise click.ClickException("There is already an owner. Use set-password to change the password.")
    email = clean_email(email)
    problem = name_problem(name.strip()) or email_problem(email)
    if problem:
        raise click.ClickException(problem)
    password = click.prompt("Password (at least 8 characters)", hide_input=True, confirmation_prompt=True)
    if len(password) < PASSWORD_MIN_LENGTH:
        raise click.ClickException(f"Use at least {PASSWORD_MIN_LENGTH} characters.")
    workspace = Workspace(name=f"{name.strip()}'s Shows", sender_name=name.strip(), sender_email=email)
    s.add(workspace)
    create_user(s, workspace, name.strip(), email, password)
    s.commit()
    click.echo(f"Done. Log in with {email}.")


@click.command("set-password")
@with_appcontext
@click.argument("email")
def set_password_command(email):
    """Change someone's password (and unlock the account). Also ends their sessions."""
    s = db.session()
    user = find_user(s, email)
    if user is None or user.is_demo:
        raise click.ClickException(f"There is no account for {email}.")
    password = click.prompt("New password", hide_input=True, confirmation_prompt=True)
    if len(password) < PASSWORD_MIN_LENGTH:
        raise click.ClickException(f"Use at least {PASSWORD_MIN_LENGTH} characters.")
    set_password(user, password)
    s.commit()
    click.echo("The password is changed.")


def init_app(app):
    app.jinja_env.globals["csrf_token"] = csrf_token
    app.cli.add_command(create_owner_command)
    app.cli.add_command(set_password_command)
