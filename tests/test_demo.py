"""Try Demo: a sandbox per visitor, deleted after 24 hours, that can't touch the owner's."""
from datetime import timedelta

from oncue import db, seed
from oncue.dates import utcnow
from oncue.models import User, Workspace

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
