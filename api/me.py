"""Vercel serverless function — GET /api/me

Verifies the session cookie and returns the authenticated user's email and
name. Returns 401 if the session is missing or expired.

The front-end calls this on page load: if it gets a 401 it redirects the
browser to /api/login, so unauthenticated users never see the chat UI.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from http.server import BaseHTTPRequestHandler

SESSION_JWT_SECRET = os.environ.get("SESSION_JWT_SECRET", "")
_SESSION_COOKIE = "adfel_session"


# ---- minimal HS256 JWT verify (stdlib only) ---------------------------------

def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _b64url_decode(s: str) -> bytes:
    padding = 4 - len(s) % 4
    return base64.urlsafe_b64decode(s + "=" * padding)


def _verify_token(token: str) -> dict | None:
    if not SESSION_JWT_SECRET or not token:
        return None
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return None
        header, payload, sig = parts
        msg = f"{header}.{payload}".encode()
        expected = _b64url(hmac.new(SESSION_JWT_SECRET.encode(), msg, hashlib.sha256).digest())
        if not hmac.compare_digest(sig, expected):
            return None
        claims = json.loads(_b64url_decode(payload))
        if claims.get("exp", 0) < time.time():
            return None
        return claims
    except Exception:
        return None


def _get_cookie(cookie_header: str, name: str) -> str:
    for part in cookie_header.split(";"):
        k, _, v = part.strip().partition("=")
        if k.strip() == name:
            return v.strip()
    return ""


# ---- Vercel handler ---------------------------------------------------------

class handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        token = _get_cookie(self.headers.get("Cookie", ""), _SESSION_COOKIE)
        claims = _verify_token(token)
        if not claims:
            self._json({"error": "unauthorized"}, 401)
            return
        self._json({"email": claims.get("email", ""), "name": claims.get("name", "")}, 200)

    def _json(self, data: dict, status: int) -> None:
        payload = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, fmt, *args) -> None:
        pass
