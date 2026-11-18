"""Try Demo: a sandbox per visitor, deleted after 24 hours, that can't touch the owner's."""
from datetime import timedelta

from sqlalchemy import select

from oncue import audience, db, seed, worker
from oncue.dates import utcnow
from oncue.models import Contact, ContactList, Script, Segment, Show, User, Workspace

from .conftest import browser, is_logged_in, make_owner, post, text


def start_demo(app):
    client = browser(app)
    response = post(client, "/demo")
    assert response.status_code == 302
    return client


def test_try_demo_gives_a_sandbox_of_your_own(app):
    first, second = start_demo(app), start_demo(app)
    with app.app_context():
        s = db.session()
        demos = s.query(Workspace).filter_by(is_demo=True).all()
        assert len(demos) == 2 and all(w.demo_expires_at > utcnow() for w in demos)
    page = text(first.get("/stage", follow_redirects=True))
    assert "Teatro Luna" in page and ">Demo<" in page and "practice mail server" in page
    assert is_logged_in(second)


def test_starting_a_demo_wakes_the_crew(app, monkeypatch):
    """Its replay and its one-minute show mustn't wait for a sleeping worker (up to 30 min)."""
    woken = []
    monkeypatch.setattr(worker, "nudge", lambda: woken.append(True))
    start_demo(app)
    assert woken == [True]


def test_the_demo_audience_has_examples_on_every_page(app):
    client = start_demo(app)
    with app.app_context():
        s = db.session()
        workspace = s.query(Workspace).filter_by(is_demo=True).one()
        list_id = s.scalar(select(ContactList.id).where(ContactList.workspace_id == workspace.id))
        segment_id = s.scalar(select(Segment.id).where(Segment.workspace_id == workspace.id,
                                                       Segment.name == "Fans Abroad"))
        contact_id = s.scalar(select(Contact.id).where(Contact.workspace_id == workspace.id))
        assert audience.audience_count(s, workspace, "segment", segment_id) > 5
    for path, expected in (("/audience/", "Showing 1"), ("/audience/lists", "Season Subscribers"),
                           (f"/audience/lists/{list_id}", "Take off"), ("/audience/segments", "Fans Abroad"),
                           (f"/audience/segments/{segment_id}", "Matches Now"),
                           ("/audience/do-not-send", "Marked the March newsletter as spam"),
                           (f"/audience/{contact_id}", "Plan"), ("/audience/?status=unsubscribed", "Unsubscribed")):
        response = client.get(path)
        assert response.status_code == 200 and expected in text(response), path
        assert 'class="empty' not in text(response), path  # no empty stage anywhere


def test_the_demo_has_scripts_and_shows(app):
    client = start_demo(app)
    with app.app_context():
        s = db.session()
        workspace = s.query(Workspace).filter_by(is_demo=True).one()
        standby = s.scalar(select(Show).where(Show.workspace_id == workspace.id, Show.status == "standby"))
        script_id = s.scalar(select(Script.id).where(Script.workspace_id == workspace.id))
    for path, expected in (("/scripts", "Monthly Programme"), (f"/scripts/{script_id}", "Merge Fields"),
                           ("/shows/", "December Programme"), (f"/shows/{standby.id}", "Standby"),
                           ("/stage", "Up Next"), ("/shows/?tab=rehearsal", "Closing Night Reminder")):
        response = client.get(path)
        assert response.status_code == 200 and expected in text(response), path
        assert 'class="empty' not in text(response), path
    frame = client.get(f"/shows/{standby.id}/frame")
    assert "As a Season pass holder" in text(frame) or "friend of the theatre" in text(frame)


def test_the_replayed_past_fills_every_page(app):
    """After the demo_history job, nothing the demo can open is an empty stage."""
    from oncue import worker
    from oncue.models import Contact as ContactModel, Job, Message
    client = start_demo(app)
    assert "Replaying Teatro Luna" in text(client.get("/stage"))
    with app.app_context():
        s = db.session()
        workspace_id = s.scalar(select(Workspace.id).where(Workspace.is_demo.is_(True)))
        # The live crew leaves a replaying demo alone: its jobs aren't claimable by anyone else.
        assert s.scalar(select(Job.kind).where(Job.workspace_id.is_(None))) == "demo_history"
        worker.run_until_idle(s, worker_id="live")
        workspace = s.get(Workspace, workspace_id)
        assert workspace.replaying is False
        from oncue.models import Worker
        assert s.get(Worker, f"replay-{workspace_id}") is None  # the replay leaves no worker row
        finished = s.scalar(select(Show).where(Show.workspace_id == workspace_id, Show.name == "Season Opener: Ang Huling Ilaw"))
        assert finished.status == "curtain_down"
        message = s.scalar(select(Message).where(Message.show_id == finished.id).order_by(Message.id))
        contact_with_history = s.scalar(select(ContactModel.id).where(ContactModel.id == message.contact_id))
        weekly = s.scalar(select(Show.id).where(Show.workspace_id == workspace_id, Show.name == "Volunteer Call Sheet"))
    assert "Replaying Teatro Luna" not in text(client.get("/stage"))
    pages = {
        "/stage": "Ticket Holds Expire Tonight", "/shows/": "Season Opener", "/shows/?tab=done": "October Programme",
        "/shows/?tab=cut": "Rain Delay Notice", f"/shows/{finished.id}": "Results",
        f"/shows/{finished.id}/emails": "Every Email", f"/shows/{finished.id}/emails/{message.id}": "What Happened",
        f"/shows/{weekly}": "Every Monday, Thursday", "/call-board/": "Call Board", "/call-board/?view=month": "Volunteer",
        "/reviews/": "Delivery Funnel", "/backstage/": "The Crew", "/backstage/dead-letters": "Retry now",
        "/backstage/unsure": "Resend", "/backstage/inbox": "Practice", "/backstage/retries": "Missed",
        f"/audience/{contact_with_history}": "Emails They Got", "/settings": "Practice Mail Server",
    }
    for path, expected in pages.items():
        response = client.get(path)
        assert response.status_code == 200 and expected in text(response), path
        assert 'class="empty' not in text(response), path


def test_one_network_can_start_ten_demos_an_hour(app, monkeypatch):
    from oncue import seed as seed_module
    monkeypatch.setattr(seed_module, "seed_audience", lambda *args: None)  # quick demos for this test
    for _ in range(10):
        start_demo(app)
    refused = post(browser(app), "/demo", follow_redirects=True)
    assert "a lot of demos from your network" in text(refused)


def test_the_demo_cant_change_its_account(app):
    client = start_demo(app)
    response = post(client, "/settings", {"action": "name", "user_name": "Someone"})
    assert response.status_code == 403
    assert "can't be changed" in text(client.get("/settings"))


def test_the_demo_cant_log_in_with_a_password(app, client):
    start_demo(app)
    with app.app_context():
        email = db.session().query(User).filter_by(is_demo=True).one().email
    response = post(client, "/login", {"email": email, "password": "anything at all"})
    assert response.status_code == 401


def test_old_demos_are_deleted_with_everything_in_them(app):
    make_owner(app)
    client = start_demo(app)
    with app.app_context():
        s = db.session()
        assert seed.purge(s, utcnow() + timedelta(hours=25)) == 1
        s.commit()
        assert s.query(Workspace).count() == 1 and s.query(User).count() == 1  # only the owner's
    assert not is_logged_in(client)


def test_a_run_out_demo_is_logged_out(app):
    client = start_demo(app)
    with app.app_context():
        s = db.session()
        s.query(Workspace).filter_by(is_demo=True).one().demo_expires_at = utcnow() - timedelta(seconds=1)
        s.commit()
    assert not is_logged_in(client)
