"""Settings for sending: which mailer, how fast, how many a day, per-domain
limits, and how difficult the practice mail server is."""
import re

from flask import current_app
from sqlalchemy import delete, select

from . import mailers
from .models import DomainLimit
from .views import settings

RATE_MIN, RATE_MAX = 0.2, 50
CAP_MAX = 100_000
DOMAIN = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$")
PRACTICE_FIELDS = ("practice_busy", "practice_timeout", "practice_bounce", "practice_slow")


def _number(form, name, kind=float):
    try:
        return kind(form.get(name, "").strip())
    except ValueError:
        return None


def save_sending(s, workspace, form):
    errors = {}
    rate = _number(form, "send_rate")
    cap = _number(form, "daily_cap", int)
    mailer = form.get("mailer", mailers.PRACTICE)
    if rate is None or not RATE_MIN <= rate <= RATE_MAX:
        errors["send_rate"] = f"Use a number from {RATE_MIN:g} to {RATE_MAX:g}."
    if cap is None or not 1 <= cap <= CAP_MAX:
        errors["daily_cap"] = f"Use a whole number from 1 to {CAP_MAX:,}."
    if mailer not in (mailers.PRACTICE, mailers.BREVO):
        errors["mailer"] = "Choose a mailer."
    elif mailer == mailers.BREVO and (workspace.is_demo or not current_app.config.get("BREVO_API_KEY")):
        errors["mailer"] = ("The demo only sends to the practice mail server." if workspace.is_demo
                            else "Set BREVO_API_KEY on the server first (docs/DEPLOY.md).")
    if errors:
        return errors, None
    workspace.send_rate, workspace.daily_cap, workspace.mailer = rate, cap, mailer
    return {}, "The sending settings are saved."


def save_practice(s, workspace, form):
    values = {name: _number(form, name, int) for name in PRACTICE_FIELDS}
    if any(value is None or not 0 <= value <= 50 for value in values.values()):
        return {"practice": "Use whole percentages from 0 to 50."}, None
    if values["practice_busy"] + values["practice_timeout"] + values["practice_bounce"] > 90:
        return {"practice": "Busy, timeouts and bounces together can be at most 90%."}, None
    for name, value in values.items():
        setattr(workspace, name, value)
    return {}, "The practice mail server's dials are set."


def parse_domains(text):
    """'gmail.com 60' per line -> ({domain: per_minute}, error)."""
    limits = {}
    for number, line in enumerate((text or "").splitlines(), start=1):
        parts = line.replace(",", " ").split()
        if not parts:
            continue
        if len(parts) != 2 or not parts[1].isdigit():
            return None, f"Line {number}: write a domain and a number, like: gmail.com 60"
        domain, per_minute = parts[0].lower().lstrip("@"), int(parts[1])
        if not DOMAIN.match(domain):
            return None, f"Line {number}: \"{parts[0]}\" isn't a domain."
        if not 1 <= per_minute <= 10_000:
            return None, f"Line {number}: use 1 to 10,000 emails a minute."
        limits[domain] = per_minute
    return limits, None


def save_domains(s, workspace, form):
    limits, error = parse_domains(form.get("domains", ""))
    if error:
        return {"domains": error}, None
    s.execute(delete(DomainLimit).where(DomainLimit.workspace_id == workspace.id))
    s.add_all(DomainLimit(workspace_id=workspace.id, domain=domain, per_minute=per_minute)
              for domain, per_minute in sorted(limits.items()))
    return {}, "The domain limits are saved." if limits else "There are no domain limits now."


def page_data(s, workspace):
    rows = s.scalars(select(DomainLimit).where(DomainLimit.workspace_id == workspace.id)
                     .order_by(DomainLimit.domain)).all()
    return {"settings_sections": ["_settings_sending.html"],
            "domain_text": "\n".join(f"{row.domain} {row.per_minute}" for row in rows),
            "brevo_ready": bool(current_app.config.get("BREVO_API_KEY")) and not workspace.is_demo,
            "mailer_labels": mailers.LABELS}


def init_app(app):
    settings.actions.update(sending=save_sending, practice=save_practice, domains=save_domains)
    if page_data not in settings.extras:
        settings.extras.append(page_data)
