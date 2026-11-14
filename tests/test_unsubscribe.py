"""The unsubscribe page, and one-click unsubscribe from mail apps."""
from sqlalchemy import select

from oncue import audience, db
from oncue.dates import utcnow
from oncue.models import Contact, ContactList, ListMember
from oncue.views.public import unsubscribe_token

from .conftest import make_owner, post, text


def setup_person(app):
    _user, workspace_id = make_owner(app)
    with app.app_context():
        s = db.session()
        ana = Contact(workspace_id=workspace_id, email="ana@example.com", fields={})
        s.add(ana)
        for name in ("Season News", "Workshops"):
            s.add(ContactList(workspace_id=workspace_id, name=name))
        s.add(ContactList(workspace_id=workspace_id, name="Hidden Helpers", is_topic=False))
        s.flush()
        for found in s.scalars(select(ContactList)):
            audience.add_to_list(s, found.id, [ana.id], utcnow())
        s.commit()
        return ana.id, unsubscribe_token(ana.id)


def contact(app, contact_id):
    with app.app_context():
        return db.session().get(Contact, contact_id)


def test_the_page_shows_topics_but_not_hidden_lists(app, client):
    _contact_id, token = setup_person(app)
    page = text(client.get(f"/u/{token}"))
    assert "an•••@example.com" in page and "Season News" in page and "Workshops" in page
    assert "Hidden Helpers" not in page


def test_a_changed_token_finds_nobody(app, client):
    contact_id, token = setup_person(app)
    forged = token.replace(f"{contact_id}.", f"{contact_id + 1}.", 1)
    assert client.get(f"/u/{forged}").status_code == 404
    assert client.get("/u/garbage").status_code == 404


def test_leave_one_topic(app, client):
    contact_id, token = setup_person(app)
    with app.app_context():
        workshops = db.session().scalar(select(ContactList.id).where(ContactList.name == "Workshops"))
    post(client, f"/u/{token}", {"action": "topics", "keep": str(workshops)})
    with app.app_context():
        names = {found.name for found in audience.lists_of(db.session(), contact_id)}
    assert names == {"Workshops", "Hidden Helpers"}
    assert contact(app, contact_id).status == "subscribed"


def test_unsubscribe_and_come_back(app, client):
    contact_id, token = setup_person(app)
    response = post(client, f"/u/{token}", {"action": "all"}, follow_redirects=True)
    assert "You're unsubscribed" in text(response)
    assert contact(app, contact_id).status == "unsubscribed"
    post(client, f"/u/{token}", {"action": "resubscribe"})
    again = contact(app, contact_id)
    assert again.status == "subscribed" and again.consent_note == "Subscribed again on the unsubscribe page"


def test_one_click_needs_no_form_token(app, client):
    contact_id, token = setup_person(app)
    # A mail service posts with no session and no CSRF token.
    response = client.post(f"/u/{token}/one-click", data={"List-Unsubscribe": "One-Click"})
    assert response.status_code == 200
    assert contact(app, contact_id).status == "unsubscribed"
    with app.app_context():
        assert db.session().scalar(select(ListMember.contact_id)) == contact_id  # lists untouched


def test_the_normal_page_still_needs_the_form_token(app, client):
    contact_id, token = setup_person(app)
    response = client.post(f"/u/{token}", data={"action": "all"})
    assert response.status_code == 400
    assert contact(app, contact_id).status == "subscribed"
