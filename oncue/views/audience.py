"""Audience: contacts, lists, segments, the do-not-send list and CSV import.

Every query here is filtered by g.workspace.id, and every id from the address
goes through a get_*() that returns None (so a 404) for another workspace's row.
"""
from flask import Blueprint, abort, flash, g, redirect, render_template, request, url_for
from sqlalchemy import func, or_, select

from .. import audience, csv_import, db, segments
from ..auth import login_required
from ..dates import utcnow
from ..models import Contact, ContactList, ContactTag, CsvImport, ListMember, Segment, Suppression
from .settings import zone_choices

bp = Blueprint("audience", __name__, url_prefix="/audience")
PAGE_SIZE = 50
# Extra panels on a contact's page (later stages add the emails they got): function(s, contact) -> dict
contact_extras = []


@bp.before_request
@login_required
def must_log_in():
    pass


def counts(s):
    workspace_id = g.workspace.id
    count = lambda model: s.scalar(select(func.count()).select_from(model).where(model.workspace_id == workspace_id))  # noqa: E731
    return {"contacts": count(Contact), "lists": count(ContactList), "segments": count(Segment),
            "suppressions": count(Suppression)}


def render(template, **context):
    s = db.session()
    return render_template(template, tab_counts=counts(s), **context)


def contact_or_404(s, contact_id):
    contact = audience.get(s, g.workspace.id, contact_id)
    if contact is None:
        abort(404)
    return contact


def status_of(s, contact):
    return "do_not_send" if audience.is_suppressed(s, g.workspace.id, contact.email) else contact.status


# ---------- Contacts ----------

@bp.get("/")
def contacts():
    s = db.session()
    workspace_id = g.workspace.id
    q = request.args.get("q", "").strip()[:100]
    list_id = request.args.get("list", type=int)
    tag = request.args.get("tag", "").strip()
    status = request.args.get("status", "")
    page = max(request.args.get("page", 1, type=int), 1)
    query = select(Contact).where(Contact.workspace_id == workspace_id)
    if q:
        like = f"%{segments._like(q.lower())}%"
        query = query.where(or_(func.lower(Contact.email).like(like, escape="\\"),
                                func.lower(Contact.first_name + " " + Contact.last_name).like(like, escape="\\")))
    if list_id:
        query = query.where(Contact.id.in_(select(ListMember.contact_id).where(ListMember.list_id == list_id)))
    if tag:
        query = query.where(Contact.id.in_(select(ContactTag.contact_id).where(ContactTag.tag == tag)))
    suppressed = audience.suppressed_clause(workspace_id)
    if status == "subscribed":
        query = query.where(Contact.status == audience.SUBSCRIBED, ~suppressed)
    elif status == "unsubscribed":
        query = query.where(Contact.status == audience.UNSUBSCRIBED)
    elif status == "do_not_send":
        query = query.where(suppressed)
    total = s.scalar(select(func.count()).select_from(query.subquery()))
    rows = s.execute(query.add_columns(suppressed).order_by(Contact.created_at.desc(), Contact.id.desc())
                     .limit(PAGE_SIZE).offset((page - 1) * PAGE_SIZE)).all()
    filters = sum(1 for value in (list_id, tag, status) if value)
    return render("audience_contacts.html", rows=rows, total=total, page=page, page_size=PAGE_SIZE, q=q,
                  list_id=list_id, tag=tag, status=status, filters=filters,
                  all_lists=audience.lists(s, workspace_id), all_tags=audience.all_tags(s, workspace_id))


@bp.route("/new", methods=["GET", "POST"])
def new_contact():
    s = db.session()
    errors, values = {}, {}
    if request.method == "POST":
        values, errors = audience.check_contact(s, g.workspace, request.form)
        if not errors:
            contact = audience.save_contact(s, g.workspace, values, utcnow())
            list_id = request.form.get("list_id", type=int)
            if list_id and audience.get_list(s, g.workspace.id, list_id):
                audience.add_to_list(s, list_id, [contact.id], utcnow())
            s.commit()
            flash(f"{contact.email} is in your audience.")
            return redirect(url_for("audience.contact", contact_id=contact.id))
    return render("contact_form.html", contact=None, errors=errors, values=values or request.form,
                  zones=zone_choices(), all_lists=audience.lists(s, g.workspace.id)), 400 if errors else 200


@bp.get("/<int:contact_id>")
def contact(contact_id):
    s = db.session()
    found = contact_or_404(s, contact_id)
    extras = {}
    for extra in contact_extras:
        extras.update(extra(s, found))
    member_of = audience.lists_of(s, found.id)
    other_lists = [row for row, _count in audience.lists(s, g.workspace.id) if row not in member_of]
    return render("contact.html", contact=found, status=status_of(s, found), member_of=member_of,
                  other_lists=other_lists, **extras)


@bp.route("/<int:contact_id>/edit", methods=["GET", "POST"])
def edit_contact(contact_id):
    s = db.session()
    found = contact_or_404(s, contact_id)
    errors, values = {}, None
    if request.method == "POST":
        values, errors = audience.check_contact(s, g.workspace, request.form, contact=found)
        if not errors:
            audience.save_contact(s, g.workspace, values, utcnow(), contact=found)
            s.commit()
            flash("The contact is saved.")
            return redirect(url_for("audience.contact", contact_id=found.id))
    return render("contact_form.html", contact=found, errors=errors, values=request.form if values else None,
                  zones=zone_choices(), all_lists=[]), 400 if errors else 200


@bp.post("/<int:contact_id>/delete")
def delete_contact(contact_id):
    s = db.session()
    found = contact_or_404(s, contact_id)
    email = found.email
    audience.delete_contact(s, found)
    s.commit()
    flash(f"{email} is no longer in your audience.")
    return redirect(url_for("audience.contacts"))


@bp.post("/<int:contact_id>/lists")
def contact_lists(contact_id):
    s = db.session()
    found = contact_or_404(s, contact_id)
    list_id = request.form.get("list_id", type=int)
    chosen = audience.get_list(s, g.workspace.id, list_id) if list_id else None
    if chosen is None:
        abort(404)
    if request.form.get("action") == "remove":
        audience.remove_from_list(s, chosen.id, found.id)
        flash(f"Taken off {chosen.name}.")
    else:
        audience.add_to_list(s, chosen.id, [found.id], utcnow())
        flash(f"Added to {chosen.name}.")
    s.commit()
    return redirect(url_for("audience.contact", contact_id=found.id))


@bp.post("/<int:contact_id>/status")
def contact_status(contact_id):
    """The owner records that someone asked to stop (by phone, in person)."""
    s = db.session()
    found = contact_or_404(s, contact_id)
    if request.form.get("action") == "unsubscribe":
        audience.unsubscribe(s, found, utcnow())
        flash(f"{found.email} is unsubscribed and won't get your shows.")
    elif request.form.get("action") == "suppress":
        audience.suppress(s, g.workspace.id, found.email, "manual", utcnow(), "Added from the contact's page")
        flash(f"{found.email} is on the do-not-send list.")
    else:
        abort(400, "Choose something to change.")
    s.commit()
    return redirect(url_for("audience.contact", contact_id=found.id))


# ---------- Lists ----------

@bp.get("/lists")
def lists():
    s = db.session()
    return render("lists.html", lists=audience.lists(s, g.workspace.id))


def check_list(s, form, current=None):
    name = " ".join(form.get("name", "").split())
    description = form.get("description", "").strip()
    errors = {}
    if not name or len(name) > 80:
        errors["name"] = "Give the list a name of up to 80 characters."
    elif s.scalar(select(ContactList).where(ContactList.workspace_id == g.workspace.id, ContactList.name == name,
                                            ContactList.id != (current.id if current else 0))):
        errors["name"] = "You already have a list with that name."
    if len(description) > 300:
        errors["description"] = "Use at most 300 characters."
    return {"name": name, "description": description, "is_topic": form.get("is_topic") == "yes"}, errors


@bp.route("/lists/new", methods=["GET", "POST"])
def new_list():
    s = db.session()
    errors = {}
    if request.method == "POST":
        values, errors = check_list(s, request.form)
        if not errors:
            found = ContactList(workspace_id=g.workspace.id, created_at=utcnow(), **values)
            s.add(found)
            s.commit()
            flash(f"The list {found.name} is ready.")
            return redirect(url_for("audience.list_page", list_id=found.id))
    return render("list_form.html", found=None, errors=errors, form=request.form), 400 if errors else 200


@bp.get("/lists/<int:list_id>")
def list_page(list_id):
    s = db.session()
    found = audience.get_list(s, g.workspace.id, list_id) or abort(404)
    page = max(request.args.get("page", 1, type=int), 1)
    query = (select(Contact).join(ListMember, ListMember.contact_id == Contact.id)
             .where(ListMember.list_id == found.id))
    total = s.scalar(select(func.count()).select_from(query.subquery()))
    members = s.execute(query.add_columns(audience.suppressed_clause(g.workspace.id))
                        .order_by(ListMember.added_at.desc(), Contact.id.desc())
                        .limit(PAGE_SIZE).offset((page - 1) * PAGE_SIZE)).all()
    reachable = audience.audience_count(s, g.workspace, "list", found.id)
    return render("list.html", found=found, members=members, total=total, page=page, page_size=PAGE_SIZE,
                  reachable=reachable)


@bp.route("/lists/<int:list_id>/edit", methods=["GET", "POST"])
def edit_list(list_id):
    s = db.session()
    found = audience.get_list(s, g.workspace.id, list_id) or abort(404)
    errors = {}
    if request.method == "POST":
        values, errors = check_list(s, request.form, found)
        if not errors:
            for key, value in values.items():
                setattr(found, key, value)
            s.commit()
            flash("The list is saved.")
            return redirect(url_for("audience.list_page", list_id=found.id))
    return render("list_form.html", found=found, errors=errors, form=request.form), 400 if errors else 200


@bp.post("/lists/<int:list_id>/delete")
def delete_list(list_id):
    s = db.session()
    found = audience.get_list(s, g.workspace.id, list_id) or abort(404)
    name = found.name
    s.delete(found)
    s.commit()
    flash(f"The list {name} is deleted. The people on it are still in your audience.")
    return redirect(url_for("audience.lists"))


@bp.post("/lists/<int:list_id>/add")
def add_to_list(list_id):
    """Add people who are already in the audience, by email (one per line)."""
    s = db.session()
    found = audience.get_list(s, g.workspace.id, list_id) or abort(404)
    emails = {audience.clean_email(line) for line in request.form.get("emails", "").replace(",", "\n").splitlines()}
    emails.discard("")
    contacts_found = s.scalars(select(Contact.id).where(Contact.workspace_id == g.workspace.id,
                                                       Contact.email.in_(emails))).all() if emails else []
    added = audience.add_to_list(s, found.id, contacts_found, utcnow())
    s.commit()
    missing = len(emails) - len(contacts_found)
    message = f"Added {added} {'person' if added == 1 else 'people'} to {found.name}."
    if missing:
        message += f" {missing} {'address isn' if missing == 1 else 'addresses aren'}'t in your audience yet: add them first."
    flash(message, "error" if missing and not added else "message")
    return redirect(url_for("audience.list_page", list_id=found.id))


@bp.post("/lists/<int:list_id>/remove/<int:contact_id>")
def remove_from_list(list_id, contact_id):
    s = db.session()
    found = audience.get_list(s, g.workspace.id, list_id) or abort(404)
    member = contact_or_404(s, contact_id)
    audience.remove_from_list(s, found.id, member.id)
    s.commit()
    flash(f"{member.email} is off {found.name}.")
    return redirect(url_for("audience.list_page", list_id=found.id))


# ---------- Segments ----------

def segment_field_choices(s):
    choices = [(key, label) for key, label, _kind in segments.FIELDS]
    choices += [(f"field:{name}", segments.field_label(f"field:{name}"))
                for name in audience.field_names(s, g.workspace.id)]
    return choices


def list_names(s):
    return {row.id: row.name for row, _count in audience.lists(s, g.workspace.id)}


@bp.get("/segments")
def segments_page():
    s = db.session()
    found = s.scalars(select(Segment).where(Segment.workspace_id == g.workspace.id).order_by(Segment.name)).all()
    names = list_names(s)
    rows = [(segment, audience.audience_count(s, g.workspace, "segment", segment.id),
             [segments.describe(rule, names) for rule in segment.rules]) for segment in found]
    return render("segments.html", rows=rows)


def segment_form(found):
    s = db.session()
    errors, preview = {}, None
    names = list_names(s)
    form = request.form
    if request.method == "POST":
        name = " ".join(form.get("name", "").split())
        match = "any" if form.get("match") == "any" else "all"
        rules, rule_error = segments.clean_rules(form, names)
        if rule_error:
            errors["rules"] = rule_error
        if form.get("action") == "preview":
            if not rule_error:
                condition = audience.eligible(g.workspace.id) & segments.condition(rules, match, g.workspace.id)
                preview = {"count": s.scalar(select(func.count()).select_from(Contact).where(condition)),
                           "sample": s.scalars(select(Contact).where(condition).order_by(Contact.id).limit(5)).all()}
        else:
            if not name or len(name) > 80:
                errors["name"] = "Give the segment a name of up to 80 characters."
            elif s.scalar(select(Segment).where(Segment.workspace_id == g.workspace.id, Segment.name == name,
                                                Segment.id != (found.id if found else 0))):
                errors["name"] = "You already have a segment with that name."
            if not errors:
                if found is None:
                    found = Segment(workspace_id=g.workspace.id, created_at=utcnow())
                    s.add(found)
                found.name, found.match, found.rules = name, match, rules
                s.commit()
                flash(f"The segment {found.name} is saved.")
                return redirect(url_for("audience.segment_page", segment_id=found.id))
        rows = [{"field": form.get(f"rule_field_{i}", ""), "op": form.get(f"rule_op_{i}", ""),
                 "value": form.get(f"rule_value_{i}", "")} for i in range(segments.MAX_RULES)]
        values = {"name": form.get("name", ""), "match": match}
    else:
        rows = [dict(rule, value=names.get(int(rule["value"]), "")) if rule["field"] == "list" else rule
                for rule in found.rules] if found else [{"field": "tag", "op": "has", "value": ""}]
        values = {"name": found.name if found else "", "match": found.match if found else "all"}
    rows = (rows + [{"field": "", "op": "", "value": ""}] * segments.MAX_RULES)[:segments.MAX_RULES]
    return render("segment_form.html", found=found, errors=errors, values=values, rows=rows, preview=preview,
                  field_choices=segment_field_choices(s), ops=segments.OPS, list_names=names,
                  max_rules=segments.MAX_RULES), 400 if errors else 200


@bp.route("/segments/new", methods=["GET", "POST"])
def new_segment():
    return segment_form(None)


@bp.route("/segments/<int:segment_id>/edit", methods=["GET", "POST"])
def edit_segment(segment_id):
    found = audience.get_segment(db.session(), g.workspace.id, segment_id) or abort(404)
    return segment_form(found)


@bp.get("/segments/<int:segment_id>")
def segment_page(segment_id):
    s = db.session()
    found = audience.get_segment(s, g.workspace.id, segment_id) or abort(404)
    condition = audience.audience_condition(s, g.workspace, "segment", found.id)
    total = s.scalar(select(func.count()).select_from(Contact).where(condition))
    members = s.scalars(select(Contact).where(condition).order_by(Contact.id).limit(PAGE_SIZE)).all()
    names = list_names(s)
    return render("segment.html", found=found, total=total, members=members,
                  rules=[segments.describe(rule, names) for rule in found.rules])


@bp.post("/segments/<int:segment_id>/delete")
def delete_segment(segment_id):
    s = db.session()
    found = audience.get_segment(s, g.workspace.id, segment_id) or abort(404)
    name = found.name
    s.delete(found)
    s.commit()
    flash(f"The segment {name} is deleted. Nobody was removed from your audience.")
    return redirect(url_for("audience.segments_page"))


# ---------- The do-not-send list ----------

@bp.route("/do-not-send", methods=["GET", "POST"])
def do_not_send():
    s = db.session()
    error = None
    if request.method == "POST":
        email = audience.clean_email(request.form.get("email"))
        error = audience.email_problem(email)
        if not error:
            if audience.is_suppressed(s, g.workspace.id, email):
                error = "That address is already on the list."
            else:
                audience.suppress(s, g.workspace.id, email, "manual", utcnow(),
                                  request.form.get("note", "").strip()[:300])
                s.commit()
                flash(f"{email} won't get any email from you.")
                return redirect(url_for("audience.do_not_send"))
    return render("do_not_send.html", rows=audience.suppressions(s, g.workspace.id), error=error,
                  email=request.form.get("email", "")), 400 if error else 200


@bp.post("/do-not-send/<int:row_id>/remove")
def remove_suppression(row_id):
    s = db.session()
    row = s.get(Suppression, row_id)
    if row is None or row.workspace_id != g.workspace.id:
        abort(404)
    s.delete(row)
    s.commit()
    flash(f"{row.email} can get your emails again.")
    return redirect(url_for("audience.do_not_send"))


# ---------- CSV import ----------

def import_or_404(s, import_id):
    imp = s.get(CsvImport, import_id)
    if imp is None or imp.workspace_id != g.workspace.id:
        abort(404)
    return imp


@bp.route("/import", methods=["GET", "POST"])
def import_upload():
    s = db.session()
    error = None
    if request.method == "POST":
        upload = request.files.get("file")
        if upload is None or not upload.filename:
            error = "Choose a CSV file."
        else:
            header, rows, error = csv_import.parse(upload.read(), upload.filename)
            if not error:
                imp = CsvImport(workspace_id=g.workspace.id, filename=upload.filename[:200], header=header,
                                rows=rows, mapping=csv_import.guess(header), created_at=utcnow())
                s.add(imp)
                s.commit()
                return redirect(url_for("audience.import_map", import_id=imp.id))
    return render("import_upload.html", error=error, step=1), 400 if error else 200


@bp.route("/import/<int:import_id>/map", methods=["GET", "POST"])
def import_map(import_id):
    s = db.session()
    imp = import_or_404(s, import_id)
    if imp.status == "done":
        return redirect(url_for("audience.import_done", import_id=imp.id))
    error = None
    if request.method == "POST":
        mapping, error = csv_import.check_mapping(imp.header, request.form)
        imp.mapping = mapping
        list_id = request.form.get("list_id", type=int)
        imp.list_id = list_id if list_id and audience.get_list(s, g.workspace.id, list_id) else None
        if not error and request.form.get("consent") != "yes":
            error = "Only import people who agreed to get emails from you. Tick the box to say they did."
        if not error:
            imp.status = "review"
            s.commit()
            return redirect(url_for("audience.import_review", import_id=imp.id))
        s.commit()
    samples = [[row[index] for row in imp.rows[:3]] for index in range(len(imp.header))]
    return render("import_map.html", imp=imp, samples=samples, targets=csv_import.TARGETS, error=error,
                  all_lists=audience.lists(s, g.workspace.id), step=2), 400 if error else 200


@bp.route("/import/<int:import_id>/review", methods=["GET", "POST"])
def import_review(import_id):
    s = db.session()
    imp = import_or_404(s, import_id)
    if imp.status == "done":
        return redirect(url_for("audience.import_done", import_id=imp.id))
    if imp.status != "review":
        return redirect(url_for("audience.import_map", import_id=imp.id))
    if request.method == "POST":
        csv_import.apply(s, imp, utcnow())
        s.commit()
        return redirect(url_for("audience.import_done", import_id=imp.id))
    numbers = csv_import.summary(csv_import.plan(s, imp))
    chosen_list = audience.get_list(s, g.workspace.id, imp.list_id) if imp.list_id else None
    return render("import_review.html", imp=imp, numbers=numbers, chosen_list=chosen_list, step=3)


@bp.get("/import/<int:import_id>/done")
def import_done(import_id):
    s = db.session()
    imp = import_or_404(s, import_id)
    if imp.status != "done":
        return redirect(url_for("audience.import_map", import_id=imp.id))
    chosen_list = audience.get_list(s, g.workspace.id, imp.list_id) if imp.list_id else None
    return render("import_done.html", imp=imp, numbers=imp.result, chosen_list=chosen_list, step=4)
