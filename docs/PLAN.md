# Right On Cue — email campaign scheduler (plan)

*"Emails that go out right on cue."*

## Context

A new learning project: make email campaigns, schedule them, retry failed sends and track what happened to every email. The skills to practise are **background jobs, retries, scheduling and rate limits**. What the user chose:

- **Name:** Right On Cue. Package `oncue`, local port **5013** (J: Drive has 5012). Repo `https://github.com/jcatibog014/email_campaign_scheduler` (made by the user, has `main`). First step: clone it into a new folder `C:\Users\USER\Documents\Workspace\email_campaign_scheduler`, then branch `build-right-on-cue`.
- **Who sends real email:** only the **owner** (made with `flask create-owner` from the PC), through the **Brevo HTTP API** (free 300 a day, a real limit to respect). **Try Demo** gives each visitor a sample workspace (deleted after 24 h) that only ever sends to the built-in **practice mail server**.
- **Background jobs:** a **hand-built queue in the database**, no Redis or Celery.
- **Practice mail server:** realistic and controllable: mostly delivers, but sometimes is slow, says "try later" (429/503), times out or bounces, at rates set in Settings. Special addresses force a result (`bounce@`, `slow@`, `flaky@`, `complain@`).
- **Extras (all four):** Audience tools, Smart sending, Content tools, Insights + job console.
- **Look:** a **theatre backstage**, drawn in CSS like DFJC's bamboo and awning (no image files): velvet curtain, marquee bulbs, stage floor + spotlight, call board + cue cards. Light + dark.
- **Wording:** fully themed (see the word list below), each cue word with a plain-English note so nobody has to guess (the DFJC "Didn't go through" lesson).
- **PRs:** all 9 stages in **one PR** with a commit per stage, no stops.

## Stack (same as J: Drive / Ticket Tock)

- Flask 3, SQLAlchemy 2.0 + Alembic (`flask --app oncue upgrade-db`), SQLite locally, Neon Postgres live, Render (Blueprint `right-on-cue`), pytest, `.venv`, `AGENTS.md` + `CLAUDE.md`, `tzdata` + `zoneinfo`, markdown-it-py + nh3 (from Akda) for email bodies.
- Copied from `file_storage_sharing_app/jdrive/` and adapted: `db.py` (FK-safe SQLite `upgrade_db`), `auth.py` (hashing, CSRF, `session_version`, random demo session_version), `mailer.py` (Brevo part), `limits.py` (login limits), `tick.py` + `.github/workflows/tick.yml`, `formatting.py`, `dates.py`, `_toasts.html`, `theme.js`, `sw.js`/manifest, `docs/make_icons.py`, `tests/conftest.py` (quick pbkdf2), `test_readme.py`, `test_icons.py`. `dialect.py` from Tally Ho! for SQLite/Postgres differences.
- Lessons built in from the start: `sqlite_autoincrement` on users/workspaces; every FK to a bulk-deleted parent has an index starting with that column; new NOT NULL columns get a `server_default`; template helpers are Jinja globals; Settings holds theme + Log out ([[controls-in-settings]]); Title Case headings, no line under page titles, toasts bottom-right, demo notice as a passing toast + small Demo badge.

## The words (fully themed)

| Plain meaning | Right On Cue says | Note shown beside it |
|---|---|---|
| Campaign | **Show** | One email sent to an audience |
| Draft | **Rehearsal** | Not scheduled yet; you can still change everything |
| Scheduled | **Standby** | Will go out at the time shown |
| Sending | **Go** | Emails are going out now |
| Paused | **Hold** | Stopped for now; nothing more is sent until you resume |
| Sent / finished | **Curtain Down** | Every email has been handled |
| Cancelled | **Cut** | Won't be sent |
| Email waiting | **In the Wings** | Waiting its turn (or for the rate limit) |
| Email being sent | **On Stage** | Being handed to the mail service now |
| Will retry | **Missed Cue** | The mail service said "try later"; it tries again at the time shown |
| Failed for good | **Dropped** | Won't be tried again (reason shown) |
| Delivered / Opened / Clicked / Bounced / Unsubscribed | kept plain | These are the words mail services use |
| Circuit breaker | **Safety Curtain** | Sending stopped by itself after many failures in a row |

Menu: **Stage** (home), **Shows**, **Call Board** (schedule calendar), **Audience** (contacts, lists, segments), **Scripts** (templates), **Reviews** (insights), **Backstage** (job console), **Settings**.

## Core design

### Data model (`models.py`), every row has `workspace_id` (the owner's + one per demo)

| Table | Holds |
|---|---|
| `workspaces`, `users` | owner + demos; time zone (default Asia/Manila), sender name/address, postal address for the footer, quiet hours |
| `contacts` | email (unique per workspace), name, custom fields (JSON), time zone, `consent_at`, status (subscribed / unsubscribed / suppressed) |
| `lists`, `list_members`, `segments` | lists + saved rules (field op value) turned into SQL |
| `suppressions` | addresses never to send to: hard bounce, complaint, unsubscribe, manual |
| `scripts` | saved templates: subject, Markdown body |
| `shows` | campaign: subject (+ B for A/B), script snapshot, audience, `send_at`, status, recurrence rule, A/B settings, `version` (optimistic lock) |
| `performances` | one run of a show (a recurring show has many) |
| `messages` | one email to one contact: UNIQUE(performance_id, contact_id), status, attempts, `next_try_at`, provider id, `send_token` (idempotency) |
| `message_events` | append-only: delivered, opened, clicked, bounced…, UNIQUE(source, event_id) so a webhook sent twice counts once |
| `links` | each tracked link in a show (click redirect looks up the stored URL, so no open redirect) |
| `jobs` | the queue (below) |
| `buckets` | token buckets for rate limits |
| `workers` | heartbeats: id, host, pid, started, last_seen |

### The job queue (`queue.py`, `worker.py`): the background-jobs lesson

- `jobs`: kind, payload (JSON), `run_at`, priority, status (queued / running / done / dead), attempts, max_attempts, `lease_until`, `locked_by`, last_error, `dedupe_key` (UNIQUE, so "start show 7 run 3" can't be queued twice).
- **Claim:** PostgreSQL `SELECT … FOR UPDATE SKIP LOCKED LIMIT n`; SQLite a guarded `UPDATE … WHERE id = ? AND (lease_until IS NULL OR lease_until < now)` checked by rowcount. Either way two workers never take the same job.
- **Lease + heartbeat:** a running job holds a 60 s lease that the worker extends. A **reaper** puts jobs with run-out leases back (the worker crashed). Delivery is **at-least-once**, so every job is written to be safe to run twice.
- **Retries:** exponential backoff with **full jitter** (`random(0, min(cap, base·2^n))`, base 30 s, cap 1 h, 6 tries), or the mail service's `Retry-After` if it gives one. After the last try the job goes to **dead letters** (Backstage: Retry Now / Discard).
- **Where the worker runs:** locally `flask --app oncue worker` (its own process; start two to watch them share work). With `flask run`, and live on Render, a worker **thread inside the web app** (Render's free plan has no worker service). `/tasks/tick` (GitHub Actions cron, from J: Drive) wakes a sleeping site and runs one round, so scheduled shows still go out when nobody visits. Honest limit on the free plan: a show due while the site sleeps goes out at the next tick (cron every 15 min, and GitHub can run late). DEPLOY.md says so, and that Render's 750 free hours are shared by all the user's sites.
- **The scheduler** (a job that re-queues itself every 15 s): Standby shows whose time has come → a `start_performance` job, which snapshots the audience into `messages` in batches of 500 (skipping suppressed and unsubscribed addresses), then queues `send_message` jobs.

### Sending, retries and rate limits (`mailers.py`, `sending.py`, `buckets.py`)

- **Three mailers, one interface** `send(message) → Accepted(provider_id) | TryLater(retry_after) | Rejected(reason)`: Brevo (owner only), the **practice mail server** (demo, local, tests; seeded random so tests are repeatable; honours an Idempotency-Key), and console (nothing set up).
- **Error classes:** timeouts, connection errors, 429, 5xx, soft bounce → **Missed Cue** (retry); 400 bad address, hard bounce, complaint → **Dropped** + the address goes on the suppression list.
- **Token buckets** in the DB (row lock while taking a token): per mailer per second (Brevo 5/s, practice 10/s), a **daily cap** (Brevo 300/day, with the show's "Finishes about Tue 9:00 AM" estimate), and per receiving domain (gmail.com, yahoo.com…, set in Settings). No token → the job is moved to when one will be free, **without** counting as a try.
- **Adaptive slow-down:** a 429 halves that bucket's rate for 10 minutes, then it climbs back.
- **Safety Curtain** (circuit breaker): 10 failures in a row from one mailer stops it for 5 minutes, then one test send decides. Shown on Stage and Backstage, with a Raise Curtain button.
- **No double emails:** before calling the mailer, the message is marked On Stage with a fresh `send_token`. If a worker dies mid-send, the reaper finds the message On Stage: the practice server is asked again with the same token (it answers "already sent"); for Brevo, which has no idempotency key, the message becomes **Unsure** and is *not* resent by itself (a missing email is better than a double one). Backstage lists these with a Resend button.
- Every email has an unsubscribe link and `List-Unsubscribe` + `List-Unsubscribe-Post` (one-click) headers, plus the sender's postal address in the footer.

### Delivery tracking (`tracking.py`, `hooks.py`)

- **Brevo webhook** `/hooks/brevo/<secret>`: delivered, deferred, soft/hard bounce, opened, click, spam, unsubscribed, blocked. Duplicates are ignored (UNIQUE event id); late or out-of-order events never move a status backwards (each status has a rank; an Opened before a Delivered implies Delivered).
- **Practice server events:** it sends its own events back later through the same code path (delivered after a few seconds, some bounce minutes *after* being accepted, some opens and clicks), so tracking is practised without the internet.
- **Own tracking:** an open pixel `/o/<token>.gif` and click redirect `/c/<token>`, HMAC-signed tokens. Reviews notes that opens are a rough guide (Apple Mail and others preload pictures).
- A show's page refreshes its counts every 3 s while it is on Go (small JSON endpoint) with the marquee bulbs lit.

## Stages (one PR, a commit each)

1. **Setup, login, the backstage look.** App factory, models + migration 0001, `upgrade-db`, `create-owner`, login/logout, Try Demo skeleton (per-visitor workspace, View As not needed: one role), Settings (name, password, time zone, theme, Log out), the full backstage CSS: velvet curtain on the login panel and sidebar edge, scalloped valance along the top, stage-floor planks, spotlight glow behind titles, marquee bulbs (twinkle only on Go, still with `prefers-reduced-motion`), gaffer-tape labels, cue-card status stamps. Logo placeholder (an envelope in a spotlight) + icons, CSRF, security headers + CSP.
2. **Audience.** Contacts (add, edit, search, page), CSV import with column mapping, duplicate checks and a required "these people agreed to hear from you" tick, lists and tags, segments (rules → SQL, live count), suppression list, the public unsubscribe page and preference choices.
3. **Scripts and Shows.** Scripts (Markdown editor with live preview, merge fields with fallbacks `{{first_name|there}}`, preview as a chosen contact, plain-text version made automatically), the show builder (audience, script, subject, sender, when), the show state machine (`shows.py`: Rehearsal → Standby → Go → Curtain Down, plus Hold / Resume / Cut, with optimistic locking), Send a Test.
4. **The job queue and worker.** `jobs`, `queue.py` (enqueue, claim, lease, heartbeat, complete, fail, reaper, dead letters), `worker.py` (`flask worker`, in-app thread, graceful stop on Ctrl+C), `workers` heartbeats, the scheduler, `start_performance` fan-out, `/tasks/tick` + cron. Tests with a fake clock, two threads racing for jobs, a killed worker's lease running out, a fan-out run twice making no extra messages.
5. **Sending, retries and rate limits.** The three mailers, the practice server's failure controls in Settings, error classes, backoff + jitter + Retry-After, token buckets (per second, daily cap, per domain), adaptive slow-down, Safety Curtain, the Unsure state, unsubscribe headers.
6. **Delivery tracking.** `message_events`, the Brevo webhook (secret, duplicates, out-of-order), practice server events, open pixel + click redirect, suppression on hard bounce/complaint, the show page's live counts and per-email list with each email's history.
7. **Smart sending.** Send in each contact's time zone ("9:00 AM their time"), quiet hours / send windows (a send that lands outside waits for the window), A/B subject test (test 20%, wait N hours, the winner by opens or clicks goes to the rest, by a scheduled job), recurring shows (every week on chosen days at a time; each run a performance), and the **Call Board** page: a cork board with pinned cue cards for what's on Standby this week and month.
8. **Reviews and Backstage.** Reviews: delivery funnel (sent → delivered → opened → clicked, plus bounced), opens over time, clicks per link, bounce reasons, show-vs-show table; server-drawn SVG charts with colours checked by the dataviz validator. Backstage: jobs by status, running jobs with lease countdown, worker heartbeats, the retry list (next try, try n of 6, last error), dead letters (Retry Now / Discard), Unsure emails (Resend), rate-limit gauges (tokens, today's Brevo use of 300), Safety Curtain, a Pause All Sending switch.
9. **Demo, phone app, deploy, README.** Try Demo is a made-up theatre company newsletter ("Teatro Luna" with ~250 made-up contacts on `.invalid`/example addresses): past shows with real results played through the real functions on a fast clock, one recurring show, one on Hold, an A/B test, a show going on **Go one minute after you arrive** so you can watch it send, a dead letter and an Unsure email in Backstage. `tests/test_demo.py` fails if any demo page is empty. PWA. `render.yaml` + `docs/DEPLOY.md` (Brevo key + verified sender + webhook, Neon, `create-owner` from the PC, GitHub secrets `TICK_URL`/`TICK_KEY`). README in simple English with a tools table and screenshots from Try Demo (`docs/take_screenshots.py`), `docs/PLAN.md` with a progress list.

## Main files

`oncue/__init__.py`, `models.py`, `db.py`, `dialect.py`, `auth.py`, `audience.py`, `segments.py`, `csv_import.py`, `scripts.py` (rendering + merge fields), `shows.py`, `queue.py`, `worker.py`, `scheduler.py`, `mailers.py`, `practice_server.py`, `sending.py`, `buckets.py`, `tracking.py`, `hooks.py`, `smart.py` (time zones, windows, A/B, recurrence), `reviews.py`, `charts.py`, `backstage.py`, `seed.py`, `tick.py`, `migrations/versions/0001…`, `templates/`, `static/style.css` (the backstage pieces), `app.js`, `theme.js`, `sw.js`, `docs/PLAN.md`, `DEPLOY.md`, `make_icons.py`, `take_screenshots.py`, `tests/test_*.py` per stage.

## Verification

- `pytest` after each stage, with a fake clock and a seeded practice server: two workers never take one job; a crashed worker's job comes back after its lease; retries follow backoff and stop after 6 into dead letters; Retry-After is obeyed; a rate-limited job doesn't lose a try; the daily cap spreads a 500-email show over two days; the Safety Curtain falls and rises; no email is sent twice (fan-out twice, webhook twice, reaper + idempotency); suppressed and unsubscribed contacts are never sent to; time zones, quiet hours, A/B winner, recurrence (including a daylight-saving zone); webhook out-of-order; signed tracking tokens can't be changed; demo completeness, README pictures, migrations from empty.
- Run on port 5013 with `--debug` and a separate `flask worker`, click through as the owner and Try Demo, watch a show go from Standby to Curtain Down, raise the practice server's failure rates and watch Missed Cues retry in Backstage, kill the worker mid-show and check the reaper.
- Visual check and screenshots with Playwright (borrowed from expense_tracker's `.venv`), the backstage colours' contrast checked in light and dark.
- Not testable by me: real Brevo sending and webhooks (the user's key), and PostgreSQL's SKIP LOCKED path (the user runs the suite on an empty Neon branch). The PR description will say so.

## Progress

- [x] Stage 1: setup, login, Try Demo, Settings, the backstage look
- [x] Stage 2: Audience
- [x] Stage 3: Scripts and Shows
- [x] Stage 4: The job queue and worker
- [x] Stage 5: Sending, retries and rate limits
- [x] Stage 6: Delivery tracking
- [ ] Stage 7: Smart sending
- [ ] Stage 8: Reviews and Backstage
- [ ] Stage 9: Demo, phone app, deploy, README
