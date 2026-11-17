"""CSV import: Upload, Map Columns, Review, Done."""
import io

from sqlalchemy import select

from oncue import audience, db
from oncue.dates import utcnow
from oncue.models import Contact, ContactList, CsvImport

from .conftest import post, text, words

CSV = (
    "Email,First Name,Surname,City,Tags,Time zone\n"
    "ana@example.com,Ana,Santos,Cebu,\"member, donor\",Asia/Manila\n"
    "ANA@example.com,Ana,Again,Cebu,,\n"
    "not an email,X,Y,Z,,\n"
    "ben@example.com,Ben,Cruz,Davao,,Mars/Base\n"
    "carla@example.com,Carla,Reyes,Manila,,\n"
    "\n"
)


def upload(client, data=CSV, name="people.csv"):
    return post(client, "/audience/import", {"file": (io.BytesIO(data.encode("utf-8-sig")), name)},
                content_type="multipart/form-data")


def import_id(app):
    with app.app_context():
        return db.session().scalar(select(CsvImport.id).order_by(CsvImport.id.desc()))


def test_the_whole_import(app, owner):
    client, workspace_id = owner
    with app.app_context():
        s = db.session()
        # Carla is already there and unsubscribed; Ben's address is on the do-not-send list.
        carla = Contact(workspace_id=workspace_id, email="carla@example.com", fields={"plan": "Gold"})
        s.add(carla)
        s.flush()
        audience.unsubscribe(s, carla, utcnow())
        audience.suppress(s, workspace_id, "ben@example.com", "bounced", utcnow())
        s.add(ContactList(workspace_id=workspace_id, name="Season"))
        s.commit()
        list_id = s.scalar(select(ContactList.id))
    response = upload(client)
    assert response.status_code == 302 and response.headers["Location"].endswith("/map")
    page = text(client.get(response.headers["Location"]))
    # The first guess: Email, First Name and Tags are recognised; City becomes a custom field.
    assert '<option value="email" selected>' in page and '<option value="tags" selected>' in page
    mapping = {"column_0": "email", "column_1": "first_name", "column_2": "last_name", "column_3": "field",
               "column_4": "tags", "column_5": "time_zone", "list_id": str(list_id)}
    no_consent = post(client, f"/audience/import/{import_id(app)}/map", mapping)
    assert no_consent.status_code == 400 and "Only import people who agreed" in text(no_consent)
    to_review = post(client, f"/audience/import/{import_id(app)}/map", {**mapping, "consent": "yes"})
    review = words(client.get(to_review.headers["Location"]))
    assert "New 2" in review and "Already There 1" in review and "1 without a usable email, 1 repeated" in review
    assert "1 person in the file unsubscribed before" in review and "do-not-send list" in review
    assert "1 time zone wasn't recognised" in review
    with app.app_context():
        assert db.session().scalar(select(Contact).where(Contact.email == "ana@example.com")) is None  # nothing yet
    done = post(client, f"/audience/import/{import_id(app)}/review", follow_redirects=True)
    assert "people.csv Is In" in text(done)
    with app.app_context():
        s = db.session()
        ana = s.scalar(select(Contact).where(Contact.email == "ana@example.com"))
        assert (ana.first_name, ana.last_name, ana.fields, ana.time_zone) == ("Ana", "Santos", {"city": "Cebu"}, "Asia/Manila")
        assert ana.tag_names == ["donor", "member"] and ana.consent_note == "CSV import: people.csv"
        carla = s.scalar(select(Contact).where(Contact.email == "carla@example.com"))
        assert carla.status == "unsubscribed"  # an import never subscribes someone again
        assert carla.fields == {"plan": "Gold", "city": "Manila"}
        ben = s.scalar(select(Contact).where(Contact.email == "ben@example.com"))
        assert ben.time_zone is None
        members = audience.lists(s, workspace_id)[0][1]
        assert members == 3
        assert s.scalar(select(CsvImport)).rows == []  # the file's rows are let go


def test_the_email_column_is_needed(app, owner):
    client, _ = owner
    upload(client)
    response = post(client, f"/audience/import/{import_id(app)}/map", {"column_0": "skip", "consent": "yes"})
    assert "Choose which column has the email address." in text(response)


def test_bad_files_are_explained(app, owner):
    client, _ = owner
    assert "no rows" in text(upload(client, "Email\n"))
    assert "That file is empty." in text(upload(client, ""))


def test_semicolons_and_excel_encoding_work(app, owner):
    client, _ = owner
    data = "Email;Name\njos\xe9@example.com;Jos\xe9\n".encode("cp1252")
    response = post(client, "/audience/import", {"file": (io.BytesIO(data), "excel.csv")},
                    content_type="multipart/form-data")
    assert response.status_code == 302
    with app.app_context():
        imp = db.session().scalar(select(CsvImport))
        assert imp.header == ["Email", "Name"] and imp.rows == [["jos\xe9@example.com", "Jos\xe9"]]
