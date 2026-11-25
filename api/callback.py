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
import html as _html_escape
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
            self._error_page(
                "Sign-in failed",
                f"Microsoft returned an error: {_html_escape.escape(error_desc or error)}",
            )
            return

        if not code:
            self._redirect("/login")
            return

        # CSRF check — state sent to MS must match what we stored in the cookie
        expected = _get_cookie(self.headers.get("Cookie", ""), _STATE_COOKIE)
        if not expected or expected != state:
            self._error_page("Sign-in failed", "Security check failed. This can happen if you took too long to sign in or opened multiple tabs.")
            return

        # Back-channel code → token exchange (MS_CLIENT_SECRET never leaves server)
        try:
            tokens = _exchange_code(code)
        except Exception:
            self._error_page("Sign-in failed", "Could not connect to Microsoft. Please try again.")
            return

        if "error" in tokens:
            desc = tokens.get("error_description", tokens["error"])
            self._error_page("Sign-in failed", _html_escape.escape(str(desc)))
            return

        access_token = tokens.get("access_token", "")
        if not access_token:
            self._error_page("Sign-in failed", "No access token returned by Microsoft.")
            return

        # Fetch user profile from Microsoft Graph
        try:
            profile = _get_profile(access_token)
        except Exception:
            self._error_page("Sign-in failed", "Could not retrieve your profile from Microsoft.")
            return

        # Prefer mail; fall back to userPrincipalName (Cal Poly UPN = netid@calpoly.edu)
        email = profile.get("mail") or profile.get("userPrincipalName") or ""
        name = profile.get("displayName") or ""

        if not email:
            self._error_page("Sign-in failed", "No email address found in your Microsoft profile.")
            return

        # Restrict to Cal Poly accounts only
        allowed_domain = os.environ.get("MS_EMAIL_DOMAIN", "calpoly.edu")
        if not email.lower().endswith(f"@{allowed_domain}"):
            self._error_page(
                "Access denied",
                f"Only <strong>@{allowed_domain}</strong> accounts are allowed. "
                f"You signed in as <em>{_html_escape.escape(email)}</em>.<br><br>"
                f"Please use your Cal Poly Microsoft account.",
                is_403=True,
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
        self.send_header("Set-Cookie", f"{_STATE_COOKIE}=; Max-Age=0; Path=/; SameSite=None; Secure")
        self.end_headers()

    def _redirect(self, url: str) -> None:
        self.send_response(302)
        self.send_header("Location", url)
        self.end_headers()

    def _error_page(self, title: str, detail: str, is_403: bool = False) -> None:
        status = 403 if is_403 else 401
        content = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{_html_escape.escape(title)} — Lab Companion</title>
<style>
  *,*::before,*::after{{box-sizing:border-box;margin:0;padding:0}}
  body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;
    background:#0a0a0a;color:#e5e5e5;min-height:100vh;display:flex;
    align-items:center;justify-content:center;padding:24px}}
  body::before{{content:'';position:fixed;inset:0;
    background:radial-gradient(ellipse 80% 60% at 50% -10%,rgba(79,70,229,.08) 0%,transparent 70%);
    pointer-events:none}}
  .card{{background:#141414;border:1px solid #222;border-radius:20px;
    padding:48px 40px;width:100%;max-width:400px;text-align:center;
    box-shadow:0 24px 80px rgba(0,0,0,.5);position:relative;z-index:1}}
  .icon{{font-size:40px;margin-bottom:20px}}
  h1{{font-size:20px;font-weight:700;color:#f5f5f5;margin-bottom:12px}}
  p{{font-size:14px;color:#888;line-height:1.7;margin-bottom:24px}}
  a.btn{{display:inline-flex;align-items:center;gap:10px;padding:11px 24px;
    background:#fff;color:#3c3c3c;border-radius:4px;text-decoration:none;
    font-size:14px;font-weight:600;font-family:'Segoe UI',sans-serif;
    box-shadow:0 2px 8px rgba(0,0,0,.3);transition:background .15s}}
  a.btn:hover{{background:#f3f3f3}}
  .ms-logo{{width:18px;height:18px;flex-shrink:0}}
</style>
</head>
<body>
<div class="card">
  <div class="icon">{"🚫" if is_403 else "⚠️"}</div>
  <h1>{_html_escape.escape(title)}</h1>
  <p>{detail}</p>
  <a class="btn" href="/api/login">
    <svg class="ms-logo" viewBox="0 0 21 21" xmlns="http://www.w3.org/2000/svg">
      <rect x="1"  y="1"  width="9" height="9" fill="#f25022"/>
      <rect x="11" y="1"  width="9" height="9" fill="#7fba00"/>
      <rect x="1"  y="11" width="9" height="9" fill="#00a4ef"/>
      <rect x="11" y="11" width="9" height="9" fill="#ffb900"/>
    </svg>
    Try again
  </a>
</div>
</body>
</html>""".encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def log_message(self, fmt, *args) -> None:
        pass
