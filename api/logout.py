"""Vercel serverless function — GET /api/logout

Clears the session cookie and redirects to the login page.
"""

from __future__ import annotations

import os
from http.server import BaseHTTPRequestHandler

COOKIE_SECURE = os.environ.get("ADFEL_COOKIE_SECURE", "1").lower() in ("1", "true", "yes")


class handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        secure = "; Secure" if COOKIE_SECURE else ""
        self.send_response(302)
        self.send_header("Location", "/login")
        # Clear all session cookies
        self.send_header("Set-Cookie", f"adfel_session=; Max-Age=0; Path=/{secure}")
        self.send_header("Set-Cookie", f"adfel_rl=; Max-Age=0; Path=/")
        self.end_headers()

    def log_message(self, fmt, *args) -> None:
        pass
