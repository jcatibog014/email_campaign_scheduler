r"""Make the logo and the app icons (oncue/static/icons/) from the owner's drawing,
docs/logo/right-on-cue-spotlight-v2.svg: a gold spotlight shining on an envelope
with a gold seal. It has no background and is taller than wide (280 x 352).

This script:
- copies it to static/icons/logo.svg, unchanged, for the pages (the curtain menu
  and the login panel, where it stands on the velvet);
- draws every PNG icon with the drawing on a velvet tile (an app icon needs a
  solid square): icon-32/192/512.png and favicon.ico keep the tile's round
  corners; apple-touch-icon.png is square (iPhones round the corners
  themselves); icon-maskable-512.png is square with the drawing smaller,
  because Android crops these into its own shape (a circle, a squircle...).

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
SOURCE = ROOT / "docs" / "logo" / "right-on-cue-spotlight-v2.svg"
ICONS = ROOT / "oncue" / "static" / "icons"
VELVET = ('<linearGradient id="tile" x1="0" y1="0" x2="0" y2="1">'
          '<stop offset="0" stop-color="#7a1838"/><stop offset="1" stop-color="#2e0715"/></linearGradient>')


def drawing():
    """The drawing's viewBox and its insides (defs and all)."""
    svg = SOURCE.read_text(encoding="utf-8")
    view_box = re.search(r'viewBox="([^"]+)"', svg).group(1)
    inner = re.search(r"<svg[^>]*>(.*)</svg>", svg, re.S).group(1)
    return view_box, inner


def tile_svg(size, rounded, share):
    """The drawing on a velvet tile. share: how much of the tile's height the drawing fills."""
    view_box, inner = drawing()
    _x, _y, width, height = (float(part) for part in view_box.split())
    box_h = 400 * share
    box_w = box_h * width / height
    corner = 88 if rounded else 0
    # A nested <svg> scales the drawing into its box and keeps its own coordinates.
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 400 400" width="{size}" height="{size}">'
            f"<defs>{VELVET}</defs>"
            f'<rect width="400" height="400" rx="{corner}" fill="url(#tile)"/>'
            f'<svg x="{(400 - box_w) / 2:.1f}" y="{(400 - box_h) / 2:.1f}" width="{box_w:.1f}" height="{box_h:.1f}" '
            f'viewBox="{view_box}">{inner}</svg></svg>')


# (file, size, rounded corners, share of the height the drawing fills)
PNGS = [
    ("icon-32.png", 32, True, 0.9),
    ("icon-192.png", 192, True, 0.86),
    ("icon-512.png", 512, True, 0.86),
    ("apple-touch-icon.png", 180, False, 0.86),
    ("icon-maskable-512.png", 512, False, 0.68),  # inside Android's safe circle
]
ICO_SIZES = (16, 32, 48)


def render(browser, svg, size):
    page = browser.new_page(viewport={"width": size, "height": size})
    page.set_content(f'<style>html, body {{ margin: 0; background: transparent; }} svg {{ display: block; }}</style>'
                     f'<div id="icon">{svg}</div>')
    png = page.locator("#icon > svg").screenshot(omit_background=True)
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
        for name, size, rounded, share in PNGS:
            (ICONS / name).write_bytes(render(browser, tile_svg(size, rounded, share), size))
            print("saved", name)
        write_ico(ICONS / "favicon.ico", [(size, render(browser, tile_svg(size, True, 0.9), size))
                                          for size in ICO_SIZES])
        print("saved favicon.ico")
        browser.close()


if __name__ == "__main__":
    main()
