# AGENTS.md

This file guides AI coding agents (Claude Code, Codex, Cursor, Copilot and others) working in this repository.

## Overview

**Right On Cue** ("Emails that go out right on cue."), an email campaign scheduler in Python (Flask). The repo is `email_campaign_scheduler` and the Python package is `oncue`. The owner makes shows (campaigns), schedules them, and a background worker sends every email, retries failed sends, keeps to rate limits and tracks what happened to each one. The plan is `docs/PLAN.md`.

It is a learning project for **background jobs, retries, scheduling and rate limits**, so those parts stay easy to read: short comments that explain *why*, a job queue written by hand (no Celery, RQ or Redis), login written by hand (no Flask-Login), and no extra libraries unless the owner asks for them. Write the README in simple (A1-level) English.

Python 3.13 is at `%LOCALAPPDATA%\Programs\Python\Python313\python.exe`. Use the virtual environment:

- Install: `.venv\Scripts\python -m pip install -r requirements.txt` (create `.venv` with `python -m venv .venv` first if it's missing).
- First time: `.venv\Scripts\flask --app oncue upgrade-db` (makes the tables), then `.venv\Scripts\flask --app oncue create-owner` (your login; it asks for the password).
- Run: `.venv\Scripts\flask --app oncue run --debug --port 5013`, then open http://127.0.0.1:5013. (Ports: Swap Meter 5000, Ambon Alert 5001, Ang Gastos! 5002, Sangkap 5003, Gawi 5004, Akda 5005, Hire Wire 5006, Plan-It 5007, ShiroMoto 5008, DFJC 5009, Ticket Tock 5010, Tally Ho! 5011, J: Drive 5012.)
- Test: `.venv\Scripts\python -m pytest`. Every test gets a fresh SQLite file and never uses the internet. Set `TEST_DATABASE_URL` to an **empty** Neon branch to run the same tests on PostgreSQL (each test drops and remakes the tables). SQLAlchemy warnings fail the tests (`pytest.ini`).
- Other commands: `set-password <email>`.
- Logo and app icons: `docs/logo/right-on-cue.svg` (an envelope centre stage under a spotlight, on a velvet tile). `..\expense_tracker\.venv\Scripts\python docs\make_icons.py` (Playwright + Edge) copies it to `oncue/static/icons/logo.svg` and draws every PNG icon and `favicon.ico`. Run it after changing the drawing.

## Architecture

- **Accounts (`auth.py`):** no sign-up. The OWNER is made from the PC with `create-owner` (one per site); everyone else uses Try Demo. Hashes from Werkzeug; 5 wrong passwords lock the account for 15 minutes, one address may try 30 logins per 15 minutes (`limits.py`). `load_user()` checks `session_version` on every request (a password change logs out other devices) and puts `g.user` and `g.workspace` on the request.
- **Workspaces:** every row that belongs to someone has `workspace_id`. The owner's workspace is the real one; each Try Demo makes a sandbox workspace. Queries in views always filter by `g.workspace.id`.
- **Demo (`seed.py`, `views/demo.py`):** "Teatro Luna", run by Luna Reyes. Demo users have addresses ending in `.invalid`, a random `session_version`, can't change their account, and are deleted with their whole workspace after 24 hours (`seed.purge`). At most 100 demos at once, 10 per network an hour. **Every page the demo can open must show an example, so add sample data in `seed.py` with every new feature.**
- **The words (`cues.py`):** the app talks like a theatre backstage (Show, Rehearsal, Standby, Go, Hold, Curtain Down, Cut, In the Wings, On Stage, Missed Cue, Dropped, Safety Curtain). Every cue word has a plain-English note; templates show them with the `cue()` / `show_status()` / `message_status()` macros in `_macros.html`, which put the note in a `title` and in hidden text for screen readers. Add new words to `cues.py`, never straight into a template.
- **Times (`dates.py`, `formatting.py`):** the database holds naive UTC. Pages show the workspace's time zone (Settings). Functions that decide something about time take the moment as an argument instead of reading the clock.
- **Database:** SQLAlchemy 2.0 ORM (typed `Mapped[...]`), plain (not Flask-SQLAlchemy). `oncue/db.py` makes one engine per app and one session per request: `db.session()`; code outside a request (the worker) uses `db.new_session()` and closes it. SQLite locally (`instance/oncue.sqlite`, WAL mode and a 30-second busy timeout so a worker and the pages can share it), PostgreSQL (Neon, psycopg 3) when `DATABASE_URL` is set. `workspaces` and `users` use `sqlite_autoincrement`, so a deleted demo's id is never handed out again.
- **Migrations: Alembic**, in `oncue/migrations/`. `flask --app oncue upgrade-db` runs them (with SQLite's foreign keys switched off during the run, so a table rebuild can't cascade-delete rows); Render runs it before gunicorn.
  - **To change the schema:** edit `models.py`, then `.venv\Scripts\alembic revision --autogenerate -m "what changed" --rev-id 000N`, **read the new file and fix it** (booleans default to `sa.false()`, never `sa.text('0')`, so PostgreSQL accepts them; a new NOT NULL column needs a `server_default`), and commit it. Never edit a migration that has run on the live site. `tests/test_migrations.py` fails if the models and the migrations disagree.
  - Every foreign key to a parent that is deleted in bulk needs an index that STARTS with that column, or the cascade scans the whole table.
- **Every page works without JavaScript.** `app.js` only adds extras. The CSP (`__init__.py`) has no inline scripts or styles, so **never use `style=""` or inline `<script>`**: sizes and colours are classes.
- Commit messages and PR bodies go through files (`git commit -F`, `gh pr create --body-file`): PowerShell 5.1 breaks quotes in arguments.

## UI rules (the owner's priority is neatness)

- **Look: a theatre backstage**, drawn in CSS (no picture files), not just a palette: the velvet curtain (`.velvet`, the sidebar and the login panel, with a gold rope edge), the valance (`.valance`, scallops with a gold fringe along the top of every page), the stage floor (`.floor`, `--floorboards`, under page titles and empty states) with a spotlight (`.page-head::before`, `.spotlight`), marquee bulbs (`.marquee`, `.is-live` makes them chase; still for `prefers-reduced-motion`), the call board (`.corkboard`) with pinned cue cards (`.cue-card`), and gaffer-tape labels (`.tape`). Colours are tokens on `:root`: cream programme paper (`--bg` #f6efe3), plum-black ink, spotlight amber `--accent` #b45309 for buttons and links (white text 5:1; #f2a93b with dark text in dark mode). Cue stamps (`.cue-<tone>`) have a little cue light: Standby red, Go green, as on a stage manager's desk. Playfair Display for headings, Inter for text, Saira Condensed for labels, self-hosted in `static/fonts/`. Dark mode ("house lights down") is defined twice in `style.css` (device setting + the choice in Settings); change both blocks together (`tests/test_look.py` checks they match).
- **Messages are toasts** (`_toasts.html`, bottom right): `flash(msg)` is good news and fades after 6 seconds; `flash(msg, "error")` stays until closed; `flash(msg, "demo")` is the demo welcome (15 seconds).
- **Settings holds the controls, and the menus only navigate.** The theme (saved per device in `localStorage`; `theme.js` applies it before the page draws) is only on `/settings`. Log out is there too, and in the account menu at the bottom of the curtain.
- **No decorative emoji.** Icons are the line SVGs in `_icons.html`.
- **Page titles are bare:** `page_head(title)` from `_macros.html`, with no line of text under the title.
- **Title Case** for the menu, page titles (`h1`), section headings, tabs, empty-state titles and anything that names a page or a cue word. Small words (a, an, the, and, or, of, in, on, to, for, with, as, at, by) stay lowercase unless first. **Sentence case** for everything else: action buttons ("Save workspace", "Try demo"), labels, hints, messages. The main button of a page is last on the right.
- Controls are the same height (`--control-height`) with bottoms aligned. Sizes in `rem`; the root font steps up at 1600 / 1920 / 2300px. Tabs are pills that wrap, never a sideways scroll; on phones, tables should fit rather than scroll.
- The curtain menu shows from 56rem up; below that a Menu button opens it.
