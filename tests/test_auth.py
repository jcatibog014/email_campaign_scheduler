"""Logging in and out, the owner command, CSRF and safe redirects."""
from oncue import db
from oncue.models import User, Workspace

from .conftest import PASSWORD, browser, get_user, is_logged_in, make_owner, post, text


def test_the_login_page_has_only_the_login_and_the_demo(client):
    page = text(client.get("/login"))
    assert "Log in" in page and "Try the demo" in page
    assert "create-owner" not in page and "Setting up this site" not in page


def test_the_login_cookie_has_its_own_name(app, client):
    """Not Flask's "session": another Flask app on this PC (any port of 127.0.0.1) would
    write over it, and the next click would land on the login page."""
    make_owner(app)
    response = post(client, "/login", {"email": "maria@example.com", "password": PASSWORD})
    cookies = response.headers.getlist("Set-Cookie")
    assert any(cookie.startswith("oncue_session=") for cookie in cookies)
    assert not any(cookie.startswith("session=") for cookie in cookies)


def test_owner_logs_in_and_out(app, client):
    make_owner(app)
    response = post(client, "/login", {"email": "Maria@Example.com ", "password": PASSWORD})
    assert response.status_code == 302 and response.headers["Location"].endswith("/stage")
    assert is_logged_in(client)
    post(client, "/logout")
    assert not is_logged_in(client)


def test_wrong_password_and_unknown_email_get_the_same_answer(app, client):
    make_owner(app)
    wrong = post(client, "/login", {"email": "maria@example.com", "password": "nope nope"})
    unknown = post(client, "/login", {"email": "nobody@example.com", "password": "nope nope"})
    assert wrong.status_code == unknown.status_code == 401
    assert "Wrong email or password." in text(wrong) and "Wrong email or password." in text(unknown)


def test_five_wrong_passwords_lock_the_account(app, client):
    make_owner(app)
    for _ in range(5):
        post(client, "/login", {"email": "maria@example.com", "password": "wrong one"})
    locked = post(client, "/login", {"email": "maria@example.com", "password": PASSWORD})
    assert "Too many failed attempts" in text(locked)
    assert get_user(app, "maria@example.com").locked_until is not None


def test_pages_need_a_login(client):
    response = client.get("/settings")
    assert response.status_code == 302 and "/login?next=/settings" in response.headers["Location"]


def test_login_goes_back_only_to_this_site(app, client):
    make_owner(app)
    for target, expected in (("/settings", "/settings"), ("https://evil.example", "/stage"),
                             ("//evil.example", "/stage"), ("/\\evil.example", "/stage")):
        post(client, "/logout")
        response = post(client, "/login", {"email": "maria@example.com", "password": PASSWORD, "next": target})
        assert response.headers["Location"].endswith(expected), target


def test_a_form_without_the_csrf_token_is_refused(app):
    user_id, _ = make_owner(app)
    client = browser(app, user_id)
    response = client.post("/settings", data={"action": "name", "user_name": "Hacked"})
    assert response.status_code == 400
    assert get_user(app, "maria@example.com").name == "Maria Santos"


def test_a_password_change_logs_out_other_devices(app):
    user_id, _ = make_owner(app)
    phone, laptop = browser(app, user_id), browser(app, user_id)
    response = post(laptop, "/settings", {"action": "password", "current_password": PASSWORD,
                                          "new_password": "a new password", "confirm_password": "a new password"})
    assert response.status_code == 302
    assert is_logged_in(laptop) and not is_logged_in(phone)


def test_create_owner_makes_the_workspace_once(app):
    runner = app.test_cli_runner()
    result = runner.invoke(args=["create-owner", "--name", "Janelle", "--email", "Janelle@Example.com"],
                           input="long password\nlong password\n")
    assert result.exit_code == 0, result.output
    with app.app_context():
        s = db.session()
        user = s.query(User).one()
        assert user.email == "janelle@example.com" and not user.is_demo
        workspace = s.get(Workspace, user.workspace_id)
        assert workspace.sender_email == "janelle@example.com" and workspace.time_zone == "Asia/Manila"
    again = runner.invoke(args=["create-owner", "--name", "Other", "--email", "o@example.com"],
                          input="long password\nlong password\n")
    assert again.exit_code != 0 and "already an owner" in again.output


def test_set_password_command(app, client):
    make_owner(app)
    result = app.test_cli_runner().invoke(args=["set-password", "maria@example.com"],
                                          input="brand new pass\nbrand new pass\n")
    assert result.exit_code == 0, result.output
    response = post(client, "/login", {"email": "maria@example.com", "password": "brand new pass"})
    assert response.status_code == 302
