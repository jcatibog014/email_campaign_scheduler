"""Right On Cue, an email campaign scheduler. "Emails that go out right on cue."

Make shows (campaigns), schedule them, and let the background worker send
every email: politely (rate limits), patiently (retries) and on time
(scheduling), then follow what happened to each one (delivery tracking).

Flask builds the app inside create_app() (the "app factory" pattern).
Tests call it with their own settings, so each test gets a fresh app and database.
"""
import mimetypes
import os
from datetime import timedelta
from pathlib import Path

from flask import Flask, g, render_template
from werkzeug.middleware.proxy_fix import ProxyFix

# Python doesn't know this one, and browsers want the right type for the app manifest.
mimetypes.add_type("application/manifest+json", ".webmanifest")

APP_NAME = "Right On Cue"
TAGLINE = "Emails that go out right on cue."
DEV_SECRET_KEY = "dev-only-not-secret"

# What the browser may load and talk to. Scripts, styles and fonts come only
# from this site, so even if someone sneaked a <script> into a show's name,
# the browser would refuse to run it. frame-src 'self': the practice inbox
# shows an email inside a frame (from its own, even stricter page).
CONTENT_SECURITY_POLICY = "; ".join([
    "default-src 'self'",
    "script-src 'self'",
    "style-src 'self'",
    "font-src 'self'",
    "img-src 'self' data:",
    "frame-src 'self'",
    "connect-src 'self'",
    "worker-src 'self'",  # the service worker, /sw.js
    "manifest-src 'self'",
    "object-src 'none'",
    "base-uri 'self'",
    "form-action 'self'",
    "frame-ancestors 'self'",
])


def env_flag(name, default):
    value = os.environ.get(name)
    return default if value is None else value.strip().lower() in ("1", "true", "yes", "on")


def create_app(test_config=None):
    app = Flask(__name__, instance_relative_config=True)
    on_render = bool(os.environ.get("RENDER"))  # Render sets RENDER=true on its servers
    app.config.from_mapping(
        # Signs the login cookie and every link in an email (unsubscribe, opens, clicks).
        SECRET_KEY=os.environ.get("SECRET_KEY", DEV_SECRET_KEY),
        # Local SQLite file, in the instance/ folder (git ignores it).
        DATABASE=str(Path(app.instance_path) / "oncue.sqlite"),
        # Set on the live site: then we use PostgreSQL (Neon) instead of SQLite.
        DATABASE_URL=os.environ.get("DATABASE_URL"),
        ON_RENDER=on_render,
        # The site's own address, for links inside emails (they are read far from
        # any web page, so they must be full addresses).
        BASE_URL=os.environ.get("BASE_URL", "http://127.0.0.1:5013").rstrip("/"),
        # Cookie safety: not readable by JavaScript (Flask's default), not sent
        # with forms from other sites, and only over HTTPS on the live site.
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=on_render,
        PERMANENT_SESSION_LIFETIME=timedelta(days=30),
        MAX_CONTENT_LENGTH=5 * 1000 * 1000,  # the biggest CSV import
        # Brevo (mailers.py): the owner's real sending. Without a key, the owner
        # sends to the practice mail server too.
        BREVO_API_KEY=os.environ.get("BREVO_API_KEY"),
        # The secret part of the address Brevo posts delivery news to (hooks.py).
        BREVO_WEBHOOK_SECRET=os.environ.get("BREVO_WEBHOOK_SECRET"),
        # The secret the GitHub Actions timer sends to POST /tasks/tick (tick.py).
        TICK_KEY=os.environ.get("TICK_KEY"),
        # A worker thread inside the web app (worker.py). Locally you can turn it
        # off (RUN_WORKER=0) and run `flask --app oncue worker` in its own window.
        RUN_WORKER=env_flag("RUN_WORKER", True),
        # The practice mail server really waits this long for a "slow" email.
        # Tests set it to 0.
        PRACTICE_REAL_DELAYS=True,
    )
    if test_config:
        app.config.update(test_config)
    if app.testing:
        app.config.setdefault("RUN_WORKER", False)
    if on_render:
        # Render's proxy talks HTTPS to the visitor and HTTP to us. This makes Flask
        # believe the X-Forwarded-* headers: links start with https://, and the rate
        # limits see the visitor's address instead of the proxy's.
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
    if on_render and app.config["SECRET_KEY"] == DEV_SECRET_KEY:
        raise RuntimeError("Set the SECRET_KEY environment variable on Render.")
    if on_render and not app.config["DATABASE_URL"]:
        # Without it we'd use a SQLite file, and Render forgets files on every restart.
        raise RuntimeError("Set the DATABASE_URL environment variable on Render (Neon's connection string).")

    from . import auth, cues, db, formatting, seed
    from .views import demo, settings, stage

    db.init_app(app)
    auth.init_app(app)
    formatting.init_app(app)
    cues.init_app(app)
    seed.init_app(app)
    settings.init_app(app)
    # auth comes first: its before_request (load_user) must run before the others'.
    for blueprint in (auth.bp, stage.bp, settings.bp, demo.bp):
        app.register_blueprint(blueprint)

    app.jinja_env.globals.update(APP_NAME=APP_NAME, TAGLINE=TAGLINE,
                                 has_page=lambda endpoint: endpoint in app.view_functions)

    @app.get("/healthz")
    def healthz():
        return "ok"

    @app.get("/sw.js")
    def service_worker():
        # From the top of the site, not /static/: a service worker only looks after
        # the pages below the address it came from.
        response = app.send_static_file("sw.js")
        response.headers["Cache-Control"] = "no-cache"  # always check for a new version
        return response

    @app.get("/favicon.ico")
    def favicon():
        return app.send_static_file("icons/favicon.ico")

    @app.after_request
    def security_headers(response):
        response.headers.setdefault("Content-Security-Policy", CONTENT_SECURITY_POLICY)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
        if g.get("user") is not None:
            # Pages with someone's audience on them are never kept by the browser or a proxy.
            response.headers.setdefault("Cache-Control", "no-store")
        if on_render:
            response.headers.setdefault("Strict-Transport-Security", "max-age=31536000")
        return response

    def error_page(code, message):
        return render_template("error.html", code=code, message=message), code

    @app.errorhandler(400)
    def bad_request(error):
        return error_page(400, error.description or "Something in that request was wrong.")

    @app.errorhandler(403)
    def forbidden(error):
        return error_page(403, error.description or "You can't do that.")

    @app.errorhandler(404)
    def not_found(error):
        return error_page(404, "We couldn't find that. It may have been deleted.")

    @app.errorhandler(409)
    def conflict(error):
        return error_page(409, error.description or "That can't be done right now.")

    @app.errorhandler(413)
    def too_big(error):
        return error_page(413, "That file is too big. A CSV file can be at most 5 MB.")

    @app.errorhandler(429)
    def too_many(error):
        return error_page(429, error.description or "Too many tries. Please wait a little and try again.")

    return app
