"""CSV import in four steps: Upload, Map Columns, Review, Done.

The rows wait in the csv_imports table between steps. Review counts exactly
what Done will do, without changing anything, so there are no surprises:

- new contacts, and contacts that are already there (their details are updated);
- rows without a usable email (skipped), and repeats of an email in the file;
- addresses on the do-not-send list, and people who unsubscribed. Both are
  kept as they are: an import NEVER subscribes someone again. Only the person
  can do that, on the unsubscribe page.
"""
import csv
import io
from datetime import timedelta

from sqlalchemy import delete, select

from . import audience
from .dates import is_zone
from .models import Contact, CsvImport, Suppression

MAX_ROWS = 20_000
MAX_COLUMNS = 40
TARGETS = [
    ("email", "Email"),
    ("first_name", "First name"),
    ("last_name", "Last name"),
    ("time_zone", "Time zone"),
    ("tags", "Tags (comma separated)"),
    ("field", "A custom field"),
    ("skip", "Don't import"),
]
GUESSES = {
    "email": ("email", "e-mail", "email address", "mail"),
    "first_name": ("first name", "firstname", "first", "given name"),
    "last_name": ("last name", "lastname", "last", "surname", "family name"),
    "time_zone": ("time zone", "timezone", "tz"),
    "tags": ("tags", "tag", "labels"),
}


def parse(data, filename):
    """The file's header and rows, or (None, None, problem)."""
    if not data.lstrip(b"\xef\xbb\xbf").strip():  # nothing, or only Excel's byte-order mark
        return None, None, "That file is empty."
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = data.decode("cp1252", errors="replace")  # Excel on Windows often saves this way
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    reader = csv.reader(io.StringIO(text), dialect)
    try:
        header = [" ".join(cell.split()) for cell in next(reader)]
        rows = []
        for row in reader:
            if not any(cell.strip() for cell in row):
                continue
            if len(rows) >= MAX_ROWS:
                return None, None, f"That file has more than {MAX_ROWS:,} rows. Split it into smaller files."
            rows.append([cell.strip() for cell in row[:len(header)]] + [""] * (len(header) - len(row)))
    except (StopIteration, csv.Error):
        return None, None, "That doesn't look like a CSV file. Save it as CSV (comma separated) and try again."
    if not header or len(header) > MAX_COLUMNS:
        return None, None, f"A CSV file needs a first row of column names (at most {MAX_COLUMNS})."
    if not rows:
        return None, None, "That file has column names but no rows."
    return header, rows, None


def guess(header):
    """A first guess at what each column is."""
    mapping = {}
    taken = set()
    for index, name in enumerate(header):
        lowered = name.lower()
        target = next((key for key, names in GUESSES.items() if lowered in names and key not in taken), None)
        if target is None:
            target = "field" if audience.field_key(name) else "skip"
        else:
            taken.add(target)
        mapping[str(index)] = target
    return mapping


def check_mapping(header, form):
    mapping = {}
    for index in range(len(header)):
        target = form.get(f"column_{index}", "skip")
        mapping[str(index)] = target if target in dict(TARGETS) else "skip"
    chosen = [target for target in mapping.values() if target not in ("field", "skip")]
    if "email" not in chosen:
        return mapping, "Choose which column has the email address."
    if len(chosen) != len(set(chosen)):
        return mapping, "Each kind of column can be chosen once (except custom fields)."
    for index, target in mapping.items():
        if target == "field" and audience.field_key(header[int(index)]) is None:
            return mapping, (f"The column \"{header[int(index)]}\" can't be a custom field name. "
                             f"Choose \"Don't import\" for it.")
    return mapping, None


def _rows(imp):
    """Each row as {target: value} following the mapping; custom fields under 'fields'."""
    for row in imp.rows:
        values = {"fields": {}}
        for index, target in imp.mapping.items():
            cell = row[int(index)] if int(index) < len(row) else ""
            if target == "field":
                if cell:
                    values["fields"][audience.field_key(imp.header[int(index)])] = cell[:audience.FIELD_VALUE_MAX]
            elif target != "skip":
                values[target] = cell
        values["email"] = audience.clean_email(values.get("email"))
        yield values


def plan(s, imp):
    """What Done would do: {"new": [...rows], "existing": [...], counts...}. Changes nothing."""
    existing = {contact.email: contact for contact in s.scalars(
        select(Contact).where(Contact.workspace_id == imp.workspace_id))}
    blocked = set(s.scalars(select(Suppression.email).where(Suppression.workspace_id == imp.workspace_id)))
    result = {"new": [], "existing": [], "invalid": 0, "repeated": 0, "do_not_send": 0, "unsubscribed": 0,
              "bad_zone": 0}
    seen = set()
    for values in _rows(imp):
        if audience.email_problem(values["email"]):
            result["invalid"] += 1
            continue
        if values["email"] in seen:
            result["repeated"] += 1
            continue
        seen.add(values["email"])
        if values.get("time_zone") and not is_zone(values["time_zone"]):
            values["time_zone"] = ""
            result["bad_zone"] += 1
        if values["email"] in blocked:
            result["do_not_send"] += 1
        contact = existing.get(values["email"])
        if contact is None:
            result["new"].append(values)
        else:
            if contact.status == audience.UNSUBSCRIBED:
                result["unsubscribed"] += 1
            result["existing"].append((contact, values))
    return result


def summary(result):
    return {key: (len(value) if isinstance(value, list) else value) for key, value in result.items()}


def apply(s, imp, moment):
    """Done: add and update the contacts, tag them, put them on the chosen list."""
    result = plan(s, imp)
    contact_ids = []
    note = f"CSV import: {imp.filename}"[:200]
    for values in result["new"]:
        contact = Contact(workspace_id=imp.workspace_id, email=values["email"],
                          first_name=values.get("first_name", "")[:audience.NAME_MAX],
                          last_name=values.get("last_name", "")[:audience.NAME_MAX],
                          time_zone=values.get("time_zone") or None, fields=values["fields"],
                          consent_at=moment, consent_note=note, created_at=moment, updated_at=moment)
        s.add(contact)
        s.flush()
        audience.set_tags(contact, audience.clean_tags(values.get("tags", "")))
        contact_ids.append(contact.id)
    for contact, values in result["existing"]:
        for key in ("first_name", "last_name"):
            if values.get(key):
                setattr(contact, key, values[key][:audience.NAME_MAX])
        if values.get("time_zone"):
            contact.time_zone = values["time_zone"]
        if values["fields"]:
            contact.fields = {**(contact.fields or {}), **values["fields"]}
        audience.add_tags(contact, audience.clean_tags(values.get("tags", "")))
        contact.updated_at = moment
        contact_ids.append(contact.id)
    if imp.list_id:
        audience.add_to_list(s, imp.list_id, contact_ids, moment)
    imp.result = summary(result)
    imp.status = "done"
    imp.rows = []  # the file's rows aren't needed any more
    return imp.result


def purge(s, moment):
    """Imports left half-way are deleted after a day (tick.py)."""
    return s.execute(delete(CsvImport).where(CsvImport.created_at < moment - timedelta(days=1))).rowcount
