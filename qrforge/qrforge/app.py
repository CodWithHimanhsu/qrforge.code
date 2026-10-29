"""QRForge: a local-first QR code generator.

Run with ``python app.py`` and open http://127.0.0.1:5000.

This module only deals with HTTP: parsing requests, calling the validators
and the QR service, and shaping responses. QR logic lives in ``qr_service``,
validation and payload building in ``validators``.
"""

from __future__ import annotations

import base64
import logging
import math
import os
import secrets
import traceback
from datetime import timedelta
from typing import Mapping

from flask import Flask, jsonify, render_template, request, session
from werkzeug.exceptions import HTTPException
from werkzeug.middleware.proxy_fix import ProxyFix

from auth import LoginThrottle, password_matches, session_fingerprint

from qr_service import PayloadTooLargeError, generate_qr
from validators import ValidationError, contrast_warnings, parse_generation_request

logger = logging.getLogger("qrforge")

# Safeguard against abusive request bodies. A maximum-capacity QR payload is
# under 3 KB, so 128 KB is far above any legitimate request.
MAX_REQUEST_BYTES = 128 * 1024

# Endpoints reachable without logging in. Everything else under /api/ needs a session.
PUBLIC_ENDPOINTS = {"static", "index", "healthz", "api_login", "api_logout"}

# Short, fixed labels used in filenames. The payload is never used in a filename.
FILENAME_LABELS = {
    "url": "website", "text": "text", "wifi": "wifi", "contact": "contact",
    "email": "email", "phone": "phone", "sms": "sms",
}


def error_response(code: str, message: str, status: int, field: str | None = None):
    body = {"error": {"code": code, "message": message}}
    if field:
        body["error"]["field"] = field
    response = jsonify(body)
    response.status_code = status
    return response


def create_app(env: Mapping[str, str] | None = None) -> Flask:
    """Build the Flask app. ``env`` defaults to the process environment.

    Environment variables:
      QRFORGE_PASSWORD    Turns on the password gate. Unset = no gate (local use).
      QRFORGE_SECRET_KEY  Key that signs the login cookie (needed when public).
      QRFORGE_PUBLIC      "1" = public hosting mode: requires the two above,
                          trusts one proxy hop, marks cookies Secure, sends HSTS.
    """
    env = os.environ if env is None else env
    password = env.get("QRFORGE_PASSWORD") or None
    secret_key = env.get("QRFORGE_SECRET_KEY") or None
    public_mode = env.get("QRFORGE_PUBLIC") == "1"
    auth_enabled = password is not None

    if public_mode and not (password and secret_key):
        # Fail closed: never start a public instance that is accidentally open.
        raise RuntimeError("QRFORGE_PUBLIC=1 requires both QRFORGE_PASSWORD and QRFORGE_SECRET_KEY to be set.")
    if secret_key is None:
        secret_key = secrets.token_hex(32)  # local use: fine for a single process

    fingerprint = session_fingerprint(secret_key, password) if auth_enabled else ""
    throttle = LoginThrottle()

    app = Flask(__name__)
    app.config.update(
        MAX_CONTENT_LENGTH=MAX_REQUEST_BYTES,
        JSON_AS_ASCII=False,
        SECRET_KEY=secret_key,
        SESSION_COOKIE_NAME="qrforge_session",
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=public_mode,
        PERMANENT_SESSION_LIFETIME=timedelta(days=30),
    )
    if public_mode:
        # Behind the host's proxy, read the real client address and scheme (one hop only).
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

    def is_authenticated() -> bool:
        return auth_enabled and hmac_equal(str(session.get("auth", "")), fingerprint)

    @app.before_request
    def require_login():
        if not auth_enabled or request.endpoint is None or request.endpoint in PUBLIC_ENDPOINTS:
            return None
        if is_authenticated():
            return None
        return error_response("AUTH_REQUIRED", "Please sign in with the password to use QRForge.", 401)

    @app.after_request
    def add_security_headers(response):
        # Everything the page needs is served locally, so lock the page down.
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data: blob:; style-src 'self'; script-src 'self'; "
            "object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'",
        )
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        if public_mode:
            response.headers.setdefault("Strict-Transport-Security", "max-age=31536000")
        if request.path.startswith("/api/") or (auth_enabled and request.path == "/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/healthz")
    def healthz():
        return "ok", 200, {"Content-Type": "text/plain; charset=utf-8"}

    @app.get("/")
    def index():
        context = {"auth_enabled": auth_enabled, "public_mode": public_mode}
        if auth_enabled and not is_authenticated():
            return render_template("login.html", **context)
        return render_template("index.html", **context)

    @app.post("/api/login")
    def api_login():
        if not auth_enabled:
            return jsonify({"ok": True})
        client_key = request.remote_addr or "unknown"
        wait = throttle.retry_after(client_key)
        if wait:
            response = error_response(
                "TOO_MANY_ATTEMPTS",
                f"Too many incorrect attempts. Try again in about {math.ceil(wait / 60)} minute(s).",
                429,
            )
            response.headers["Retry-After"] = str(wait)
            return response

        body = request.get_json(silent=True)
        candidate = body.get("password") if isinstance(body, dict) else None
        if not isinstance(candidate, str) or not candidate:
            return error_response("MISSING_PASSWORD", "Enter the password.", 400)
        if len(candidate) > 256 or not password_matches(candidate, password):
            throttle.record_failure(client_key)
            return error_response("INVALID_PASSWORD", "Incorrect password.", 401)

        throttle.reset(client_key)
        session.clear()
        session["auth"] = fingerprint
        session.permanent = True
        return jsonify({"ok": True})

    @app.post("/api/logout")
    def api_logout():
        session.clear()
        return jsonify({"ok": True})

    @app.post("/api/generate")
    def api_generate():
        body = request.get_json(silent=True)
        if body is None:
            return error_response("MALFORMED_REQUEST", "The request body must be valid JSON.", 400)

        try:
            generation = parse_generation_request(body)
            result = generate_qr(generation.payload, generation.options)
        except ValidationError as exc:
            return error_response(exc.code, exc.message, exc.status, exc.field)
        except PayloadTooLargeError:
            return error_response(
                "PAYLOAD_TOO_LARGE",
                "This content is too large for a QR code at the selected error-correction level. "
                "QR capacity depends on the content and the level: try a lower level, shorten the "
                "text, or encode a short URL that points to the longer content.",
                413,
            )

        filename = f"qrforge-{FILENAME_LABELS[generation.content_type]}.{result.output_format}"
        warnings = list(generation.notes) + contrast_warnings(generation.options) + result.warnings

        return jsonify({
            "filename": filename,
            "mime_type": result.mime_type,
            "format": result.output_format,
            "image_base64": base64.b64encode(result.image_bytes).decode("ascii"),
            "payload": generation.payload,
            "version": result.version,
            "modules": result.modules,
            "error_correction": result.error_correction,
            "pixel_size": result.pixel_size,
            "quiet_zone": generation.options.border,
            "warnings": warnings,
        })

    @app.errorhandler(HTTPException)
    def handle_http_exception(exc: HTTPException):
        if request.path.startswith("/api/"):
            code = {404: "NOT_FOUND", 405: "METHOD_NOT_ALLOWED", 413: "REQUEST_TOO_LARGE"}.get(exc.code, "HTTP_ERROR")
            return error_response(code, exc.description if exc.code != 413 else "The request is too large.", exc.code or 500)
        return exc

    @app.errorhandler(Exception)
    def handle_unexpected(exc: Exception):
        # Log only the exception class and stack locations. Never the message
        # or variables, which could contain a password or personal data.
        frames = "".join(traceback.format_tb(exc.__traceback__))
        logger.error("Unexpected %s while handling %s\n%s", type(exc).__name__, request.path, frames)
        return error_response("INTERNAL_ERROR", "Something went wrong while generating the QR code. Please try again.", 500)

    return app


def hmac_equal(a: str, b: str) -> bool:
    """Constant-time string comparison for session values."""
    return password_matches(a, b)


app = create_app()

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    # Werkzeug logs request lines (method and path only, never bodies); keep it quiet.
    logging.getLogger("werkzeug").setLevel(logging.WARNING)
    port = int(os.environ.get("QRFORGE_PORT", "5000"))
    print(f"QRForge is running at http://127.0.0.1:{port}  (press Ctrl+C to stop)")
    if os.environ.get("QRFORGE_PASSWORD"):
        print("Password protection is ON (QRFORGE_PASSWORD is set).")
    # Loopback only: not reachable from other devices. Debug mode is off.
    app.run(host="127.0.0.1", port=port, debug=False)
