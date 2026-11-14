"""Turning a script into the email one person gets.

render(show_or_script, contact, ...) gives the subject, the HTML and the plain
text, with the merge fields filled in:

    Hi {{first_name|there}}!   ->   "Hi Ana!"  (or "Hi there!" when there is no first name)

How the HTML is made, and why in this order:
1. each merge tag is swapped for a plain placeholder (MERGE0FIELD...), so a
   name like "*Ana*" or "<b>" can't become Markdown or HTML;
2. the Markdown becomes HTML (markdown-it) and is cleaned by nh3: only safe
   tags, and links only to http, https and mailto;
3. the placeholders are swapped for the person's values, HTML-escaped;
4. links go through link_wrapper (click tracking adds itself there, stage 6);
5. it is put in the email layout, with the unsubscribe link and the sender's
   postal address at the bottom (anti-spam laws ask for both).

Email apps ignore style sheets, so the layout uses inline styles. That's fine
here: this HTML never runs in our pages, only in a mail app or a preview frame
with its own strict rules (views/scripts.py, preview_response).
"""
import html
import re

import nh3
from markdown_it import MarkdownIt

from .audience import BUILT_IN_FIELDS

MERGE_TAG = re.compile(r"\{\{\s*([a-zA-Z][a-zA-Z0-9_]*)\s*(?:\|([^{}]*))?\}\}")
SAMPLE = {"first_name": "Ana", "last_name": "Santos", "email": "ana@example.com", "name": "Ana Santos"}
ALLOWED_TAGS = {"p", "br", "strong", "em", "a", "ul", "ol", "li", "h1", "h2", "h3", "blockquote", "hr", "code",
                "pre", "img", "del"}
ALLOWED_ATTRIBUTES = {"a": {"href", "title"}, "img": {"src", "alt", "width", "height"}}
markdown = MarkdownIt("commonmark", {"html": False, "linkify": False, "typographer": True}).enable("strikethrough")


def values_for(contact, sample=False):
    """The merge values of one person (or Ana Santos, for a preview with nobody chosen)."""
    if contact is None:
        return dict(SAMPLE, city="Cebu") if sample else {}
    values = {"first_name": contact.first_name, "last_name": contact.last_name, "email": contact.email,
              "name": contact.name}
    for key, value in (contact.fields or {}).items():
        values.setdefault(key, value)
    return values


def merge_tags(text):
    """[(field, fallback or None)] used in a text."""
    return [(match.group(1).lower(), match.group(2)) for match in MERGE_TAG.finditer(text or "")]


def unknown_fields(texts, known):
    """Merge fields nobody in the audience has (likely typos), with no fallback."""
    known = set(known) | set(BUILT_IN_FIELDS)
    found = []
    for text in texts:
        for field, fallback in merge_tags(text):
            if field not in known and fallback is None and field not in found:
                found.append(field)
    return found


def fill(text, values):
    """Merge fields in plain text (the subject, the plain-text email)."""
    def one(match):
        value = values.get(match.group(1).lower())
        return str(value) if value not in (None, "") else (match.group(2) or "").strip()
    return MERGE_TAG.sub(one, text or "")


def body_html(body, values, link_wrapper=None):
    """Markdown -> clean HTML with the merge fields filled in (steps 1 to 4)."""
    placeholders = {}

    def hide(match):
        key = f"MERGE{len(placeholders)}FIELD"
        placeholders[key] = match
        return key

    hidden = MERGE_TAG.sub(hide, body or "")
    clean = nh3.clean(markdown.render(hidden), tags=ALLOWED_TAGS, attributes=ALLOWED_ATTRIBUTES,
                      url_schemes={"http", "https", "mailto"}, link_rel="noopener")
    for key, match in placeholders.items():
        clean = clean.replace(key, html.escape(fill(match.group(0), values)))
    if link_wrapper is not None:
        clean = re.sub(r'href="([^"]+)"', lambda m: f'href="{html.escape(link_wrapper(html.unescape(m.group(1))))}"', clean)
    return clean


def body_text(body, values):
    """The plain-text version: the Markdown with merge fields filled, links written out."""
    text = fill(body, values)
    text = re.sub(r"!\[([^\]]*)\]\([^)]*\)", r"\1", text)  # pictures: their description
    text = re.sub(r"\[([^\]]+)\]\(([^)\s]+)[^)]*\)", r"\1 (\2)", text)  # [words](url) -> words (url)
    text = re.sub(r"^#{1,6}\s*", "", text, flags=re.M)
    text = re.sub(r"(\*\*|__)(.+?)\1", r"\2", text)
    return text.strip()


def layout(inner, *, preheader="", sender_name="", postal_address="", unsubscribe_url="#", pixel_url=None):
    """The email around the body: inline styles, because mail apps ignore style sheets."""
    pixel = f'<img src="{html.escape(pixel_url)}" width="1" height="1" alt="" style="display:block;border:0">' \
        if pixel_url else ""
    hidden_preheader = (f'<div style="display:none;max-height:0;overflow:hidden;opacity:0">'
                        f'{html.escape(preheader)}</div>') if preheader else ""
    footer_address = f"{html.escape(postal_address)}<br>" if postal_address else ""
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(sender_name)}</title></head>
<body style="margin:0;padding:0;background:#f4efe7;font-family:Georgia,'Times New Roman',serif;color:#2b1d24">
{hidden_preheader}
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f4efe7;padding:24px 12px">
<tr><td align="center">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:600px;background:#ffffff;border-radius:8px;border-top:6px solid #6b1430">
<tr><td style="padding:22px 28px 0;font:700 15px/1.3 Georgia,serif;color:#6b1430;letter-spacing:.04em;text-transform:uppercase">{html.escape(sender_name)}</td></tr>
<tr><td style="padding:8px 28px 24px;font:16px/1.6 Georgia,'Times New Roman',serif;color:#2b1d24">{inner}</td></tr>
</table>
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:600px">
<tr><td style="padding:16px 28px;font:12px/1.5 Arial,sans-serif;color:#6b5a5f;text-align:center">
{footer_address}You get this because you agreed to hear from {html.escape(sender_name)}.
<a href="{html.escape(unsubscribe_url)}" style="color:#6b5a5f">Unsubscribe or choose topics</a>
</td></tr></table>
{pixel}
</td></tr></table>
</body></html>"""


def text_layout(text, *, sender_name="", postal_address="", unsubscribe_url="#"):
    lines = [text, "", "--", f"You get this because you agreed to hear from {sender_name}."]
    if postal_address:
        lines.append(postal_address)
    lines.append(f"Unsubscribe or choose topics: {unsubscribe_url}")
    return "\n".join(lines)


def render(item, workspace, contact=None, *, sample=False, unsubscribe_url="#", link_wrapper=None, pixel_url=None,
           subject=None):
    """(subject, html, text) for one person. item is a Show or a Script (subject, preheader, body)."""
    values = values_for(contact, sample=sample)
    inner = body_html(item.body, values, link_wrapper)
    footer = {"sender_name": workspace.sender_name, "postal_address": workspace.postal_address,
              "unsubscribe_url": unsubscribe_url}
    return (fill(subject if subject is not None else item.subject, values).strip(),
            layout(inner, preheader=fill(item.preheader, values), pixel_url=pixel_url, **footer),
            text_layout(body_text(item.body, values), **footer))


def links_in(body):
    """Every http(s) link in a body, in order, once (click tracking numbers them)."""
    found = []
    for url in re.findall(r'href="(https?://[^"]+)"', body_html(body, {})):
        url = html.unescape(url)
        if url not in found:
            found.append(url)
    return found
