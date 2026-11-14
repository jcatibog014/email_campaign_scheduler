"""The data model: one class per table.

Every row that belongs to someone carries workspace_id. There is the owner's
workspace (the real one) and one sandbox workspace per Try Demo visitor, so a
demo can never see or touch the owner's audience, shows or jobs.

Times are naive UTC (dates.utcnow()). Ids of workspaces and users are never
reused (sqlite_autoincrement), so an old demo cookie can't open a newer demo.
"""
from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint, false, true
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


# ---------- Stage 2: the audience ----------

class Contact(Base):
    """One person who may get your shows. Their email is unique in the workspace."""
    __tablename__ = "contacts"
    __table_args__ = (UniqueConstraint("workspace_id", "email"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    workspace_id: Mapped[int] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"))
    email: Mapped[str] = mapped_column(String(254))
    first_name: Mapped[str] = mapped_column(String(80), default="", server_default="")
    last_name: Mapped[str] = mapped_column(String(80), default="", server_default="")
    # Anything else from a CSV file ("city": "Cebu", "plan": "Season pass"): merge fields
    # in a Script, and segment rules. JSON, so new columns need no migration.
    fields: Mapped[dict] = mapped_column(JSON, default=dict)
    # Their own time zone, for shows sent "at 9:00 AM their time". None = the workspace's.
    time_zone: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(20), default="subscribed", server_default="subscribed")
    # When and how they agreed to hear from you. Sending without consent is spam.
    consent_at: Mapped[datetime | None] = mapped_column(DateTime)
    consent_note: Mapped[str] = mapped_column(String(200), default="", server_default="")
    unsubscribed_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    tags: Mapped[list["ContactTag"]] = relationship(cascade="all, delete-orphan", passive_deletes=True,
                                                    order_by="ContactTag.tag")

    @property
    def name(self):
        return " ".join(part for part in (self.first_name, self.last_name) if part)

    @property
    def tag_names(self):
        return [tag.tag for tag in self.tags]


class ContactTag(Base):
    """A label on a contact ("member", "donor"). A contact can have many."""
    __tablename__ = "contact_tags"

    contact_id: Mapped[int] = mapped_column(ForeignKey("contacts.id", ondelete="CASCADE"), primary_key=True)
    tag: Mapped[str] = mapped_column(String(40), primary_key=True)


Index("ix_contact_tags_tag", ContactTag.tag)


class ContactList(Base):
    """A list people are put on by hand or by an import ("Season Subscribers").
    A list marked as a topic shows on the unsubscribe page, so people can leave
    just that list instead of everything."""
    __tablename__ = "lists"
    __table_args__ = (UniqueConstraint("workspace_id", "name"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    workspace_id: Mapped[int] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(80))
    description: Mapped[str] = mapped_column(String(300), default="", server_default="")
    is_topic: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true())
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class ListMember(Base):
    __tablename__ = "list_members"

    list_id: Mapped[int] = mapped_column(ForeignKey("lists.id", ondelete="CASCADE"), primary_key=True)
    contact_id: Mapped[int] = mapped_column(ForeignKey("contacts.id", ondelete="CASCADE"), primary_key=True)
    added_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


# Deleting a contact deletes their memberships: look them up by contact quickly.
Index("ix_list_members_contact_id", ListMember.contact_id)


class Segment(Base):
    """A saved search: everyone who matches its rules right now (segments.py).
    Unlike a list, nobody is put on it: it is worked out each time."""
    __tablename__ = "segments"
    __table_args__ = (UniqueConstraint("workspace_id", "name"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    workspace_id: Mapped[int] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(80))
    match: Mapped[str] = mapped_column(String(3), default="all", server_default="all")  # all / any
    # [{"field": "city", "op": "is", "value": "Cebu"}, ...]
    rules: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Suppression(Base):
    """The do-not-send list: addresses that never get an email again, whatever
    list or segment they are on. Bounces and spam complaints land here by
    themselves (tracking.py); the owner can add an address by hand."""
    __tablename__ = "suppressions"
    __table_args__ = (UniqueConstraint("workspace_id", "email"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    workspace_id: Mapped[int] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"))
    email: Mapped[str] = mapped_column(String(254))
    reason: Mapped[str] = mapped_column(String(20))  # bounced / complained / manual
    note: Mapped[str] = mapped_column(String(300), default="", server_default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class CsvImport(Base):
    """A CSV file between Upload and Done. The rows wait here (not in the login
    cookie, which is too small) while the owner maps the columns and checks the
    numbers. Old ones are deleted after a day."""
    __tablename__ = "csv_imports"

    id: Mapped[int] = mapped_column(primary_key=True)
    workspace_id: Mapped[int] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"), index=True)
    filename: Mapped[str] = mapped_column(String(200))
    header: Mapped[list] = mapped_column(JSON)
    rows: Mapped[list] = mapped_column(JSON)
    mapping: Mapped[dict] = mapped_column(JSON, default=dict)
    list_id: Mapped[int | None] = mapped_column(ForeignKey("lists.id", ondelete="SET NULL"), index=True)
    status: Mapped[str] = mapped_column(String(10), default="mapping")  # mapping / review / done
    result: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


# ---------- Stage 3: scripts and shows ----------

class Script(Base):
    """A saved email (a template): subject, preview line and a Markdown body
    with merge fields like {{first_name|there}}. A show copies it when made,
    so changing a script later never changes a show that already went out."""
    __tablename__ = "scripts"
    __table_args__ = (UniqueConstraint("workspace_id", "name"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    workspace_id: Mapped[int] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(80))
    subject: Mapped[str] = mapped_column(String(200), default="", server_default="")
    preheader: Mapped[str] = mapped_column(String(200), default="", server_default="")
    body: Mapped[str] = mapped_column(Text, default="", server_default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Show(Base):
    """A campaign: one email to an audience, at a time (shows.py has its rules).

    version: SQLAlchemy adds "AND version = <what we read>" to every UPDATE and
    raises StaleDataError if nothing matched. So when the worker moves a show
    on (Go -> Curtain Down) while the owner presses Hold on an old page, one of
    them fails loudly instead of quietly undoing the other (optimistic locking).
    """
    __tablename__ = "shows"

    id: Mapped[int] = mapped_column(primary_key=True)
    workspace_id: Mapped[int] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(120))
    subject: Mapped[str] = mapped_column(String(200), default="", server_default="")
    preheader: Mapped[str] = mapped_column(String(200), default="", server_default="")
    body: Mapped[str] = mapped_column(Text, default="", server_default="")
    script_id: Mapped[int | None] = mapped_column(ForeignKey("scripts.id", ondelete="SET NULL"), index=True)
    # Who gets it: "all", or "list" / "segment" / "tag" with audience_ref (an id or the tag).
    audience_kind: Mapped[str] = mapped_column(String(10), default="all", server_default="all")
    audience_ref: Mapped[str | None] = mapped_column(String(60))
    status: Mapped[str] = mapped_column(String(15), default="rehearsal", server_default="rehearsal", index=True)
    send_at: Mapped[datetime | None] = mapped_column(DateTime, index=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    version: Mapped[int] = mapped_column(Integer, default=1, server_default="1")

    __mapper_args__ = {"version_id_col": version}
