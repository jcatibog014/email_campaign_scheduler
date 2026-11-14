"""The backstage look: the theatre pieces are drawn in CSS and every page wears them,
and the cue words always come with their plain meaning."""
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from oncue import cues

from .conftest import text

CSS = (Path(__file__).parent.parent / "oncue" / "static" / "style.css").read_text(encoding="utf-8")


def test_the_theatre_pieces_are_drawn_in_css():
    for piece in (".velvet", ".valance", ".floor", ".marquee", ".corkboard", ".cue-card", ".tape", ".spotlight"):
        assert re.search(re.escape(piece) + r"[\s{,:.]", CSS), piece
    assert "url(" not in CSS.replace('url("fonts/', "")  # no picture files, only fonts


def test_dark_mode_is_defined_twice_with_the_same_tokens():
    device = re.search(r"@media \(prefers-color-scheme: dark\) \{\s*:root:not\(\[data-theme=\"light\"\]\) \{(.*?)\n  \}", CSS, re.S).group(1)
    chosen = re.search(r":root\[data-theme=\"dark\"\] \{(.*?)\n\}", CSS, re.S).group(1)
    tokens = lambda block: sorted(re.findall(r"(--[\w-]+):", block))  # noqa: E731
    assert tokens(device) == tokens(chosen)


def test_the_bulbs_stand_still_for_less_motion():
    assert "prefers-reduced-motion: reduce" in CSS


def test_the_login_has_the_curtain_and_the_stage(client):
    page = text(client.get("/login"))
    assert 'class="welcome-panel velvet"' in page and 'class="welcome-stage"' in page


def test_signed_in_pages_have_the_curtain_menu_and_the_valance(owner):
    client, _ = owner
    page = text(client.get("/stage"))
    assert 'class="sidebar velvet"' in page and 'class="valance"' in page
    assert "The Stage Is Dark" in page


def test_every_cue_word_has_a_plain_note():
    for table in (cues.SHOW, cues.MESSAGE, cues.JOB, cues.WORDS):
        for key, cue in table.items():
            assert cue.note.endswith((".", ")")), key
            assert f".cue-{cue.tone}" in CSS or cue.tone == "rehearsal", key


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js isn't installed")
def test_the_scripts_have_no_syntax_errors():
    static = Path(__file__).parent.parent / "oncue" / "static"
    for script in ("app.js", "theme.js", "sw.js"):
        result = subprocess.run(["node", "--check", str(static / script)], capture_output=True, text=True)
        assert result.returncode == 0, f"{script}: {result.stderr}"


def test_no_inline_styles_or_scripts():
    templates = Path(__file__).parent.parent / "oncue" / "templates"
    for path in templates.glob("*.html"):
        source = path.read_text(encoding="utf-8")
        assert 'style="' not in source, path.name
        assert not re.search(r"<script(?![^>]*\bsrc=)", source), path.name
