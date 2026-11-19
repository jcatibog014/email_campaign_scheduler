r"""Take the README's screenshots (docs/screenshots/), all from Try Demo, so every
picture shows made-up people and a made-up theatre.

1. Run the app with --debug (templates reload, and the demo limit is off):

       cd C:\Users\USER\Documents\Workspace\email_campaign_scheduler
       .venv\Scripts\flask --app oncue run --debug --port 5013

2. In another window (Playwright isn't in requirements.txt: borrow another project's):

       cd C:\Users\USER\Documents\Workspace\email_campaign_scheduler
       ..\expense_tracker\.venv\Scripts\python docs\take_screenshots.py

It starts a demo, waits for its past to finish replaying, then visits each page.
"""
import re
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:5013"
OUT = Path(__file__).parent / "screenshots"
WIDE = {"width": 1440, "height": 900}
PHONE = {"width": 390, "height": 844}
CSV = (b"Email,First name,Last name,City,Tags\n"
       b"rosa.lim@gmail.example,Rosa,Lim,Iloilo,\"member, donor\"\n"
       b"jun.tan@yahoo.example,Jun,Tan,Cebu,student\n"
       b"not an email,Bad,Row,,\n")


def shot(page, name, full=True):
    page.wait_for_load_state("networkidle")  # the email preview frames, too
    page.evaluate("window.scrollTo(0, 0)")  # the curtain menu sticks to the top: start there
    page.wait_for_timeout(500)
    page.screenshot(path=str(OUT / name), full_page=full)
    print("saved", name)


def start_demo(page):
    page.goto(BASE + "/login")
    page.click("text=Try the demo")
    for _ in range(120):  # the replay of its past: about 10 seconds locally
        if not page.request.get(BASE + "/stage/replay.json").json()["replaying"]:
            return
        time.sleep(1)
    raise SystemExit("The demo's replay didn't finish. Is the app running with its worker?")


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="msedge")
        wide = browser.new_context(viewport=WIDE)
        page = wide.new_page()
        page.on("dialog", lambda dialog: dialog.accept())
        page.goto(BASE + "/login")
        shot(page, "login.png", full=False)
        start_demo(page)
        page.goto(BASE + "/stage")
        shot(page, "stage.png")
        page.goto(BASE + "/shows/")
        shot(page, "shows.png")
        page.click("text=Season Opener: Ang Huling Ilaw")
        show_url = page.url
        shot(page, "show-results.png")
        page.goto(show_url + "/emails")
        shot(page, "show-emails.png")
        page.goto(show_url + "/emails?group=trouble")
        page.locator("table a.row-link").first.click()
        shot(page, "email-timeline.png")
        page.goto(BASE + "/shows/?tab=rehearsal")
        page.click("text=Closing Night Reminder")
        shot(page, "show-schedule.png")
        page.get_by_role("button", name="Start now").click()  # asks first, in the app's own dialog
        page.locator("dialog.confirm-dialog").wait_for(state="visible")
        page.wait_for_timeout(300)
        shot(page, "confirm.png", full=False)
        page.keyboard.press("Escape")
        page.goto(BASE + "/shows/new")
        page.fill("input[name=name]", "Season Two Announcement")
        page.fill("input[name=subject]", "Season two is here, {{first_name|friend}}")
        page.fill("textarea[name=body]", "Hi {{first_name|there}},\n\n**Season two** opens in March, with three new plays.\n\n[See the season](https://teatro-luna.example/season)")
        page.click("text=A/B test the subject")
        page.check("input[name=ab_on]")
        page.fill("input[name=subject_b]", "{{first_name|Friend}}, your seat for season two")
        page.click("[data-preview-button]")
        page.wait_for_timeout(800)
        shot(page, "show-builder.png")
        page.goto(BASE + "/call-board/")
        shot(page, "call-board.png")
        page.locator('a.board-card[href*="run="]').first.click()  # a past run of the weekly show
        page.wait_for_load_state("networkidle")
        shot(page, "show-run.png", full=False)
        page.goto(BASE + "/call-board/?view=month")
        shot(page, "call-board-month.png")
        page.goto(BASE + "/audience/")
        shot(page, "audience.png", full=False)
        page.locator("table a.row-link").nth(3).click()
        shot(page, "contact.png")
        page.goto(BASE + "/audience/segments")
        shot(page, "segments.png")
        page.goto(BASE + "/audience/import")
        page.set_input_files("input[name=file]", {"name": "new-members.csv", "mimeType": "text/csv", "buffer": CSV})
        page.click("button:has-text('Upload')")
        shot(page, "import.png")
        page.goto(BASE + "/scripts")
        page.click("text=Monthly Programme")
        page.wait_for_timeout(600)
        shot(page, "script-editor.png")
        page.goto(BASE + "/reviews/")
        shot(page, "reviews.png")
        page.emulate_media(color_scheme="dark")
        shot(page, "reviews-dark.png")
        page.emulate_media(color_scheme="light")
        page.goto(BASE + "/backstage/")
        shot(page, "backstage.png")
        page.goto(BASE + "/backstage/dead-letters")
        shot(page, "dead-letters.png")
        page.goto(BASE + "/backstage/unsure")
        shot(page, "unsure.png")
        page.goto(BASE + "/backstage/inbox?q=what's on in")  # October's programme: its people are on topic lists
        page.locator("table a.row-link").first.click()
        page.wait_for_timeout(600)
        shot(page, "practice-mail.png")
        unsubscribe = re.search(r"Unsubscribe or choose topics: (https?://[^\s<]+/u/[\w.-]+)", page.content()).group(1)
        page.goto(BASE + "/settings#sending")
        shot(page, "settings.png")

        visitor = browser.new_context(viewport=WIDE).new_page()
        visitor.goto(unsubscribe.replace("http://127.0.0.1:5013", BASE).replace("&amp;", "&"))
        shot(visitor, "unsubscribe.png", full=False)

        phone = browser.new_context(viewport=PHONE, is_mobile=True)
        phone_page = phone.new_page()
        start_demo(phone_page)
        phone_page.goto(BASE + "/stage")
        shot(phone_page, "phone-stage.png", full=False)
        phone_page.goto(BASE + "/shows/?tab=done")
        phone_page.click("text=Season Opener: Ang Huling Ilaw")
        shot(phone_page, "phone-show.png", full=False)
        browser.close()


if __name__ == "__main__":
    main()
