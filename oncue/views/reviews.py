"""Reviews: the numbers across shows, for a period."""
from datetime import timedelta

from flask import Blueprint, g, render_template, request

from .. import charts, db, reviews
from ..auth import login_required
from ..dates import utcnow

bp = Blueprint("reviews", __name__, url_prefix="/reviews")


@bp.before_request
@login_required
def must_log_in():
    pass


@bp.get("/")
def index():
    s = db.session()
    days = request.args.get("days", 30, type=int)
    if days not in reviews.PERIODS:
        days = 30
    end = utcnow()
    start = end - timedelta(days=days)
    numbers = reviews.funnel_numbers(s, g.workspace.id, start, end)
    points, weekly = reviews.opens_over_time(s, g.workspace, start, end)
    funnel = charts.funnel([("Sent", numbers["sent"]), ("Delivered", numbers["delivered"]),
                            ("Opened", numbers["opened"]), ("Clicked", numbers["clicked"])])
    return render_template("reviews.html", days=days, periods=reviews.PERIODS, numbers=numbers, funnel=funnel,
                           opens=charts.columns(points), opens_points=points, weekly=weekly,
                           bounces=reviews.bounce_reasons(s, g.workspace.id, start, end),
                           links=reviews.top_links(s, g.workspace.id, start, end),
                           rows=reviews.show_rows(s, g.workspace.id, start, end))
