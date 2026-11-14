"""Segments: a saved search turned into SQL.

A segment is a few rules ("city is Cebu", "tag is donor", "added in the last
30 days") joined by ALL (and) or ANY (or). condition() turns them into one
SQLAlchemy expression, so the database does the searching, and the same rules
work on SQLite and PostgreSQL. Custom fields live in a JSON column; SQLAlchemy's
`Contact.fields[name].as_string()` becomes json_extract() on SQLite and ->> on
PostgreSQL.
"""
from datetime import timedelta

from sqlalchemy import and_, exists, false, func, or_, true

from .dates import utcnow
from .models import Contact, ContactTag, ListMember

MAX_RULES = 6

# What a rule can look at: (key, label, kind). Custom fields are "field:<name>".
FIELDS = [
    ("email", "Email", "text"),
    ("first_name", "First name", "text"),
    ("last_name", "Last name", "text"),
    ("time_zone", "Time zone", "text"),
    ("tag", "Tag", "tag"),
    ("list", "List", "list"),
    ("added", "Added", "days"),
]
OPS = {
    "text": [("is", "is"), ("is_not", "is not"), ("contains", "contains"), ("starts_with", "starts with"),
             ("is_set", "is filled in"), ("not_set", "is empty")],
    "tag": [("has", "includes"), ("has_not", "doesn't include")],
    "list": [("on", "is on"), ("not_on", "isn't on")],
    "days": [("within", "in the last (days)"), ("older", "more than (days) ago")],
}
NEEDS_NO_VALUE = ("is_set", "not_set")


def field_kind(field):
    if field.startswith("field:"):
        return "text"
    return next((kind for key, _label, kind in FIELDS if key == field), None)


def field_label(field):
    if field.startswith("field:"):
        return field[6:].replace("_", " ").capitalize()
    return next((label for key, label, _kind in FIELDS if key == field), field)


def clean_rules(form, list_names):
    """Read rule rows rule_field_N / rule_op_N / rule_value_N. Returns (rules, error).
    Empty rows are skipped. A list rule's value is the list's name (stored as its id)."""
    ids_by_name = {name.lower(): list_id for list_id, name in list_names.items()}
    rules = []
    for index in range(MAX_RULES):
        field = form.get(f"rule_field_{index}", "").strip()
        op = form.get(f"rule_op_{index}", "").strip()
        value = " ".join(form.get(f"rule_value_{index}", "").split())[:200]
        if not field:
            continue
        kind = field_kind(field)
        if kind is None or op not in dict(OPS[kind]):
            return rules, f"Rule {index + 1}: choose what to look at and how to compare."
        if op not in NEEDS_NO_VALUE and not value:
            return rules, f"Rule {index + 1}: fill in a value."
        if kind == "days" and not (value.isdigit() and 0 < int(value) <= 3650):
            return rules, f"Rule {index + 1}: use a number of days from 1 to 3650."
        if kind == "list":
            if value.lower() not in ids_by_name:
                return rules, f"Rule {index + 1}: type the name of one of your lists."
            value = str(ids_by_name[value.lower()])
        rules.append({"field": field, "op": op, "value": "" if op in NEEDS_NO_VALUE else value})
    if not rules:
        return rules, "Add at least one rule."
    return rules, None


def _like(text):
    """Escape % and _ so "50%" means fifty percent, not "50 then anything"."""
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _text_column(field):
    if field.startswith("field:"):
        return Contact.fields[field[6:]].as_string()
    return {"email": Contact.email, "first_name": Contact.first_name, "last_name": Contact.last_name,
            "time_zone": Contact.time_zone}[field]


def rule_condition(rule, moment=None):
    field, op, value = rule["field"], rule["op"], rule.get("value", "")
    kind = field_kind(field)
    if kind == "tag":
        has = exists().where(ContactTag.contact_id == Contact.id, ContactTag.tag == value.lower())
        return has if op == "has" else ~has
    if kind == "list":
        on = exists().where(ListMember.list_id == int(value), ListMember.contact_id == Contact.id)
        return on if op == "on" else ~on
    if kind == "days":
        since = (moment or utcnow()) - timedelta(days=int(value))
        return Contact.created_at >= since if op == "within" else Contact.created_at < since
    column = _text_column(field)
    lowered = func.lower(func.coalesce(column, ""))
    if op == "is":
        return lowered == value.lower()
    if op == "is_not":
        return lowered != value.lower()
    if op == "contains":
        return lowered.like(f"%{_like(value.lower())}%", escape="\\")
    if op == "starts_with":
        return lowered.like(f"{_like(value.lower())}%", escape="\\")
    if op == "is_set":
        return lowered != ""
    if op == "not_set":
        return lowered == ""
    return false()


def condition(rules, match, workspace_id, moment=None):
    """One expression for all the rules. (workspace_id: the caller adds the
    workspace filter; it is here so a rule can never reach outside it.)"""
    parts = [rule_condition(rule, moment) for rule in rules or []]
    if not parts:
        return false()
    joined = or_(*parts) if match == "any" else and_(*parts)
    return and_(Contact.workspace_id == workspace_id, joined) if parts else true()


def describe(rule, list_names):
    """'City is Cebu', 'Tag includes donor', 'Added in the last 30 days'."""
    field, op, value = rule["field"], rule["op"], rule.get("value", "")
    kind = field_kind(field)
    op_label = dict(OPS.get(kind, [])).get(op, op)
    if kind == "list":
        value = list_names.get(int(value), "a deleted list") if str(value).isdigit() else value
    if kind == "days":
        return f"Added in the last {value} days" if op == "within" else f"Added more than {value} days ago"
    if op in NEEDS_NO_VALUE:
        return f"{field_label(field)} {op_label}"
    return f"{field_label(field)} {op_label} {value}"
