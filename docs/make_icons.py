r"""Make the logo and the app icons (oncue/static/icons/) from docs/logo/right-on-cue.svg.

The logo is an envelope centre stage under a spotlight, on a velvet tile.
This script:

- copies it to static/icons/logo.svg for the pages (the menu, the login panel);
- draws icon-32.png and favicon.ico for browser tabs;
- draws the app icons: icon-192/512.png keep the tile's round corners;
  apple-touch-icon.png is square (iPhones round the corners themselves);
  icon-maskable-512.png is square with the drawing a little smaller, because
  Android crops these into its own shape (a circle, a squircle...).

Phones and browsers want PNG pictures in fixed sizes, so this draws the SVG in
a browser at each size, through Playwright + Edge (Playwright isn't in
requirements.txt: borrow another project's):

    cd C:\Users\USER\Documents\Workspace\email_campaign_scheduler
    ..\expense_tracker\.venv\Scripts\python docs\make_icons.py
"""
import re
import struct
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).parent.parent
SOURCE = ROOT / "docs" / "logo" / "right-on-cue.svg"
ICONS = ROOT / "oncue" / "static" / "icons"


def inner():
    """The drawing without its <svg> wrapper and without the rounded tile's corners."""
    svg = SOURCE.read_text(encoding="utf-8")
    return re.search(r"<svg[^>]*>(.*)</svg>", svg, re.S).group(1)


def svg_for(how, size):
    body = inner()
    if how in ("square", "maskable"):
        body = body.replace('rx="14"', 'rx="0"', 1)
    if how == "maskable":
        # Android's safe zone is the middle 80%: shrink the stage, keep the velvet full size.
        defs = re.search(r"<defs>.*?</defs>", body, re.S).group(0)
        tile = re.search(r'<rect width="64" height="64"[^>]*/>', body).group(0)
        rest = body.replace(defs, "").replace(tile, "")
        body = f'{defs}{tile}<g transform="translate(32 32) scale(.8) translate(-32 -32)">{rest}</g>'
    return f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64" width="{size}" height="{size}">{body}</svg>'


PNGS = [
    ("icon-32.png", 32, "rounded"),
    ("icon-192.png", 192, "rounded"),
    ("icon-512.png", 512, "rounded"),
    ("apple-touch-icon.png", 180, "square"),
    ("icon-maskable-512.png", 512, "maskable"),
]
ICO_SIZES = (16, 32, 48)


def render(browser, svg, size):
    page = browser.new_page(viewport={"width": size, "height": size})
    page.set_content(f'<style>html, body {{ margin: 0; background: transparent; }} svg {{ display: block; }}</style>'
                     f'<div id="icon">{svg}</div>')
    png = page.locator("#icon svg").screenshot(omit_background=True)
    page.close()
    return png


def write_ico(path, pictures):
    """favicon.ico is a small header, a list, then the PNG files themselves."""
    header = struct.pack("<HHH", 0, 1, len(pictures))
    offset = 6 + 16 * len(pictures)
    entries, data = b"", b""
    for size, png in pictures:
        entries += struct.pack("<BBBBHHII", size, size, 0, 0, 1, 32, len(png), offset + len(data))
        data += png
    path.write_bytes(header + entries + data)


def main():
    ICONS.mkdir(parents=True, exist_ok=True)
    (ICONS / "logo.svg").write_text(SOURCE.read_text(encoding="utf-8"), encoding="utf-8")
    print("saved logo.svg")
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="msedge")
        for name, size, how in PNGS:
            (ICONS / name).write_bytes(render(browser, svg_for(how, size), size))
            print("saved", name)
        write_ico(ICONS / "favicon.ico", [(size, render(browser, svg_for("rounded", size), size)) for size in ICO_SIZES])
        print("saved favicon.ico")
        browser.close()


if __name__ == "__main__":
    main()
