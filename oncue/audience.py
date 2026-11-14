"""The audience: contacts, their tags and lists, the do-not-send list, and
unsubscribing.

The one rule that matters most: an email only ever goes to a contact who is
SUBSCRIBED and NOT on the do-not-send list (eligible()). Every way of choosing
an audience (everyone, a list, a tag, a segment) goes through it, and the
sender checks it again just before sending (sending.py), because someone may
unsubscribe between the show starting and their email's turn.
"""
import re

from sqlalchemy import and_, delete, exists, func, select

from . import segments
from .auth import EMAIL_PATTERN
from .dates import is_zone
from .models import Contact, ContactList, ContactTag, ListMember, Segment, Suppression

SUBSCRIBED, UNSUBSCRIBED = "subscribed", "unsubscribed"
NAME_MAX = 80
TAG_MAX = 40
TAGS_PER_CONTACT = 20
FIELD_NAME = re.compile(r"[a-z][a-z0-9_]{0,39}")
FIELD_VALUE_MAX = 200
# Merge fields every contact has; custom fields can't take these names.
BUILT_IN_FIELDS = ("email", "first_name", "last_name", "name", "time_zone")


# ---------- Cleaning what people type ----------

def clean_email(text):
    return (text or "").strip().lower()


def email_problem(email):
    if not email:
        return "Enter an email address."
    if len(email) > 254 or not EMAIL_PATTERN.fullmatch(email):
        return "That doesn't look like an email address."
    return None


def clean_tags(text):
    """'Member, donor ,member' -> ['donor', 'member']: lower case, no repeats."""
    tags = set()
    for raw in re.split(r"[,;]", text or ""):
        tag = " ".join(raw.split()).lower()[:TAG_MAX]
        if tag:
            tags.add(tag)
    return sorted(tags)[:TAGS_PER_CONTACT]


def field_key(text):
    """'Favourite Show' -> 'favourite_show' (a custom field's name), or None."""
    key = re.sub(r"[^a-z0-9]+", "_", (text or "").strip().lower()).strip("_")
    if key and key[0].isdigit():
        key = "f_" + key
    key = key[:40]
    if not key or key in BUILT_IN_FIELDS or not FIELD_NAME.fullmatch(key):
        return None
    return key


# ---------- Contacts ----------

def find(s, workspace_id, email):
    return s.scalar(select(Contact).where(Contact.workspace_id == workspace_id, Contact.email == clean_email(email)))


def get(s, workspace_id, contact_id):
    """A contact of this workspace, or None (never someone else's)."""
    contact = s.get(Contact, contact_id)
    return contact if contact is not None and contact.workspace_id == workspace_id else None


def set_tags(contact, tags):
    wanted = set(tags)
    for tag in list(contact.tags):
        if tag.tag not in wanted:
            contact.tags.remove(tag)
    have = set(contact.tag_names)
    for tag in sorted(wanted - have):
        contact.tags.append(ContactTag(tag=tag))


def add_tags(contact, tags):
    set_tags(contact, sorted(set(contact.tag_names) | set(tags))[:TAGS_PER_CONTACT])


def check_contact(s, workspace, form, contact=None):
    """Read the contact form. Returns (values, errors)."""
    values = {
        "email": clean_email(form.get("email")),
        "first_name": " ".join(form.get("first_name", "").split())[:NAME_MAX + 1],
        "last_name": " ".join(form.get("last_name", "").split())[:NAME_MAX + 1],
        "time_zone": form.get("time_zone", "").strip(),
        "tags": clean_tags(form.get("tags", "")),
        "fields": {},
    }
    errors = {}
    if problem := email_problem(values["email"]):
        errors["email"] = problem
    else:
        other = find(s, workspace.id, values["email"])
        if other is not None and (contact is None or other.id != contact.id):
            errors["email"] = "Someone in your audience already has that email."
    for key in ("first_name", "last_name"):
        if len(values[key]) > NAME_MAX:
            errors[key] = f"Use at most {NAME_MAX} characters."
    if values["time_zone"] and not is_zone(values["time_zone"]):
        errors["time_zone"] = "Choose a time zone from the list, or leave it empty."
    # Custom fields come as field_name_N / field_value_N pairs.
    for index in range(int(form.get("field_count", 0) or 0)):
        name, value = form.get(f"field_name_{index}", ""), form.get(f"field_value_{index}", "").strip()
        if not name.strip() and not value:
            continue
        key = field_key(name)
        if key is None:
            errors["fields"] = (f"\"{name.strip()}\" can't be a field name. Use letters, numbers and spaces, "
                                f"and not {', '.join(BUILT_IN_FIELDS)}.")
        elif value:
            values["fields"][key] = value[:FIELD_VALUE_MAX]
    if form.get("consent") != "yes" and contact is None:
        errors["consent"] = "Only add people who agreed to get emails from you."
    return values, errors


def save_contact(s, workspace, values, moment, contact=None, consent_note="Added by hand"):
    if contact is None:
        contact = Contact(workspace_id=workspace.id, consent_at=moment, consent_note=consent_note,
                          created_at=moment, fields={})
        s.add(contact)
    contact.email = values["email"]
    contact.first_name, contact.last_name = values["first_name"], values["last_name"]
    contact.time_zone = values["time_zone"] or None
    contact.fields = dict(values["fields"])  # a new dict, so SQLAlchemy sees the change
    contact.updated_at = moment
    set_tags(contact, values["tags"])
    s.flush()
    return contact


def delete_contact(s, contact):
    s.delete(contact)


def lists_of(s, contact_id):
    return s.scalars(select(ContactList).join(ListMember, ListMember.list_id == ContactList.id)
                     .where(ListMember.contact_id == contact_id).order_by(ContactList.name)).all()


def all_tags(s, workspace_id):
    """[(tag, how many contacts)] for the filter menus."""
    rows = s.execute(select(ContactTag.tag, func.count()).join(Contact, Contact.id == ContactTag.contact_id)
                     .where(Contact.workspace_id == workspace_id).group_by(ContactTag.tag).order_by(ContactTag.tag))
    return rows.all()


def field_names(s, workspace_id, limit=2000):
    """Every custom field name used in the workspace (merge fields, segment rules)."""
    names = set()
    for fields in s.scalars(select(Contact.fields).where(Contact.workspace_id == workspace_id).limit(limit)):
        names.update((fields or {}).keys())
    return sorted(names)


# ---------- Lists ----------

def get_list(s, workspace_id, list_id):
    found = s.get(ContactList, list_id)
    return found if found is not None and found.workspace_id == workspace_id else None


def lists(s, workspace_id):
    """[(list, member count)]"""
    count = (select(func.count()).select_from(ListMember).where(ListMember.list_id == ContactList.id)
             .correlate(ContactList).scalar_subquery())
    return s.execute(select(ContactList, count).where(ContactList.workspace_id == workspace_id)
                     .order_by(ContactList.name)).all()


def add_to_list(s, list_id, contact_ids, moment):
    """Put these contacts on the list (those already on it stay as they are). Returns how many joined."""
    have = set(s.scalars(select(ListMember.contact_id).where(ListMember.list_id == list_id,
                                                             ListMember.contact_id.in_(contact_ids))))
    new = [contact_id for contact_id in dict.fromkeys(contact_ids) if contact_id not in have]
    s.add_all(ListMember(list_id=list_id, contact_id=contact_id, added_at=moment) for contact_id in new)
    return len(new)


def remove_from_list(s, list_id, contact_id):
    s.execute(delete(ListMember).where(ListMember.list_id == list_id, ListMember.contact_id == contact_id))


def get_segment(s, workspace_id, segment_id):
    found = s.get(Segment, segment_id)
    return found if found is not None and found.workspace_id == workspace_id else None


# ---------- The do-not-send list ----------

def suppressed_clause(workspace_id, email_column=Contact.email):
    """True when this address is on the workspace's do-not-send list."""
    return exists().where(Suppression.workspace_id == workspace_id, Suppression.email == email_column)


def is_suppressed(s, workspace_id, email):
    return s.scalar(select(suppressed_clause(workspace_id, clean_email(email))))


def suppress(s, workspace_id, email, reason, moment, note=""):
    """Put an address on the do-not-send list (once: a second bounce changes nothing)."""
    email = clean_email(email)
    row = s.scalar(select(Suppression).where(Suppression.workspace_id == workspace_id, Suppression.email == email))
    if row is None:
        row = Suppression(workspace_id=workspace_id, email=email, reason=reason, note=note[:300], created_at=moment)
        s.add(row)
        s.flush()
    return row


def suppressions(s, workspace_id):
    return s.scalars(select(Suppression).where(Suppression.workspace_id == workspace_id)
                     .order_by(Suppression.created_at.desc())).all()


# ---------- Unsubscribing ----------

def unsubscribe(s, contact, moment, from_list_ids=None):
    """Everything (from_list_ids None), or only some topic lists."""
    if from_list_ids is None:
        if contact.status != UNSUBSCRIBED:
            contact.status, contact.unsubscribed_at = UNSUBSCRIBED, moment
        return
    s.execute(delete(ListMember).where(ListMember.contact_id == contact.id, ListMember.list_id.in_(from_list_ids)))


def resubscribe(s, contact, moment):
    """Only when the person asks for it themselves, on the unsubscribe page."""
    contact.status, contact.unsubscribed_at, contact.consent_at = SUBSCRIBED, None, moment
    contact.consent_note = "Subscribed again on the unsubscribe page"


# ---------- Choosing an audience ----------

AUDIENCE_KINDS = ("all", "list", "segment", "tag")


def eligible(workspace_id):
    """Who may get an email at all: subscribed, and not on the do-not-send list."""
    return and_(Contact.workspace_id == workspace_id, Contact.status == SUBSCRIBED,
                ~suppressed_clause(workspace_id))


def audience_condition(s, workspace, kind, ref):
    """The condition for a show's audience, or None if the list/segment is gone."""
    base = eligible(workspace.id)
    if kind == "all":
        return base
    if kind in ("list", "segment"):
        ref = int(ref)  # shows keep the reference as text
    if kind == "list":
        if get_list(s, workspace.id, ref) is None:
            return None
        return and_(base, exists().where(ListMember.list_id == ref, ListMember.contact_id == Contact.id))
    if kind == "tag":
        return and_(base, exists().where(ContactTag.contact_id == Contact.id, ContactTag.tag == ref))
    if kind == "segment":
        segment = get_segment(s, workspace.id, ref)
        if segment is None:
            return None
        return and_(base, segments.condition(segment.rules, segment.match, workspace.id))
    return None


def audience_ids(s, workspace, kind, ref, after_id=0, limit=None):
    """The contact ids of an audience, in id order (the sender works through them in batches)."""
    condition = audience_condition(s, workspace, kind, ref)
    if condition is None:
        return []
    query = select(Contact.id).where(condition, Contact.id > after_id).order_by(Contact.id)
    if limit:
        query = query.limit(limit)
    return s.scalars(query).all()


def audience_count(s, workspace, kind, ref):
    condition = audience_condition(s, workspace, kind, ref)
    if condition is None:
        return 0
    return s.scalar(select(func.count()).select_from(Contact).where(condition))


def audience_choices(s, workspace):
    """[(value, label)] for the show builder: 'all', 'list:3', 'segment:2', 'tag:donor'."""
    choices = [("all", f"Everyone subscribed ({audience_count(s, workspace, 'all', None):,})")]
    for found, _members in lists(s, workspace.id):
        choices.append((f"list:{found.id}", f"List: {found.name} ({audience_count(s, workspace, 'list', found.id):,})"))
    for segment in s.scalars(select(Segment).where(Segment.workspace_id == workspace.id).order_by(Segment.name)):
        count = audience_count(s, workspace, "segment", segment.id)
        choices.append((f"segment:{segment.id}", f"Segment: {segment.name} ({count:,})"))
    for tag, _count in all_tags(s, workspace.id):
        choices.append((f"tag:{tag}", f"Tag: {tag} ({audience_count(s, workspace, 'tag', tag):,})"))
    return choices


def parse_audience(value):
    """'list:3' -> ('list', 3); 'tag:donor' -> ('tag', 'donor'); 'all' -> ('all', None)."""
    kind, _, ref = (value or "").partition(":")
    if kind == "all":
        return "all", None
    if kind in ("list", "segment") and ref.isdigit():
        return kind, int(ref)
    if kind == "tag" and ref:
        return "tag", ref
    return None, None


def audience_label(s, workspace, kind, ref):
    if kind == "all":
        return "Everyone subscribed"
    if kind == "list":
        found = get_list(s, workspace.id, int(ref))
        return f"List: {found.name}" if found else "A deleted list"
    if kind == "segment":
        found = get_segment(s, workspace.id, int(ref))
        return f"Segment: {found.name}" if found else "A deleted segment"
    return f"Tag: {ref}"
