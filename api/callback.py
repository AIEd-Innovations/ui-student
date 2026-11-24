"""Vercel serverless function — GET /api/callback

Receives the Microsoft OAuth2 authorization code, verifies the CSRF state
cookie, exchanges the code back-channel with Microsoft's token endpoint,
calls Graph /me for the user's profile, mints an HS256 session JWT, and
redirects the browser back to the app root with an HttpOnly session cookie.

No external dependencies — uses Python stdlib only (hmac + urllib).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler

MS_TENANT_ID = os.environ.get("MS_TENANT_ID", "")
MS_CLIENT_ID = os.environ.get("MS_CLIENT_ID", "")
MS_CLIENT_SECRET = os.environ.get("MS_CLIENT_SECRET", "")
MS_REDIRECT_URI = os.environ.get(
    "MS_REDIRECT_URI", "https://ui-student-ashen.vercel.app/api/callback"
)
SESSION_JWT_SECRET = os.environ.get("SESSION_JWT_SECRET", "")
COOKIE_SECURE = os.environ.get("ADFEL_COOKIE_SECURE", "1").lower() in ("1", "true", "yes")

_SESSION_COOKIE = "adfel_session"
_STATE_COOKIE = "adfel_ms_state"
_SESSION_TTL = 28800  # 8 hours


# ---- minimal HS256 JWT (stdlib only) ----------------------------------------

def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _mint_token(email: str, name: str) -> str:
    if not SESSION_JWT_SECRET:
        raise RuntimeError("SESSION_JWT_SECRET is not set")
    now = int(time.time())
    header = _b64url(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    payload = _b64url(json.dumps({
        "sub": email,
        "email": email,
        "name": name,
        "iat": now,
        "exp": now + _SESSION_TTL,
    }).encode())
    msg = f"{header}.{payload}".encode()
    sig = _b64url(hmac.new(SESSION_JWT_SECRET.encode(), msg, hashlib.sha256).digest())
    return f"{header}.{payload}.{sig}"


# ---- cookie helper ----------------------------------------------------------

def _get_cookie(cookie_header: str, name: str) -> str:
    for part in cookie_header.split(";"):
        k, _, v = part.strip().partition("=")
        if k.strip() == name:
            return v.strip()
    return ""


# ---- Microsoft back-channel calls -------------------------------------------

def _exchange_code(code: str) -> dict:
    data = urllib.parse.urlencode({
        "client_id": MS_CLIENT_ID,
        "client_secret": MS_CLIENT_SECRET,
        "code": code,
        "redirect_uri": MS_REDIRECT_URI,
        "grant_type": "authorization_code",
        "scope": "openid email profile User.Read",
    }).encode()
    req = urllib.request.Request(
        f"https://login.microsoftonline.com/{MS_TENANT_ID}/oauth2/v2.0/token",
        data=data,
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read())


def _get_profile(access_token: str) -> dict:
    req = urllib.request.Request(
        "https://graph.microsoft.com/v1.0/me",
        headers={"Authorization": f"Bearer {access_token}"},
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read())


# ---- Vercel handler ---------------------------------------------------------

class handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        code = (qs.get("code") or [""])[0]
        state = (qs.get("state") or [""])[0]
        error = (qs.get("error") or [""])[0]
        error_desc = (qs.get("error_description") or [""])[0]

        if error:
            self._html(
                f"<p>Microsoft sign-in failed: {error_desc or error}</p>"
                "<p><a href='/api/login'>Try again</a></p>",
                401,
            )
            return

        if not code:
            self._redirect("/api/login")
            return

        # CSRF check — state sent to MS must match what we stored in the cookie
        expected = _get_cookie(self.headers.get("Cookie", ""), _STATE_COOKIE)
        if not expected or expected != state:
            self._html(
                "<p>Sign-in failed: state mismatch.</p>"
                "<p><a href='/api/login'>Try again</a></p>",
                401,
            )
            return

        # Back-channel code → token exchange (MS_CLIENT_SECRET never leaves server)
        try:
            tokens = _exchange_code(code)
        except Exception:
            self._html(
                "<p>Token exchange with Microsoft failed.</p>"
                "<p><a href='/api/login'>Try again</a></p>",
                502,
            )
            return

        if "error" in tokens:
            desc = tokens.get("error_description", tokens["error"])
            self._html(
                f"<p>Token error: {desc}</p>"
                "<p><a href='/api/login'>Try again</a></p>",
                401,
            )
            return

        access_token = tokens.get("access_token", "")
        if not access_token:
            self._html(
                "<p>No access token in Microsoft response.</p>"
                "<p><a href='/api/login'>Try again</a></p>",
                401,
            )
            return

        # Fetch user profile from Microsoft Graph
        try:
            profile = _get_profile(access_token)
        except Exception:
            self._html(
                "<p>Failed to fetch your Microsoft profile.</p>"
                "<p><a href='/api/login'>Try again</a></p>",
                502,
            )
            return

        # Prefer mail; fall back to userPrincipalName (Cal Poly UPN = netid@calpoly.edu)
        email = profile.get("mail") or profile.get("userPrincipalName") or ""
        name = profile.get("displayName") or ""

        if not email:
            self._html(
                "<p>No email address found in your Microsoft profile.</p>"
                "<p><a href='/api/login'>Try again</a></p>",
                401,
            )
            return

        try:
            token = _mint_token(email, name)
        except RuntimeError as exc:
            self._html(f"<p>Server configuration error: {exc}</p>", 500)
            return

        secure = "; Secure" if COOKIE_SECURE else ""
        self.send_response(302)
        self.send_header("Location", "/")
        self.send_header(
            "Set-Cookie",
            f"{_SESSION_COOKIE}={token}; HttpOnly; SameSite=Lax; "
            f"Max-Age={_SESSION_TTL}; Path=/{secure}",
        )
        self.send_header("Set-Cookie", f"{_STATE_COOKIE}=; Max-Age=0; Path=/")
        self.end_headers()

    def _redirect(self, url: str) -> None:
        self.send_response(302)
        self.send_header("Location", url)
        self.end_headers()

    def _html(self, body: str, status: int) -> None:
        content = f"<!DOCTYPE html><html><body>{body}</body></html>".encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def log_message(self, fmt, *args) -> None:
        pass
