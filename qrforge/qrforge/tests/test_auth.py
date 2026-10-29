"""Tests for the password gate, public mode, and the login throttle."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import create_app
from auth import LoginThrottle, password_matches, session_fingerprint

# A dummy password with the same kinds of characters as a real one (symbols and non-ASCII).
PASSWORD = "T3st:)@pw_ü"
SECRET = "unit-test-secret-key-0123456789abcdef"
GOOD_REQUEST = {"type": "text", "data": {"text": "hello"}}
HTTPS = "https://localhost"


def make_client(**extra_env):
    env = {"QRFORGE_PASSWORD": PASSWORD, "QRFORGE_SECRET_KEY": SECRET, **extra_env}
    return create_app(env).test_client()


class GateTests(unittest.TestCase):
    def setUp(self):
        self.client = make_client()

    def login(self, password=PASSWORD, **kwargs):
        return self.client.post("/api/login", json={"password": password}, **kwargs)

    def test_unauthenticated_page_is_login_not_app(self):
        page = self.client.get("/").get_data(as_text=True)
        self.assertIn('id="login-form"', page)
        self.assertNotIn('id="qr-form"', page)
        self.assertNotIn(PASSWORD, page)

    def test_unauthenticated_api_is_refused_with_json(self):
        response = self.client.post("/api/generate", json=GOOD_REQUEST)
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.get_json()["error"]["code"], "AUTH_REQUIRED")
        self.assertEqual(self.client.get("/api/nothing-here").status_code, 404)  # unknown URLs reveal nothing

    def test_public_endpoints_need_no_login(self):
        self.assertEqual(self.client.get("/healthz").status_code, 200)
        for path in ("/static/js/login.js", "/static/css/styles.css"):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200)
            response.close()

    def test_wrong_password_is_rejected_and_sets_no_cookie(self):
        response = self.login("wrong-password")
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.get_json()["error"]["code"], "INVALID_PASSWORD")
        self.assertFalse(response.headers.getlist("Set-Cookie"))
        self.assertEqual(self.client.post("/api/generate", json=GOOD_REQUEST).status_code, 401)

    def test_almost_right_passwords_fail(self):
        for bad in (PASSWORD + " ", PASSWORD[:-1], PASSWORD.upper(), " " + PASSWORD, PASSWORD + PASSWORD):
            with self.subTest(bad=bad):
                self.assertEqual(self.login(bad).status_code, 401)

    def test_malformed_login_requests(self):
        for body in ({}, {"password": ""}, {"password": 123}, {"password": None}, {"password": ["x"]}):
            with self.subTest(body=body):
                self.assertEqual(self.client.post("/api/login", json=body).status_code, 400)
        self.assertEqual(self.client.post("/api/login", data="nope", content_type="application/json").status_code, 400)

    def test_correct_password_unlocks_app_and_api(self):
        response = self.login()
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["ok"])
        page = self.client.get("/").get_data(as_text=True)
        self.assertIn('id="qr-form"', page)
        self.assertIn('id="logout-btn"', page)
        generated = self.client.post("/api/generate", json=GOOD_REQUEST)
        self.assertEqual(generated.status_code, 200)

    def test_session_cookie_flags(self):
        cookie = self.login().headers["Set-Cookie"]
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Lax", cookie)
        self.assertNotIn(PASSWORD, cookie)

    def test_logout_ends_session(self):
        self.login()
        self.assertEqual(self.client.post("/api/logout").status_code, 200)
        self.assertEqual(self.client.post("/api/generate", json=GOOD_REQUEST).status_code, 401)
        self.assertIn('id="login-form"', self.client.get("/").get_data(as_text=True))

    def test_changing_the_password_invalidates_existing_sessions(self):
        self.login()
        cookie = self.client.get_cookie("qrforge_session").value
        other = make_client(QRFORGE_PASSWORD="a-different-password")
        other.set_cookie("qrforge_session", cookie)
        self.assertEqual(other.post("/api/generate", json=GOOD_REQUEST).status_code, 401)

    def test_forged_cookie_is_rejected(self):
        self.client.set_cookie("qrforge_session", "eyJhdXRoIjp0cnVlfQ.forged.signature")
        self.assertEqual(self.client.post("/api/generate", json=GOOD_REQUEST).status_code, 401)

    def test_lockout_after_repeated_failures(self):
        for _ in range(5):
            self.assertEqual(self.login("nope").status_code, 401)
        locked = self.login(PASSWORD)  # even the right password is refused while locked
        self.assertEqual(locked.status_code, 429)
        self.assertEqual(locked.get_json()["error"]["code"], "TOO_MANY_ATTEMPTS")
        self.assertGreater(int(locked.headers["Retry-After"]), 0)

    def test_success_resets_failure_count(self):
        for _ in range(4):
            self.login("nope")
        self.assertEqual(self.login().status_code, 200)
        for _ in range(4):
            self.assertEqual(self.login("nope").status_code, 401)

    def test_no_password_means_no_gate(self):
        client = create_app({}).test_client()
        self.assertIn('id="qr-form"', client.get("/").get_data(as_text=True))
        self.assertNotIn('id="logout-btn"', client.get("/").get_data(as_text=True))
        self.assertEqual(client.post("/api/generate", json=GOOD_REQUEST).status_code, 200)

    def test_password_never_appears_in_responses_or_logs(self):
        with self.assertNoLogs("qrforge", level="INFO"):
            for response in (self.login("wrong"), self.login(), self.client.get("/")):
                self.assertNotIn(PASSWORD, response.get_data(as_text=True))


class PublicModeTests(unittest.TestCase):
    def test_refuses_to_start_without_password_or_secret(self):
        with self.assertRaises(RuntimeError):
            create_app({"QRFORGE_PUBLIC": "1"})
        with self.assertRaises(RuntimeError):
            create_app({"QRFORGE_PUBLIC": "1", "QRFORGE_PASSWORD": PASSWORD})
        with self.assertRaises(RuntimeError):
            create_app({"QRFORGE_PUBLIC": "1", "QRFORGE_SECRET_KEY": SECRET})
        create_app({"QRFORGE_PUBLIC": "1", "QRFORGE_PASSWORD": PASSWORD, "QRFORGE_SECRET_KEY": SECRET})

    def test_public_cookie_hsts_and_wording(self):
        client = make_client(QRFORGE_PUBLIC="1")
        response = client.post("/api/login", json={"password": PASSWORD}, base_url=HTTPS)
        self.assertIn("Secure", response.headers["Set-Cookie"])
        self.assertIn("max-age", response.headers["Strict-Transport-Security"])
        page = client.get("/", base_url=HTTPS).get_data(as_text=True)
        self.assertIn("Password protected", page)
        self.assertNotIn("Runs on your computer", page)
        self.assertIn("Is my content stored?", page)

    def test_local_wording_unchanged_without_public_mode(self):
        client = make_client()
        client.post("/api/login", json={"password": PASSWORD})
        page = client.get("/").get_data(as_text=True)
        self.assertIn("Runs on your computer", page)
        self.assertNotIn("Is my content stored?", page)

    def test_lockout_is_per_client_address_behind_proxy(self):
        client = make_client(QRFORGE_PUBLIC="1")

        def attempt(ip, password):
            return client.post("/api/login", json={"password": password}, base_url=HTTPS,
                               headers={"X-Forwarded-For": ip})

        for _ in range(5):
            attempt("203.0.113.5", "nope")
        self.assertEqual(attempt("203.0.113.5", PASSWORD).status_code, 429)
        self.assertEqual(attempt("198.51.100.7", PASSWORD).status_code, 200)

    def test_request_size_cap(self):
        client = make_client(QRFORGE_PUBLIC="1")
        client.post("/api/login", json={"password": PASSWORD}, base_url=HTTPS)
        big = {"type": "text", "data": {"text": "A" * (200 * 1024)}}
        self.assertEqual(client.post("/api/generate", json=big, base_url=HTTPS).status_code, 413)


class HelperTests(unittest.TestCase):
    def test_password_matches(self):
        self.assertTrue(password_matches(PASSWORD, PASSWORD))
        self.assertFalse(password_matches(PASSWORD, PASSWORD + "x"))
        self.assertFalse(password_matches("", PASSWORD))

    def test_fingerprint_depends_on_password_and_key(self):
        base = session_fingerprint(SECRET, PASSWORD)
        self.assertNotEqual(base, session_fingerprint(SECRET, PASSWORD + "x"))
        self.assertNotEqual(base, session_fingerprint(SECRET + "x", PASSWORD))
        self.assertNotIn(PASSWORD, base)

    def test_throttle_window_expires(self):
        now = [0.0]
        throttle = LoginThrottle(max_failures=3, window_seconds=60, clock=lambda: now[0])
        for _ in range(3):
            throttle.record_failure("a")
        self.assertGreater(throttle.retry_after("a"), 0)
        self.assertEqual(throttle.retry_after("b"), 0)
        now[0] = 61
        self.assertEqual(throttle.retry_after("a"), 0)


if __name__ == "__main__":
    unittest.main()
