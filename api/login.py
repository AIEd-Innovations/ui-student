"""Vercel serverless function — GET /api/login

Redirects the browser to the Microsoft Entra ID authorization page and
stores a random CSRF state in a short-lived HttpOnly cookie. The state is
verified in /api/callback before the code is exchanged.
"""

from __future__ import annotations

import os
import secrets
from http.server import BaseHTTPRequestHandler
from urllib.parse import quote

MS_TENANT_ID = os.environ.get("MS_TENANT_ID", "")
MS_CLIENT_ID = os.environ.get("MS_CLIENT_ID", "")
MS_REDIRECT_URI = os.environ.get(
    "MS_REDIRECT_URI", "https://ui-student-ashen.vercel.app/api/callback"
)
COOKIE_SECURE = os.environ.get("ADFEL_COOKIE_SECURE", "1").lower() in ("1", "true", "yes")

_STATE_COOKIE = "adfel_ms_state"


class handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if not (MS_TENANT_ID and MS_CLIENT_ID):
            self._html(
                "<p>Microsoft auth is not configured.<br>"
                "Set <code>MS_TENANT_ID</code> and <code>MS_CLIENT_ID</code> "
                "in Vercel environment variables.</p>",
                500,
            )
            return

        state = secrets.token_urlsafe(32)
        auth_url = (
            f"https://login.microsoftonline.com/{MS_TENANT_ID}/oauth2/v2.0/authorize"
            f"?client_id={MS_CLIENT_ID}"
            f"&response_type=code"
            f"&redirect_uri={quote(MS_REDIRECT_URI, safe='')}"
            f"&scope=openid+email+profile+User.Read"
            f"&state={state}"
            f"&response_mode=query"
        )
        secure = "; Secure" if COOKIE_SECURE else ""
        self.send_response(302)
        self.send_header("Location", auth_url)
        self.send_header(
            "Set-Cookie",
            f"{_STATE_COOKIE}={state}; HttpOnly; SameSite=Lax; Max-Age=600; Path=/{secure}",
        )
        self.end_headers()

    def _html(self, body: str, status: int) -> None:
        content = f"<!DOCTYPE html><html><body>{body}</body></html>".encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def log_message(self, fmt, *args) -> None:  # silence Vercel access logs
        pass
