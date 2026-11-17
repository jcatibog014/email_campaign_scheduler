"""Audience: contacts, tags, lists, segments, the do-not-send list, and who may get a show."""
from datetime import timedelta

from sqlalchemy import select

from oncue import audience, db, segments
from oncue.dates import utcnow
from oncue.models import Contact, ContactList, ListMember, Segment, Workspace

from .conftest import browser, make_owner, post, text, words


def add_contact(client, email="ana@example.com", **extra):
    data = {"email": email, "first_name": "Ana", "last_name": "Santos", "tags": "member, Donor",
            "consent": "yes", "field_count": "5", **extra}
    return post(client, "/audience/new", data)


def contact_in(app, email):
    with app.app_context():
        contact = db.session().scalar(select(Contact).where(Contact.email == email))
        if contact is not None:
            contact.tag_names  # load the tags while the session is open
        return contact


def seed_people(app, workspace_id, people):
    """people: [(email, {field: value}, [tags], created days ago)]"""
    with app.app_context():
        s = db.session()
        for email, fields, tags, days in people:
            contact = Contact(workspace_id=workspace_id, email=email, fields=fields,
                              created_at=utcnow() - timedelta(days=days))
            s.add(contact)
            s.flush()
            audience.set_tags(contact, tags)
        s.commit()


def test_add_a_contact_with_tags_and_custom_fields(app, owner):
    client, _ = owner
    response = add_contact(client, "Ana@Example.com", field_name_0="Favourite Show", field_value_0="Noli",
                           field_name_1="city", field_value_1="Cebu")
    assert response.status_code == 302
    contact = contact_in(app, "ana@example.com")
    assert contact.tag_names == ["donor", "member"]
    assert contact.fields == {"favourite_show": "Noli", "city": "Cebu"}
    assert contact.consent_note == "Added by hand" and contact.status == "subscribed"
    page = text(client.get(response.headers["Location"]))
    assert "Favourite show" in page and "Noli" in page and "Subscribed" in page


def test_a_contact_needs_consent_and_a_unique_email(app, owner):
    client, _ = owner
    response = add_contact(client, consent="")
    assert response.status_code == 400 and "Only add people who agreed" in text(response)
    add_contact(client)
    again = add_contact(client, "ANA@example.com")
    assert again.status_code == 400 and "already has that email" in text(again)
    bad_field = add_contact(client, "ben@example.com", field_name_0="email", field_value_0="x")
    assert "can't be a field name" in text(bad_field)


def test_another_workspaces_contact_is_not_found(app, owner):
    client, _ = owner
    add_contact(client)
    contact_id = contact_in(app, "ana@example.com").id
    other_user, _ = make_owner(app, name="Other Owner", email="other@example.com")
    other = browser(app, other_user)
    assert other.get(f"/audience/{contact_id}").status_code == 404
    assert post(other, f"/audience/{contact_id}/delete").status_code == 404
    assert contact_in(app, "ana@example.com") is not None


def test_search_and_filter_contacts(app, owner):
    client, workspace_id = owner
    seed_people(app, workspace_id, [("ana@example.com", {}, ["member"], 1), ("ben@example.com", {}, [], 2),
                                    ("carla_50%@example.com", {}, ["member"], 3)])
    assert "ben@example.com" in text(client.get("/audience/?q=BEN"))
    assert "ana@example.com" not in text(client.get("/audience/?q=BEN"))
    by_tag = text(client.get("/audience/?tag=member"))
    assert "ana@example.com" in by_tag and "ben@example.com" not in by_tag
    # % and _ in a search are plain characters, not wildcards.
    assert "carla_50%" in text(client.get("/audience/?q=50%25"))
    assert "ana@example.com" not in text(client.get("/audience/?q=_"))


def test_lists_add_and_remove_people(app, owner):
    client, workspace_id = owner
    seed_people(app, workspace_id, [("ana@example.com", {}, [], 1), ("ben@example.com", {}, [], 1)])
    response = post(client, "/audience/lists/new", {"name": "Season Subscribers", "is_topic": "yes"})
    list_id = int(response.headers["Location"].rsplit("/", 1)[1])
    added = post(client, f"/audience/lists/{list_id}/add", {"emails": "ana@example.com\nnobody@example.com"},
                 follow_redirects=True)
    assert "Added 1 person to Season Subscribers. 1 address isn't in your audience yet" in words(added)
    with app.app_context():
        s = db.session()
        assert s.scalar(select(ListMember.contact_id).where(ListMember.list_id == list_id)) is not None
    duplicate = post(client, "/audience/lists/new", {"name": "Season Subscribers"})
    assert duplicate.status_code == 400 and "already have a list with that name" in text(duplicate)


def test_the_do_not_send_list_and_eligibility(app, owner):
    client, workspace_id = owner
    seed_people(app, workspace_id, [("ana@example.com", {}, [], 1), ("ben@example.com", {}, [], 1),
                                    ("carla@example.com", {}, [], 1)])
    post(client, "/audience/do-not-send", {"email": "BEN@example.com", "note": "Asked on the phone"})
    with app.app_context():
        s = db.session()
        carla = s.scalar(select(Contact).where(Contact.email == "carla@example.com"))
        audience.unsubscribe(s, carla, utcnow())
        s.commit()
        workspace = s.get(Workspace, workspace_id)
        # Only Ana: Ben is on the do-not-send list and Carla unsubscribed.
        assert audience.audience_count(s, workspace, "all", None) == 1
        assert [s.get(Contact, i).email for i in audience.audience_ids(s, workspace, "all", None)] == ["ana@example.com"]
    page = text(client.get("/audience/?status=do_not_send"))
    assert "ben@example.com" in page and "ana@example.com" not in page


def test_segments_turn_rules_into_sql(app, owner):
    client, workspace_id = owner
    seed_people(app, workspace_id, [
        ("ana@gmail.example", {"city": "Cebu"}, ["member"], 3),
        ("ben@yahoo.example", {"city": "cebu"}, [], 40),
        ("carla@gmail.example", {"city": "Davao"}, ["member"], 5),
        ("dan@gmail.example", {}, [], 1),
    ])
    with app.app_context():
        s = db.session()
        workspace = s.get(Workspace, workspace_id)

        def matching(rules, match="all"):
            condition = audience.eligible(workspace_id) & segments.condition(rules, match, workspace_id)
            return sorted(s.scalars(select(Contact.email).where(condition)))

        assert matching([{"field": "field:city", "op": "is", "value": "CEBU"}]) == ["ana@gmail.example", "ben@yahoo.example"]
        assert matching([{"field": "field:city", "op": "not_set", "value": ""}]) == ["dan@gmail.example"]
        assert matching([{"field": "email", "op": "contains", "value": "@gmail"},
                         {"field": "tag", "op": "has", "value": "member"}]) == ["ana@gmail.example", "carla@gmail.example"]
        assert matching([{"field": "added", "op": "within", "value": "4"},
                         {"field": "field:city", "op": "is", "value": "Davao"}], "any") == \
            ["ana@gmail.example", "carla@gmail.example", "dan@gmail.example"]
        assert matching([{"field": "tag", "op": "has_not", "value": "member"}]) == ["ben@yahoo.example", "dan@gmail.example"]
        assert workspace is not None


def test_make_a_segment_with_a_list_rule_by_name(app, owner):
    client, workspace_id = owner
    seed_people(app, workspace_id, [("ana@example.com", {}, [], 1), ("ben@example.com", {}, [], 1)])
    post(client, "/audience/lists/new", {"name": "Volunteers"})
    with app.app_context():
        s = db.session()
        found = s.scalar(select(ContactList))
        audience.add_to_list(s, found.id, [s.scalar(select(Contact.id).where(Contact.email == "ben@example.com"))], utcnow())
        s.commit()
    form = {"name": "Helpers", "match": "all", "rule_field_0": "list", "rule_op_0": "on", "rule_value_0": "volunteers"}
    preview = post(client, "/audience/segments/new", {**form, "action": "preview"})
    assert "1 person match" in words(preview) and "ben@example.com" in text(preview)
    saved = post(client, "/audience/segments/new", {**form, "action": "save"})
    assert saved.status_code == 302
    page = words(client.get(saved.headers["Location"]))
    assert "List is on Volunteers" in page and "ben@example.com" in page and "ana@example.com" not in page
    with app.app_context():
        assert db.session().scalar(select(Segment)).rules[0]["value"] == str(found.id)
    wrong = post(client, "/audience/segments/new", {**form, "rule_value_0": "Nope", "action": "save"})
    assert "type the name of one of your lists" in text(wrong)


def test_audience_choices_count_only_who_can_get_emails(app, owner):
    _client, workspace_id = owner
    seed_people(app, workspace_id, [("ana@example.com", {}, ["donor"], 1), ("ben@example.com", {}, ["donor"], 1)])
    with app.app_context():
        s = db.session()
        audience.suppress(s, workspace_id, "ben@example.com", "bounced", utcnow())
        s.commit()
        choices = dict(audience.audience_choices(s, s.get(Workspace, workspace_id)))
        assert choices["all"] == "Everyone subscribed (1)" and choices["tag:donor"] == "Tag: donor (1)"
        assert audience.parse_audience("list:7") == ("list", 7) and audience.parse_audience("nope") == (None, None)
