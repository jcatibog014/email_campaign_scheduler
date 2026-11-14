"""Signed tokens for the links inside emails: unsubscribe, opens and clicks.

An email is read far from this site, by someone who isn't logged in, so its
links must carry their own proof. A token is the facts in plain sight plus a
signature over them:

    make("unsubscribe", 41, 907)  ->  "41.907.Xq3...k2"

read() gives the facts back only if the signature matches. Changing 41 to 42
(to unsubscribe somebody else) breaks the signature, so it gets nothing.

The signature is an HMAC-SHA256 with a key made from SECRET_KEY and the
PURPOSE, so an "open" token can never be used as an "unsubscribe" one. Never
change SECRET_KEY on the live site: every link in every sent email would stop
working.
"""
import base64
import hashlib
import hmac

from flask import current_app

SIGNATURE_LENGTH = 22  # characters of base64: 132 bits, plenty for a link


def _key(purpose):
    secret = current_app.config["SECRET_KEY"].encode()
    return hmac.new(secret, f"oncue-token:{purpose}".encode(), hashlib.sha256).digest()


def _sign(purpose, body):
    digest = hmac.new(_key(purpose), body.encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).decode().rstrip("=")[:SIGNATURE_LENGTH]


def make(purpose, *parts):
    """A token for these parts (ints or short strings without dots)."""
    body = ".".join(str(part) for part in parts)
    return f"{body}.{_sign(purpose, body)}"


def read(purpose, token, count=None):
    """The parts as strings if the token is genuine (and has `count` parts), else None."""
    body, _, signature = (token or "").rpartition(".")
    if not body or len(token) > 300 or not hmac.compare_digest(signature, _sign(purpose, body)):
        return None
    parts = body.split(".")
    if count is not None and len(parts) != count:
        return None
    return parts


def read_ints(purpose, token, count):
    """Like read(), for tokens made of numbers: a tuple of ints, or None."""
    parts = read(purpose, token, count)
    if parts is None or not all(part.isdigit() for part in parts):
        return None
    return tuple(int(part) for part in parts)
