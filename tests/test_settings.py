"""Settings: the workspace details, the time zone and the account."""
from oncue import db
from oncue.models import Workspace

from .conftest import post, text


def workspace(app, workspace_id):
    with app.app_context():
        return db.session().get(Workspace, workspace_id)


def test_save_the_workspace(app, owner):
    client, workspace_id = owner
    response = post(client, "/settings", {"action": "workspace", "name": "Maria's Theatre",
                                          "sender_name": "Maria at the Theatre", "sender_email": "Box@Example.com",
                                          "postal_address": "1 Stage Road, Manila", "time_zone": "Asia/Tokyo"})
    assert response.status_code == 302
    saved = workspace(app, workspace_id)
    assert (saved.name, saved.sender_email, saved.time_zone) == ("Maria's Theatre", "box@example.com", "Asia/Tokyo")


def test_bad_workspace_details_are_explained(app, owner):
    client, workspace_id = owner
    response = post(client, "/settings", {"action": "workspace", "name": "", "sender_name": "M",
                                          "sender_email": "not an email", "time_zone": "Mars/Base"})
    page = text(response)
    assert response.status_code == 400
    assert "Enter a name for the workspace." in page and "doesn't look like an email" in page
    assert "Choose a time zone from the list." in page
    assert workspace(app, workspace_id).time_zone == "Asia/Manila"


def test_the_settings_page_has_every_section(owner):
    client, _ = owner
    page = text(client.get("/settings"))
    for heading in ("Workspace", "Appearance", "Your Account", "Install the App", "Log Out"):
        assert f">{heading}<" in page, heading
    assert '<option value="Asia/Manila" selected>' in page


def test_the_page_shows_times_in_the_workspace_zone(app, ctx):
    from datetime import datetime

    from oncue.formatting import clock, nice_datetime
    moment = datetime(2026, 11, 16, 1, 0)  # 1:00 AM UTC
    assert clock(moment) == "9:00 AM"  # Manila is UTC+8
    assert clock(moment, "America/New_York") == "8:00 PM"
    assert nice_datetime(moment, "Asia/Manila").endswith("Nov 16, 9:00 AM")
