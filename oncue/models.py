"""The data model: one class per table.

Every row that belongs to someone carries workspace_id. There is the owner's
workspace (the real one) and one sandbox workspace per Try Demo visitor, so a
demo can never see or touch the owner's audience, shows or jobs.

Times are naive UTC (dates.utcnow()). Ids of workspaces and users are never
reused (sqlite_autoincrement), so an old demo cookie can't open a newer demo.
"""
from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint, false
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .dates import DEFAULT_ZONE, utcnow
from .db import Base


class Workspace(Base):
    """One sending account: the owner's, or a demo's sandbox."""
    __tablename__ = "workspaces"
    __table_args__ = {"sqlite_autoincrement": True}

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    is_demo: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    demo_expires_at: Mapped[datetime | None] = mapped_column(DateTime)
    time_zone: Mapped[str] = mapped_column(String(64), default=DEFAULT_ZONE, server_default=DEFAULT_ZONE)
    # Who the emails come from, and the postal address every email shows at the
    # bottom (anti-spam laws ask for one).
    sender_name: Mapped[str] = mapped_column(String(120), default="", server_default="")
    sender_email: Mapped[str] = mapped_column(String(254), default="", server_default="")
    postal_address: Mapped[str] = mapped_column(String(300), default="", server_default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    users: Mapped[list["User"]] = relationship(back_populates="workspace", cascade="all, delete-orphan",
                                               passive_deletes=True)


class User(Base):
    """Someone who can log in. The owner (made with flask create-owner) or a demo visitor."""
    __tablename__ = "users"
    __table_args__ = {"sqlite_autoincrement": True}

    id: Mapped[int] = mapped_column(primary_key=True)
    workspace_id: Mapped[int] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(80))
    email: Mapped[str] = mapped_column(String(254), unique=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    is_demo: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    # Part of the login cookie. Changing it (a new password) logs out every other device.
    session_version: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    failed_logins: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    locked_until: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    workspace: Mapped[Workspace] = relationship(back_populates="users")

    @property
    def first_name(self):
        return self.name.split()[0] if self.name else ""

    @property
    def initial(self):
        return (self.name or "?")[0].upper()


class RateLimit(Base):
    """'At most N tries per window' counters for logins (limits.py).

    Not to be confused with the SENDING limits (buckets.py): these stop people
    guessing passwords; those stop us sending faster than the mail service allows.
    """
    __tablename__ = "rate_limits"

    key: Mapped[str] = mapped_column(String(200), primary_key=True)
    count: Mapped[int] = mapped_column(Integer, default=0)
    window_start: Mapped[datetime] = mapped_column(DateTime)
