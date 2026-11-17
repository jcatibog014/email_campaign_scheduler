"""Scripts and shows: the builder, the cue rules, scheduling and the preview frame."""
from datetime import timedelta

import pytest
from sqlalchemy import select

from oncue import db, shows
from oncue.dates import utcnow
from oncue.models import Contact, Script, Show, Workspace

from .conftest import browser, make_owner, post, text, words


def with_audience(app, workspace_id, count=3):
    with app.app_context():
        s = db.session()
        for index in range(count):
            s.add(Contact(workspace_id=workspace_id, email=f"person{index}@example.com", first_name=f"P{index}", fields={}))
        s.commit()


def make_show(client, **extra):
    data = {"name": "Opening Night", "audience": "all", "subject": "Hi {{first_name|there}}",
            "preheader": "Doors at 7", "body": "Come to **opening night**!", **extra}
    response = post(client, "/shows/new", data)
    assert response.status_code == 302, text(response)
    return int(response.headers["Location"].rsplit("/", 1)[1])


def get_show(app, show_id):
    with app.app_context():
        return db.session().get(Show, show_id)


def local_input(moment):
    """The datetime-local value for a UTC moment, in Manila time (UTC+8)."""
    return (moment + timedelta(hours=8)).strftime("%Y-%m-%dT%H:%M")


def test_make_a_show_in_rehearsal(app, owner):
    client, workspace_id = owner
    with_audience(app, workspace_id)
    show_id = make_show(client)
    show = get_show(app, show_id)
    assert (show.status, show.audience_kind, show.subject) == ("rehearsal", "all", "Hi {{first_name|there}}")
    page = words(client.get(f"/shows/{show_id}"))
    assert "Rehearsal" in page and "3 people can get emails now" in page and "Schedule" in page


def test_schedule_then_unschedule(app, owner):
    client, workspace_id = owner
    with_audience(app, workspace_id)
    show_id = make_show(client)
    when = utcnow().replace(second=0, microsecond=0) + timedelta(days=1)
    response = post(client, f"/shows/{show_id}/schedule", {"send_at": local_input(when), "version": "1"},
                    follow_redirects=True)
    assert "Opening Night is on Standby." in text(response)
    show = get_show(app, show_id)
    assert show.status == "standby" and show.send_at == when
    post(client, f"/shows/{show_id}/unschedule", {"version": str(show.version)})
    show = get_show(app, show_id)
    assert show.status == "rehearsal" and show.send_at is None


def test_a_show_needs_a_subject_an_email_and_people(app, owner):
    client, workspace_id = owner
    show_id = make_show(client, subject="", body="")
    page = words(client.get(f"/shows/{show_id}"))
    assert "Not ready to go on Standby" in page and "Write a subject." in page and "Nobody in its audience" in page
    when = local_input(utcnow() + timedelta(days=1))
    response = post(client, f"/shows/{show_id}/schedule", {"send_at": when}, follow_redirects=True)
    assert "Write a subject." in text(response) and get_show(app, show_id).status == "rehearsal"


def test_the_past_is_refused(app, owner):
    client, workspace_id = owner
    with_audience(app, workspace_id)
    show_id = make_show(client)
    response = post(client, f"/shows/{show_id}/schedule", {"send_at": local_input(utcnow() - timedelta(hours=2))},
                    follow_redirects=True)
    assert "Choose a time that hasn't passed" in text(response)


def test_the_cue_rules(app, ctx):
    show = Show(workspace_id=1, name="x", status="rehearsal")
    moment = utcnow()
    with pytest.raises(shows.ShowError):
        shows.move(ctx, show, shows.GO, moment)  # rehearsal can't jump to Go
    show.status = shows.GO
    shows.move(ctx, show, shows.HOLD, moment)
    shows.move(ctx, show, shows.GO, moment)
    shows.move(ctx, show, shows.CURTAIN_DOWN, moment)
    assert show.finished_at == moment
    for target in shows.ALLOWED:
        assert not shows.can(show, target)  # Curtain Down is the end


def test_an_old_page_cant_undo_a_newer_change(app, owner):
    client, workspace_id = owner
    with_audience(app, workspace_id)
    show_id = make_show(client)
    old_version = get_show(app, show_id).version
    post(client, f"/shows/{show_id}/start", {"version": str(old_version)})
    assert get_show(app, show_id).status == "standby"
    # The same old page's "Back to rehearsal" button: the version no longer matches.
    response = post(client, f"/shows/{show_id}/unschedule", {"version": str(old_version)}, follow_redirects=True)
    assert "changed since you opened the page" in text(response)
    assert get_show(app, show_id).status == "standby"


def test_the_database_refuses_a_stale_update(app, owner):
    """Optimistic locking: two sessions read version 1; the second save fails."""
    client, workspace_id = owner
    with_audience(app, workspace_id)
    show_id = make_show(client)
    with app.app_context():
        first, second = db.new_session(), db.new_session()
        mine, theirs = first.get(Show, show_id), second.get(Show, show_id)
        theirs.name = "Changed by the worker"
        second.commit()
        mine.name = "Changed on an old page"
        with pytest.raises(shows.ShowError):
            shows.save_or_conflict(first)
        first.close(), second.close()
    assert get_show(app, show_id).name == "Changed by the worker"


def test_only_one_scheduler_can_start_a_show(app, owner):
    client, workspace_id = owner
    with_audience(app, workspace_id)
    show_id = make_show(client)
    post(client, f"/shows/{show_id}/start", {})
    with app.app_context():
        s = db.session()
        assert shows.claim_status(s, show_id, shows.STANDBY, shows.GO, utcnow()) is True
        assert shows.claim_status(s, show_id, shows.STANDBY, shows.GO, utcnow()) is False
        s.commit()
    assert get_show(app, show_id).status == "go"


def test_a_started_show_cant_be_edited_but_can_be_duplicated(app, owner):
    client, workspace_id = owner
    with_audience(app, workspace_id)
    show_id = make_show(client)
    with app.app_context():
        s = db.session()
        s.get(Show, show_id).status = shows.GO
        s.commit()
    assert "can't be changed" in text(client.get(f"/shows/{show_id}/edit", follow_redirects=True))
    response = post(client, f"/shows/{show_id}/duplicate")
    copy = get_show(app, int(response.headers["Location"].split("/")[-2]))
    assert copy.name == "Opening Night (copy)" and copy.status == "rehearsal" and copy.body == "Come to **opening night**!"
    assert post(client, f"/shows/{show_id}/delete", follow_redirects=True).status_code == 200
    assert get_show(app, show_id) is not None  # only rehearsals can be deleted


def test_hold_resume_and_cut(app, owner):
    client, workspace_id = owner
    with_audience(app, workspace_id)
    show_id = make_show(client)
    with app.app_context():
        s = db.session()
        s.get(Show, show_id).status = shows.GO
        s.commit()
    post(client, f"/shows/{show_id}/hold")
    assert get_show(app, show_id).status == "hold"
    post(client, f"/shows/{show_id}/resume")
    assert get_show(app, show_id).status == "go"
    post(client, f"/shows/{show_id}/cut")
    show = get_show(app, show_id)
    assert show.status == "cut" and show.finished_at is not None


def test_another_workspaces_show_is_not_found(app, owner):
    client, workspace_id = owner
    with_audience(app, workspace_id)
    show_id = make_show(client)
    other_user, _ = make_owner(app, name="Other", email="other@example.com")
    other = browser(app, other_user)
    for path in (f"/shows/{show_id}", f"/shows/{show_id}/frame", f"/shows/{show_id}/edit"):
        assert other.get(path).status_code == 404, path
    assert post(other, f"/shows/{show_id}/cut").status_code == 404


def test_the_preview_frame_has_its_own_strict_rules(app, owner):
    client, workspace_id = owner
    with_audience(app, workspace_id)
    show_id = make_show(client)
    with app.app_context():
        contact_id = db.session().scalar(select(Contact.id).where(Contact.first_name == "P1"))
    response = client.get(f"/shows/{show_id}/frame?contact={contact_id}")
    policy = response.headers["Content-Security-Policy"]
    assert "default-src 'none'" in policy and "script-src" not in policy and "style-src 'unsafe-inline'" in policy
    assert "Come to <strong>opening night</strong>!" in text(response)
    draft = post(client, "/preview-frame", {"subject": "x", "body": "Hello {{first_name}}", "contact": str(contact_id)})
    assert "Hello P1" in text(draft) and "default-src 'none'" in draft.headers["Content-Security-Policy"]


def test_scripts_and_using_one_in_a_show(app, owner):
    client, workspace_id = owner
    response = post(client, "/scripts/new", {"name": "Welcome", "subject": "Welcome, {{first_name|friend}}",
                                             "preheader": "", "body": "Hi {{frist_name}}"})
    script_id = int(response.headers["Location"].rsplit("/", 1)[1])
    page = text(client.get(f"/scripts/{script_id}"))
    assert "Nobody has" in page and "{{frist_name}}" in page  # a likely typo is pointed out
    builder = text(client.get(f"/shows/new?script={script_id}"))
    assert "Starting from the script Welcome" in builder and "Welcome, {{first_name|friend}}" in builder
    with app.app_context():
        assert db.session().get(Script, script_id).workspace_id == workspace_id


def test_the_stage_shows_whats_on(app, owner):
    client, workspace_id = owner
    with_audience(app, workspace_id)
    standby_id = make_show(client, name="Season Opener")
    post(client, f"/shows/{standby_id}/schedule", {"send_at": local_input(utcnow() + timedelta(days=2))})
    live_id = make_show(client, name="Tonight's Reminder")
    with app.app_context():
        s = db.session()
        live = s.get(Show, live_id)
        live.status, live.started_at = shows.GO, utcnow()
        s.commit()
        assert s.get(Workspace, workspace_id) is not None
    page = text(client.get("/stage"))
    assert "Now Playing" in page and "Tonight&#39;s Reminder" in client.get("/stage").get_data(as_text=True)
    assert 'class="card marquee is-live"' in page and "cue-card" in page and "Season Opener" in page
