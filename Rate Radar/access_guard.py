"""Access key for the Rate Radar service.

Until now every route of this service (start a crawl, upload a bank list, overwrite a rate, reset) answered anyone who knew
the address. With RATE_RADAR_ACCESS_KEY set, every route needs the key:

    X-Rate-Radar-Key: <key>              (the Hub's backend sends this)
    or HTTP Basic auth, any user name, the key as the password   (a person opening the page in a browser)

- On Railway (RAILWAY_ENVIRONMENT is set) and no key configured: every route answers 503. It never falls back to open.
- On a developer machine with no key: open, as before, so local runs keep working.
- The key is compared in constant time and is never logged or returned.
"""
import hmac
import os

from flask import Response, request


def configured_key(env=None):
    return ((env if env is not None else os.environ).get("RATE_RADAR_ACCESS_KEY") or "").strip()


def is_production(env=None):
    return bool((env if env is not None else os.environ).get("RAILWAY_ENVIRONMENT"))


def key_matches(supplied, key):
    """Constant-time comparison. An empty key or an empty value never matches."""
    if not supplied or not key:
        return False
    return hmac.compare_digest(supplied.encode("utf-8"), key.encode("utf-8"))


def guard():
    """Flask before_request hook: None lets the request through, a Response refuses it."""
    key = configured_key()
    if not key:
        if is_production():
            return Response("Rate Radar access key is not configured", 503)
        return None
    supplied = request.headers.get("X-Rate-Radar-Key", "")
    if not supplied:
        auth = request.authorization
        if auth is not None and auth.password:
            supplied = auth.password
    if key_matches(supplied, key):
        return None
    return Response("Access key required", 401, {"WWW-Authenticate": 'Basic realm="Rate Radar"'})


def install(app):
    app.before_request(guard)
