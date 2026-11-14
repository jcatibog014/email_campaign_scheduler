"""A show's life: the rules for moving between cue states.

    Rehearsal --schedule--> Standby --its time comes--> Go --every email handled--> Curtain Down
        ^                     |                         |  ^
        +----unschedule-------+                     hold|  |resume
                                                        v  |
    Standby, Go and Hold can be Cut.                    Hold

Every change goes through move(), which checks the change is allowed from the
state the show is in NOW. Two things can change a show at the same moment
(the owner pressing Hold, and the worker finishing the last email), so:
- the Show model has a version column (optimistic locking): the second
  UPDATE finds a different version, SQLAlchemy raises StaleDataError, and
  that change is refused with "this show just changed";
- the worker uses claim_status(), a guarded UPDATE ... WHERE status = 'standby',
  so two schedulers can never both start the same show.
"""
from sqlalchemy import func, select, update
from sqlalchemy.orm.exc import StaleDataError

from . import audience
from .models import Show

REHEARSAL, STANDBY, GO, HOLD, CURTAIN_DOWN, CUT = "rehearsal", "standby", "go", "hold", "curtain_down", "cut"
ALLOWED = {
    REHEARSAL: {STANDBY},
    STANDBY: {REHEARSAL, GO, CUT},
    GO: {HOLD, CURTAIN_DOWN, CUT},
    HOLD: {GO, CUT},
    CURTAIN_DOWN: set(),
    CUT: set(),
}
EDITABLE = {REHEARSAL, STANDBY}
LIVE = {GO, HOLD}
NAME_MAX, SUBJECT_MAX = 120, 200
# Things that happen on a change (later stages add their own): function(s, show, old, new, moment)
on_move = []


class ShowError(Exception):
    """A change that isn't allowed. The message says why, for the page."""


def get(s, workspace_id, show_id):
    show = s.get(Show, show_id)
    return show if show is not None and show.workspace_id == workspace_id else None


def can(show, new_status):
    return new_status in ALLOWED[show.status]


def move(s, show, new_status, moment):
    """Change the status, or raise ShowError. The caller commits (and catches StaleDataError via commit())."""
    old = show.status
    if not can(show, new_status):
        raise ShowError(f"A show on {old.replace('_', ' ').title()} can't go to {new_status.replace('_', ' ').title()}.")
    show.status, show.updated_at = new_status, moment
    if new_status == GO and show.started_at is None:
        show.started_at = moment
    if new_status in (CURTAIN_DOWN, CUT):
        show.finished_at = moment
    if new_status == REHEARSAL:
        show.send_at = None
    for hook in on_move:
        hook(s, show, old, new_status, moment)


def save_or_conflict(s):
    """Commit; if someone else changed the show first, undo ours and say so."""
    try:
        s.commit()
    except StaleDataError:
        s.rollback()
        raise ShowError("This show just changed (maybe the crew backstage moved it on). "
                        "Reload the page and try again.") from None


def claim_status(s, show_id, old, new, moment):
    """Move a show from old to new only if it is still on old. True if this call did it.

    One UPDATE with the old status in its WHERE: the database lets only one
    caller match. Used by the scheduler, never by pages."""
    result = s.execute(update(Show).where(Show.id == show_id, Show.status == old)
                       .values(status=new, updated_at=moment, version=Show.version + 1,
                               **({"started_at": func.coalesce(Show.started_at, moment)} if new == GO else {}),
                               **({"finished_at": moment} if new in (CURTAIN_DOWN, CUT) else {}))
                       .execution_options(synchronize_session=False))
    return result.rowcount == 1


# ---------- What a show needs before it can go on Standby ----------

def problems(s, show, workspace):
    """Reasons it isn't ready to schedule (an empty list when it is)."""
    found = []
    if not show.subject.strip():
        found.append("Write a subject.")
    if not show.body.strip():
        found.append("Write the email.")
    if not workspace.sender_email or not workspace.sender_name:
        found.append("Add who the emails come from in Settings.")
    if audience.audience_condition(s, workspace, show.audience_kind, show.audience_ref) is None:
        found.append("Its list or segment was deleted. Choose another audience.")
    elif audience.audience_count(s, workspace, show.audience_kind, show.audience_ref) == 0:
        found.append("Nobody in its audience can get emails yet.")
    return found


def schedule(s, show, workspace, send_at, moment):
    """Rehearsal (or Standby, to change the time) -> Standby at send_at."""
    if show.status not in (REHEARSAL, STANDBY):
        raise ShowError("Only a show in rehearsal or on standby can be scheduled.")
    if found := problems(s, show, workspace):
        raise ShowError(" ".join(found))
    if send_at < moment.replace(second=0, microsecond=0):
        raise ShowError("Choose a time that hasn't passed. To send it now, use Start now.")
    show.send_at = send_at
    if show.status == REHEARSAL:
        move(s, show, STANDBY, moment)
    show.updated_at = moment


def start_now(s, show, workspace, moment):
    """Standby at this very minute: the scheduler picks it up on its next round."""
    schedule(s, show, workspace, moment, moment)


def duplicate(s, show, moment):
    copy = Show(workspace_id=show.workspace_id, name=f"{show.name} (copy)"[:NAME_MAX], subject=show.subject,
                preheader=show.preheader, body=show.body, script_id=show.script_id,
                audience_kind=show.audience_kind, audience_ref=show.audience_ref, created_at=moment,
                updated_at=moment)
    s.add(copy)
    s.flush()
    return copy


def counts_by_status(s, workspace_id):
    return dict(s.execute(select(Show.status, func.count()).where(Show.workspace_id == workspace_id)
                          .group_by(Show.status)).all())
