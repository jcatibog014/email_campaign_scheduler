"""The data model: one class per table.

Every row that belongs to someone carries workspace_id. There is the owner's
workspace (the real one) and one sandbox workspace per Try Demo visitor, so a
demo can never see or touch the owner's audience, shows or jobs.

Times are naive UTC (dates.utcnow()). Ids of workspaces and users are never
reused (sqlite_autoincrement), so an old demo cookie can't open a newer demo.
"""
from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint, false, true
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
    # Sending (stage 5, buckets.py): which mailer, how fast, how many a day.
    mailer: Mapped[str] = mapped_column(String(12), default="practice", server_default="practice")
    send_rate: Mapped[float] = mapped_column(Float, default=5.0, server_default="5")  # emails a second
    daily_cap: Mapped[int] = mapped_column(Integer, default=300, server_default="300")  # Brevo's free plan
    # How difficult the practice mail server is, in percent of emails (practice_server.py).
    practice_busy: Mapped[int] = mapped_column(Integer, default=6, server_default="6")
    practice_timeout: Mapped[int] = mapped_column(Integer, default=3, server_default="3")
    practice_bounce: Mapped[int] = mapped_column(Integer, default=3, server_default="3")
    practice_slow: Mapped[int] = mapped_column(Integer, default=10, server_default="10")

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


# ---------- Stage 4: the job queue, the crew, performances and messages ----------

class Job(Base):
    """One piece of background work in the queue (queue.py).

    status: queued -> running -> done, or back to queued for a retry, or dead
    (a dead letter) after its last try. A running job holds a LEASE: if its
    worker dies, the lease runs out and the reaper puts the job back.
    dedupe_key is UNIQUE, so the same work ("start performance 7") can never
    be queued twice, even by two workers at the same moment.
    """
    __tablename__ = "jobs"
    __table_args__ = (Index("ix_jobs_status_run_at", "status", "run_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    workspace_id: Mapped[int | None] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(40))
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(12), default="queued", server_default="queued")
    run_at: Mapped[datetime] = mapped_column(DateTime)
    priority: Mapped[int] = mapped_column(Integer, default=5, server_default="5")  # lower goes first
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    max_attempts: Mapped[int] = mapped_column(Integer, default=6, server_default="6")
    lease_until: Mapped[datetime | None] = mapped_column(DateTime)
    locked_by: Mapped[str | None] = mapped_column(String(80))
    last_error: Mapped[str] = mapped_column(Text, default="", server_default="")
    dedupe_key: Mapped[str | None] = mapped_column(String(120), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)


class Worker(Base):
    """A member of the crew: a worker thread in the web app, or `flask worker`.
    It writes last_seen every round (a heartbeat), so Backstage can tell who is
    on duty and who has gone quiet."""
    __tablename__ = "workers"

    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    kind: Mapped[str] = mapped_column(String(20))  # "in the web app" / "own process" / "timer"
    started_at: Mapped[datetime] = mapped_column(DateTime)
    last_seen: Mapped[datetime] = mapped_column(DateTime)
    jobs_done: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    stopped_at: Mapped[datetime | None] = mapped_column(DateTime)


class Performance(Base):
    """One run of a show. A one-off show has one; a weekly show one per week."""
    __tablename__ = "performances"
    __table_args__ = (UniqueConstraint("show_id", "number"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    workspace_id: Mapped[int] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"), index=True)
    show_id: Mapped[int] = mapped_column(ForeignKey("shows.id", ondelete="CASCADE"))
    number: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(12), default="fanning")  # fanning / sending / done / cut
    starts_at: Mapped[datetime] = mapped_column(DateTime)
    fanned_out: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    audience_size: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)


class Message(Base):
    """One email to one person, and everything that happened to it.

    UNIQUE(performance_id, contact_id): however many times the fan-out runs
    (a retry, two workers), each person gets one message per performance.
    send_token is the idempotency key: it is saved BEFORE the mail service is
    called, so after a crash we can ask "did you already take this one?".
    """
    __tablename__ = "messages"
    __table_args__ = (UniqueConstraint("performance_id", "contact_id"),
                      Index("ix_messages_performance_status", "performance_id", "status"))

    id: Mapped[int] = mapped_column(primary_key=True)
    workspace_id: Mapped[int] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"), index=True)
    show_id: Mapped[int] = mapped_column(ForeignKey("shows.id", ondelete="CASCADE"), index=True)
    performance_id: Mapped[int | None] = mapped_column(ForeignKey("performances.id", ondelete="CASCADE"))
    contact_id: Mapped[int | None] = mapped_column(ForeignKey("contacts.id", ondelete="SET NULL"), index=True)
    email: Mapped[str] = mapped_column(String(254))
    is_test: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    status: Mapped[str] = mapped_column(String(12), default="wings", server_default="wings")
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    due_at: Mapped[datetime] = mapped_column(DateTime)
    next_try_at: Mapped[datetime | None] = mapped_column(DateTime)
    send_token: Mapped[str | None] = mapped_column(String(40))
    mailer: Mapped[str] = mapped_column(String(12), default="", server_default="")
    provider_id: Mapped[str | None] = mapped_column(String(120), index=True)
    last_error: Mapped[str] = mapped_column(String(300), default="", server_default="")
    sent_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class PracticeMail(Base):
    """The practice mail server's inbox: every email it took (practice_server.py).
    idempotency_key is UNIQUE: the same send_token sent twice is taken once."""
    __tablename__ = "practice_mail"

    id: Mapped[int] = mapped_column(primary_key=True)
    workspace_id: Mapped[int] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"), index=True)
    idempotency_key: Mapped[str] = mapped_column(String(40), unique=True)
    provider_id: Mapped[str] = mapped_column(String(60), unique=True)
    to: Mapped[str] = mapped_column(String(254))
    subject: Mapped[str] = mapped_column(String(300))
    html: Mapped[str] = mapped_column(Text)
    text: Mapped[str] = mapped_column(Text)
    headers: Mapped[dict] = mapped_column(JSON, default=dict)
    fate: Mapped[str] = mapped_column(String(12), default="deliver", server_default="deliver")
    accepted_at: Mapped[datetime] = mapped_column(DateTime)


# ---------- Stage 5: sending limits and the Safety Curtain ----------

class Bucket(Base):
    """A token bucket (buckets.py): `tokens` refill at a steady rate up to a
    capacity; each email takes one. Changed only by compare-and-swap, so two
    workers can never both spend the last token."""
    __tablename__ = "buckets"

    key: Mapped[str] = mapped_column(String(120), primary_key=True)
    workspace_id: Mapped[int | None] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"), index=True)
    tokens: Mapped[float] = mapped_column(Float)
    updated_at: Mapped[datetime] = mapped_column(DateTime)
    # After a 429 ("too many"), the bucket refills at half speed until this time.
    slow_until: Mapped[datetime | None] = mapped_column(DateTime)


class SendCount(Base):
    """How many emails a mailer took today (the daily cap)."""
    __tablename__ = "send_counts"

    key: Mapped[str] = mapped_column(String(120), primary_key=True)  # day:<workspace>:<mailer>:<YYYY-MM-DD>
    workspace_id: Mapped[int | None] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"), index=True)
    count: Mapped[int] = mapped_column(Integer, default=0)


class Breaker(Base):
    """The Safety Curtain of one workspace's mailer (a circuit breaker).

    closed: sending as usual. open: too many failures in a row, so nothing is
    sent until retry_at. half_open: one test email (the probe) decides whether
    it closes again or stays open."""
    __tablename__ = "breakers"

    key: Mapped[str] = mapped_column(String(80), primary_key=True)  # <workspace>:<mailer>
    workspace_id: Mapped[int] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"), index=True)
    mailer: Mapped[str] = mapped_column(String(12))
    state: Mapped[str] = mapped_column(String(10), default="closed")
    failures: Mapped[int] = mapped_column(Integer, default=0)
    opened_at: Mapped[datetime | None] = mapped_column(DateTime)
    retry_at: Mapped[datetime | None] = mapped_column(DateTime)
    probe_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_error: Mapped[str] = mapped_column(String(300), default="")
    version: Mapped[int] = mapped_column(Integer, default=1)


class DomainLimit(Base):
    """At most per_minute emails a minute to one receiving domain (gmail.com).
    Big mail services slow down senders who send them too much at once."""
    __tablename__ = "domain_limits"
    __table_args__ = (UniqueConstraint("workspace_id", "domain"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    workspace_id: Mapped[int] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"))
    domain: Mapped[str] = mapped_column(String(120))
    per_minute: Mapped[int] = mapped_column(Integer)
