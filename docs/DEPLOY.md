# Putting Right On Cue Online

Right On Cue runs on **Render** (the website, free), **Neon** (the database, free) and **Brevo** (real emails, free for 300 a day). A **GitHub Actions** timer wakes the site so scheduled shows go out. Do the parts in order.

Never paste a password, connection string or API key into a chat. Put them only into Render, GitHub or your own PowerShell window.

## Before You Start: What the Free Plans Mean

- **Render's free website sleeps** after 15 minutes without visits. While it sleeps, nothing is sent. The timer (part 5) wakes it every 30 minutes, so **a show due while the site sleeps can go out up to about 30 minutes late**. A show due while you are on the site goes out on time.
- **Neon's free database** has a monthly limit of hours awake. The worker sleeps until the next thing is due, and the timer runs every 30 minutes (not every 5), so the database can rest in between. If you want shows more on time, change `*/30` in `.github/workflows/tick.yml` to `*/15`; the database will be awake more.
- Render's 750 free hours a month are shared by all your Render sites.
- GitHub stops scheduled workflows in a repository with no activity for 60 days. If that happens, press **Enable workflow** on the Actions page.

## Part 1: The Database (Neon)

1. Go to https://neon.tech and log in (you already have an account).
2. **New Project**. Name: `right-on-cue`. Region: **Asia Pacific (Singapore)**. Create.
3. On the project's dashboard, press **Connect**, and copy the **connection string** (it starts with `postgresql://`). Keep it in a safe place for a few minutes.

## Part 2: The Website (Render)

1. Go to https://render.com and log in.
2. **New** > **Blueprint**. Choose the `email_campaign_scheduler` repository. Render reads `render.yaml`.
3. Render asks for three values:
   - `DATABASE_URL`: paste the Neon connection string.
   - `BASE_URL`: type `https://right-on-cue.onrender.com` (if Render gives the site another name, change it later to the real address).
   - `BREVO_API_KEY`: leave it empty for now (part 4).
4. **Apply**. The first deploy takes a few minutes. Open the site: you see the login page.
5. If the address is not exactly `https://right-on-cue.onrender.com`: in Render, open the service > **Environment**, set `BASE_URL` to the real address, and save.

## Part 3: Your Login

The owner's login is made from your PC, straight into the Neon database. In PowerShell, run these **one line at a time** (press Enter after each). The second line has a placeholder: paste your Neon connection string instead of `PASTE-NEON-STRING-HERE`, keeping the quotes.

```powershell
cd C:\Users\USER\Documents\Workspace\email_campaign_scheduler
$env:DATABASE_URL = "PASTE-NEON-STRING-HERE"
.venv\Scripts\flask --app oncue create-owner
```

It asks for your name, your email and a password (twice). Then log in on the website.

When you are done, close the PowerShell window (or run `Remove-Item Env:DATABASE_URL`), so the next command doesn't use the live database by accident.

## Part 4: Real Emails (Brevo)

1. Go to https://www.brevo.com and make a free account (or log in).
2. **Senders, Domains and Dedicated IPs** > **Senders** > **Add a sender**: your name and your own email address (for example your Gmail). Brevo emails you a code: confirm it.
3. **SMTP and API** > **API Keys** > **Generate a new API key**. Copy it.
4. In Render: the service > **Environment** > set `BREVO_API_KEY` to that key. Save (the site restarts).
5. Delivery news: in Render, the service > **Environment**, copy the value of `BREVO_WEBHOOK_SECRET`. In Brevo: **Transactional** > **Settings** > **Webhook** > **Add a new webhook**:
   - URL: `https://right-on-cue.onrender.com/hooks/brevo/` followed by that secret (use your real site address);
   - events: Sent, Delivered, Opened, Clicked, Soft bounce, Hard bounce, Blocked, Invalid email, Deferred, Spam, Unsubscribed, Error.
6. On the website: **Settings** > **Workspace**: set **From address** to the sender you confirmed in step 2. Then **Settings** > **Sending**: choose **Brevo (real email)**, keep **Emails a day** at 300 (Brevo's free limit), and save.
7. Send yourself a test: open a show and use **Send a Test** with your own address.

Mail from Brevo with a Gmail sender may land in spam at first. Ask a friend to mark it "Not spam". Only send to people who agreed to hear from you.

## Part 5: The Timer (GitHub Actions)

1. In Render, the service > **Environment**: copy the value of `TICK_KEY`.
2. In GitHub, the repository > **Settings** > **Secrets and variables** > **Actions** > **New repository secret**, twice:
   - `TICK_URL`: `https://right-on-cue.onrender.com/tasks/tick` (your real address);
   - `TICK_KEY`: the value from step 1.
3. In GitHub, **Actions** > **Stage manager timer** > **Run workflow**, to try it now. It should finish with a green tick and print how many jobs it ran.

## Checking It Works

- **Backstage** > **The Crew** shows a worker "In the web app", On duty, seen a few seconds ago.
- Make a small show to yourself, schedule it 2 minutes from now, and watch it go from Standby to Go to Curtain Down.
- Try Demo works from the login page, and its past fills in within a minute or two.

## If Something Goes Wrong

- **"Set the DATABASE_URL environment variable"** in Render's log: part 2, step 3.
- **"password authentication failed"**: the Neon password changed. Copy a fresh connection string into `DATABASE_URL`.
- **Emails stay "Missed Cue" with "Brevo refused the API key"**: check `BREVO_API_KEY`. After 10 failures in a row the Safety Curtain comes down; once the key is right, press **Raise the curtain** in Backstage.
- **No opens or clicks from Brevo**: check the webhook address (part 4, step 5) ends with the exact `BREVO_WEBHOOK_SECRET`.
- Lines about IPv6 "Network is unreachable" in Neon's log are harmless.
