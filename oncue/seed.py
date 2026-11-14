"""Try Demo: every visitor gets a sandbox workspace of their own.

The demo is "Teatro Luna", a made-up community theatre that emails its
audience. Its contacts have made-up addresses, and a demo workspace can only
send to the practice mail server (mailers.py), so nothing a demo does ever
reaches a real inbox. A demo is deleted 24 hours after it starts.
"""
import secrets
from datetime import timedelta

from sqlalchemy import delete, func, select

from . import auth
from .models import User, Workspace

DEMO_LIFETIME = timedelta(hours=24)
MAX_DEMOS = 100
DEMO_NAME = "Luna Reyes"


def count(s):
    return s.scalar(select(func.count()).select_from(Workspace).where(Workspace.is_demo.is_(True)))


def purge(s, moment):
    """Delete demos whose time is up. Everything inside goes with them (ON DELETE CASCADE)."""
    gone = s.scalars(select(Workspace.id).where(Workspace.is_demo.is_(True),
                                                Workspace.demo_expires_at <= moment)).all()
    if gone:
        s.execute(delete(Workspace).where(Workspace.id.in_(gone)))
    return len(gone)


def build(s, moment):
    """A new demo workspace with its sample content. Returns the demo user."""
    workspace = Workspace(name="Teatro Luna", is_demo=True, demo_expires_at=moment + DEMO_LIFETIME,
                          sender_name="Teatro Luna", sender_email="hello@teatro-luna.example",
                          postal_address="12 Mabini Street, Quezon City, Philippines", created_at=moment)
    s.add(workspace)
    s.flush()
    # A random session_version: even if an id were ever reused, an old cookie can't match.
    user = auth.create_user(s, workspace, DEMO_NAME, f"demo-{workspace.id}-{secrets.token_hex(4)}@demo.invalid",
                            secrets.token_urlsafe(16), is_demo=True,
                            session_version=secrets.randbelow(10**9) + 2, created_at=moment)
    return user


def init_app(app):
    pass


def demo_user(s, workspace_id):
    return s.scalar(select(User).where(User.workspace_id == workspace_id))
