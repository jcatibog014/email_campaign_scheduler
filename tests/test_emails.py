"""Turning a script into one person's email: merge fields, safety, the plain-text version."""
from types import SimpleNamespace

from oncue import emails

WORKSPACE = SimpleNamespace(sender_name="Teatro Luna", postal_address="12 Mabini St, Quezon City")


def item(body, subject="Hi {{first_name|there}}", preheader=""):
    return SimpleNamespace(subject=subject, preheader=preheader, body=body)


def person(first="Ana", last="Santos", email="ana@example.com", **fields):
    return SimpleNamespace(first_name=first, last_name=last, email=email, name=f"{first} {last}".strip(), fields=fields)


def test_merge_fields_and_fallbacks():
    subject, page, text = emails.render(item("Dear {{ first_name | friend }}, see you in {{city|Manila}}."),
                                        WORKSPACE, person(city="Cebu"))
    assert subject == "Hi Ana"
    assert "Dear Ana, see you in Cebu." in page and "Dear Ana, see you in Cebu." in text
    subject, page, _text = emails.render(item("Dear {{first_name|friend}} from {{city|Manila}}."), WORKSPACE,
                                         person(first=""))
    assert subject == "Hi there" and "Dear friend from Manila." in page


def test_a_name_can_never_become_html_or_markdown():
    _subject, page, _text = emails.render(item("Hello {{first_name}}!"), WORKSPACE,
                                          person(first="<script>alert(1)</script>*bold*"))
    assert "<script>" not in page and "&lt;script&gt;" in page
    assert "<em>bold</em>" not in page and "*bold*" in page


def test_the_body_is_cleaned():
    body = ("# Opening Night\n\n**Tickets** are [on sale](https://teatro.example/tickets).\n\n"
            "<script>alert(1)</script>\n\n[bad](javascript:alert(1))\n\n<img src=x onerror=alert(1)>")
    _subject, page, _text = emails.render(item(body), WORKSPACE, person())
    assert "<h1>Opening Night</h1>" in page and "<strong>Tickets</strong>" in page
    assert 'href="https://teatro.example/tickets"' in page
    # Raw HTML stays visible as harmless text; the javascript: link never becomes a link.
    assert "<script" not in page and "<img" not in page and 'href="javascript' not in page
    assert "&lt;script&gt;" in page


def test_links_can_be_wrapped_and_listed():
    body = "[Book](https://teatro.example/book?a=1&b=2) and [again](https://teatro.example/book?a=1&b=2), [mail](mailto:x@example.com)"
    wrapped = emails.body_html(body, {}, link_wrapper=lambda url: "https://track.example/c?u=" + url)
    assert wrapped.count('href="https://track.example/c?u=https://teatro.example/book?a=1&amp;b=2"') == 2
    assert emails.links_in(body) == ["https://teatro.example/book?a=1&b=2"]


def test_the_plain_text_version():
    _subject, _page, text = emails.render(item("# Hello {{first_name}}\n\n**Come** to [the show](https://t.example/s)."),
                                          WORKSPACE, person())
    assert text.startswith("Hello Ana\n\nCome to the show (https://t.example/s).")
    assert "12 Mabini St, Quezon City" in text and "Unsubscribe or choose topics: #" in text


def test_the_footer_and_preheader():
    _subject, page, _text = emails.render(item("Hi", preheader="Doors open at 7"), WORKSPACE, person(),
                                          unsubscribe_url="https://site.example/u/abc")
    assert "Doors open at 7" in page and 'href="https://site.example/u/abc"' in page
    assert "12 Mabini St, Quezon City" in page and "You get this because you agreed to hear from Teatro Luna" in page


def test_unknown_fields_are_typos_unless_they_have_a_fallback():
    texts = ["Hi {{frist_name}} from {{city}}", "{{plan|a member}} {{first_name}}"]
    assert emails.unknown_fields(texts, known=["city"]) == ["frist_name"]


def test_a_preview_without_a_person_uses_the_sample():
    subject, _page, _text = emails.render(item("x"), WORKSPACE, None, sample=True)
    assert subject == "Hi Ana"
