"""Stage: the home page. What is on Go now, what is on Standby next, and how
the last shows did."""
from flask import Blueprint, g, render_template

from ..auth import login_required

bp = Blueprint("stage", __name__)
# Parts of the home page that later modules fill: (name, function(s, workspace) -> dict).
panels = []


@bp.get("/stage")
@login_required
def home():
    from .. import db
    s = db.session()
    data = {}
    for _name, panel in panels:
        data.update(panel(s, g.workspace))
    return render_template("stage.html", **data)
