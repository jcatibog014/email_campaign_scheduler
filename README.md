# Right On Cue

*Emails that go out right on cue.*

Right On Cue is an **email campaign scheduler**. You write an email (a **show**), choose who gets it (the **audience**), and choose when it goes. Then a crew of background workers sends every email: they keep to speed limits, they try again when the mail service says "try later", and they tell you what happened to each email (delivered, opened, clicked, bounced).

The app looks and talks like a **theatre backstage**: velvet curtains, a stage floor, a cork call board, marquee lights. A show waits on **Standby**, then goes out on **Go**, and ends with **Curtain Down**. Every theatre word has a plain explanation beside it.

I made it to practise **background jobs, retries, scheduling and rate limits**.

Try it: on the login page, press **Try the demo**. You get your own sample theatre company, "Teatro Luna", for 24 hours. Everything in the demo goes to a pretend mail server, never to a real inbox. Its past six weeks are played back through the real crew when it starts, so every page has examples.

![The login page: the curtain opens on a lit stage](docs/screenshots/login.png)

![The Stage: a show on Go under marquee lights, the next shows pinned on the call board, and finished shows with their results](docs/screenshots/stage.png)

## What You Can Do

### Shows (campaigns)

- Write a show with **Markdown** and **merge fields**: `Hi {{first_name|there}}` becomes "Hi Ana", or "Hi there" when there is no first name.
- See a **live preview** as any person in your audience.
- Send it **once at a time**, **at a time in each person's own time zone** ("9:00 AM their time"), or **every week**.
- **Hold** it, **Resume** it, or **Cut** it while it sends.
- **A/B test the subject**: some people get subject A, some get subject B, and the winner goes to everyone else by itself.
- **Send a test** to yourself first.

![All shows, with their cue words](docs/screenshots/shows.png)

![A finished show: progress, results and clicks per link](docs/screenshots/show-results.png)

![Writing a show, with an A/B test and the live preview](docs/screenshots/show-builder.png)

![Scheduling: once, at their own time, or every week](docs/screenshots/show-schedule.png)

Before something big (Start now, Delete, Resend), the app asks first, in its own look:

![Asking before Start now](docs/screenshots/confirm.png)

### What Happened to Each Email

- Every email has a **timeline**: in the wings, sent, delivered, opened, clicked, bounced.
- News comes from the mail service, from a tiny picture in the email (opens), and from links in the email (clicks).
- Addresses that **bounce** or **mark an email as spam** go on the **do-not-send list** by themselves.

![Every email of a show, in groups](docs/screenshots/show-emails.png)

![One email's timeline](docs/screenshots/email-timeline.png)

### The Call Board

The schedule as cue cards pinned on a cork board, by week or by month. A weekly show has a card on every day it goes.

![The Call Board, week](docs/screenshots/call-board.png)

![The Call Board, month](docs/screenshots/call-board-month.png)

### Audience

- **Contacts** with tags, their own time zone, and any extra fields (city, plan...).
- **Lists** (people you choose) and **segments** (rules like "city is Cebu", worked out each time).
- **Import a CSV file** in four steps. The Review step shows exactly what will happen before anything changes.
- People can **unsubscribe** from every email, or leave only some topics. Mail apps get a one-click unsubscribe too.

![Contacts](docs/screenshots/audience.png)

![One contact, with the emails they got](docs/screenshots/contact.png)

![Segments](docs/screenshots/segments.png)

![Importing a CSV file](docs/screenshots/import.png)

![The page people see when they unsubscribe](docs/screenshots/unsubscribe.png)

### Scripts

Emails you can use again. Each show starts with its own copy.

![The script editor](docs/screenshots/script-editor.png)

### Reviews

How your shows did: the delivery funnel, first opens per day, why emails bounced, the most clicked links, and every show side by side.

![Reviews](docs/screenshots/reviews.png)

![Reviews with the house lights down (dark mode)](docs/screenshots/reviews-dark.png)

### Backstage

The job console. See the workers (the crew) and their heartbeats, the queue, emails waiting to try again, jobs that failed every try (**dead letters**), emails nobody can be sure about (**Unsure**), and the pretend mail server's inbox. **Pause all sending** with one button.

![Backstage: the crew, the queue and the sending limits](docs/screenshots/backstage.png)

![Dead letters, with Retry now and Discard](docs/screenshots/dead-letters.png)

![Unsure emails: a person decides](docs/screenshots/unsure.png)

![An email in the practice inbox](docs/screenshots/practice-mail.png)

### Settings

Who emails come from, the time zone, how fast to send, how many a day, limits per receiving domain (like gmail.com), quiet hours at night, how difficult the practice mail server is, and light or dark mode.

![Settings](docs/screenshots/settings.png)

### On a Phone

Right On Cue works on a phone, and you can add it to your home screen like an app.

![The Stage on a phone](docs/screenshots/phone-stage.png) ![A show on a phone](docs/screenshots/phone-show.png)

## How the Sending Works

| Idea | What it means here |
|---|---|
| **Job queue** | Work waits in a database table. Workers take jobs from it. Two workers never take the same job. |
| **Lease** | A worker holds a job for 60 seconds at a time. If the worker stops, the job comes back for another worker. |
| **Retry with backoff** | When the mail service says "try later", the email waits a little longer each time (30 seconds, 1 minute, 2 minutes... at most 1 hour), with some randomness, up to 6 tries. |
| **Dead letter** | A job that failed every try. A person decides: retry or discard. |
| **Idempotency** | Each send has a key. If a worker stops in the middle, the retry uses the same key, so the person never gets the email twice. |
| **Rate limit** | A token bucket: at most a few emails a second, at most 300 a day (Brevo's free plan), and limits per domain. Waiting for a token never counts as a failed try. |
| **Safety Curtain** | After 10 failures in a row, sending stops for 5 minutes, then one test email decides. |
| **Scheduler** | It starts each show at its time, in each person's own time zone, and not in quiet hours. |

## Tools I Used

| Tool | What it is | What it does here |
|---|---|---|
| Python 3.13 | A programming language | All the app's code |
| Flask | A small web framework | The pages and the web addresses |
| SQLAlchemy | A database library | Talks to the database |
| Alembic | A migrations tool | Changes the database tables safely |
| SQLite | A small database in one file | The database on my computer and in tests |
| PostgreSQL (Neon) | A big database in the cloud | The database of the live site |
| markdown-it-py and nh3 | Markdown and HTML tools | Turn a script into a safe email |
| Brevo | An email service | Sends the owner's real emails |
| Render | A hosting service | Runs the live website |
| GitHub Actions | A timer in the cloud | Wakes the site so shows go out on time |
| pytest | A testing tool | More than 175 tests |
| Playwright | A browser robot | Takes the screenshots |
| HTML, CSS and JavaScript | The languages of web pages | The look (all drawn with CSS, no pictures) and the live updates |

## Run It on Your Computer

```powershell
cd C:\Users\USER\Documents\Workspace\email_campaign_scheduler
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\flask --app oncue upgrade-db
.venv\Scripts\flask --app oncue create-owner
.venv\Scripts\flask --app oncue run --debug --port 5013
```

Then open http://127.0.0.1:5013. A worker runs inside the app. To watch two workers share the work, see `AGENTS.md`.

Run the tests with `.venv\Scripts\python -m pytest`.

To put it online, follow [docs/DEPLOY.md](docs/DEPLOY.md). The plan is in [docs/PLAN.md](docs/PLAN.md).
