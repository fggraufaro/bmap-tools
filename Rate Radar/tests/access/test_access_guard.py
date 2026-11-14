"""Tests for access_guard (stdlib unittest: python -m unittest discover -s tests/access).

Run from the "Rate Radar" folder. Uses a tiny Flask app with the same routes that matter (start, upload, manual-save).
"""
import base64
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from flask import Flask, jsonify  # noqa: E402

import access_guard  # noqa: E402

KEY = "k" * 40  # test value, not a real key


def make_app():
    app = Flask(__name__)
    access_guard.install(app)

    @app.route("/status")
    def status():
        return jsonify({"running": False})

    @app.route("/start", methods=["POST"])
    def start():
        return jsonify({"ok": True})

    @app.route("/manual-save", methods=["POST"])
    def manual_save():
        return jsonify({"saved": 1})

    return app


def basic(password, user="anyone"):
    token = base64.b64encode(f"{user}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


class WithKey(unittest.TestCase):
    def setUp(self):
        p = mock.patch.dict(os.environ, {"RATE_RADAR_ACCESS_KEY": KEY, "RAILWAY_ENVIRONMENT": "production"}, clear=False)
        p.start()
        self.addCleanup(p.stop)
        self.c = make_app().test_client()

    def test_no_key_is_refused_with_a_browser_prompt(self):
        r = self.c.get("/status")
        self.assertEqual(r.status_code, 401)
        self.assertIn("Basic", r.headers["WWW-Authenticate"])

    def test_every_route_is_covered_not_only_reads(self):
        for method, path in (("post", "/start"), ("post", "/manual-save"), ("get", "/status")):
            self.assertEqual(getattr(self.c, method)(path).status_code, 401, path)

    def test_wrong_key_is_refused(self):
        self.assertEqual(self.c.get("/status", headers={"X-Rate-Radar-Key": "x" * 40}).status_code, 401)
        self.assertEqual(self.c.get("/status", headers=basic("x" * 40)).status_code, 401)

    def test_a_key_that_is_a_prefix_or_longer_is_refused(self):
        self.assertEqual(self.c.get("/status", headers={"X-Rate-Radar-Key": KEY[:-1]}).status_code, 401)
        self.assertEqual(self.c.get("/status", headers={"X-Rate-Radar-Key": KEY + "k"}).status_code, 401)

    def test_header_key_is_accepted(self):
        self.assertEqual(self.c.post("/start", headers={"X-Rate-Radar-Key": KEY}).status_code, 200)

    def test_browser_basic_auth_with_the_key_as_password_is_accepted(self):
        self.assertEqual(self.c.get("/status", headers=basic(KEY)).status_code, 200)
        self.assertEqual(self.c.get("/status", headers=basic(KEY, user="")).status_code, 200)

    def test_empty_password_is_refused(self):
        self.assertEqual(self.c.get("/status", headers=basic("")).status_code, 401)

    def test_the_key_is_never_in_a_refusal(self):
        r = self.c.get("/status", headers={"X-Rate-Radar-Key": "wrong"})
        self.assertNotIn(KEY, r.get_data(as_text=True))
        self.assertNotIn(KEY, str(r.headers))


class WithoutKey(unittest.TestCase):
    def test_production_without_a_key_refuses_everything(self):
        with mock.patch.dict(os.environ, {"RAILWAY_ENVIRONMENT": "production"}, clear=False):
            os.environ.pop("RATE_RADAR_ACCESS_KEY", None)
            c = make_app().test_client()
            self.assertEqual(c.get("/status").status_code, 503)
            self.assertEqual(c.post("/start").status_code, 503)

    def test_production_with_a_blank_key_refuses_everything(self):
        with mock.patch.dict(os.environ, {"RAILWAY_ENVIRONMENT": "production", "RATE_RADAR_ACCESS_KEY": "   "}, clear=False):
            self.assertEqual(make_app().test_client().get("/status").status_code, 503)

    def test_a_developer_machine_without_a_key_stays_open(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("RATE_RADAR_ACCESS_KEY", None)
            os.environ.pop("RAILWAY_ENVIRONMENT", None)
            self.assertEqual(make_app().test_client().get("/status").status_code, 200)

    def test_key_matches_helper_never_matches_empty(self):
        self.assertFalse(access_guard.key_matches("", KEY))
        self.assertFalse(access_guard.key_matches(KEY, ""))
        self.assertTrue(access_guard.key_matches(KEY, KEY))


if __name__ == "__main__":
    unittest.main()
