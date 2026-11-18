"""
Chainlit client shell — thin HTTP/SSE proxy to the ADFEL server.

This file is the ONLY place Chainlit is imported. It holds no agent logic:
every turn is forwarded to the remote server via HTTP, and step-progress
events are streamed back via SSE.

Authentication supports **Microsoft Entra ID** (preferred) or **Cal Poly CAS**
as a fallback. The server is authoritative for token validation — this client
only drives the browser redirect and carries the resulting session JWT.

Microsoft flow (when MS_TENANT_ID + MS_CLIENT_ID + MS_REDIRECT_URI are set):
    browser → GET /login/ms             → 302 to Microsoft login (or mock)
    MS      → GET /login/ms/callback?code=…&state=…
            → POST {code} to server /api/v1/auth/ms/validate
            → server exchanges code back-channel, returns session JWT
            → set HttpOnly session cookie → 302 /

CAS flow (when CAS_BASE_URL + CAS_SERVICE_URL are set):
    browser → GET /login/cas            → 302 to Cal Poly CAS (or mock)
    CAS     → GET /login/cas/callback?ticket=…
            → POST {ticket} to server /api/v1/auth/cas/validate
            → set HttpOnly session cookie → 302 /

Required env (Microsoft):
    MS_TENANT_ID        Azure AD tenant ID (e.g. the Cal Poly tenant GUID)
    MS_CLIENT_ID        App registration client ID
    MS_REDIRECT_URI     Registered callback URL, e.g. https://host/login/ms/callback
    MS_MOCK             "1" to skip the real redirect (local dev)

Required env (CAS fallback):
    CAS_BASE_URL        Cal Poly CAS base, e.g. https://cas.calpoly.edu/cas
    CAS_SERVICE_URL     This app's approved callback
    CAS_MOCK            "1" to skip the real redirect (local dev)

Shared:
    ADFEL_SERVER_URL    Base URL of the FastAPI server (default http://localhost:8080)
    CHAINLIT_AUTH_SECRET  Signs Chainlit's own session (required once auth is on)
    ADFEL_COOKIE_SECURE "1" to mark the session cookie Secure (HTTPS only). Leave
                        blank for http://localhost or the browser drops the cookie.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import secrets
from http.cookies import SimpleCookie
from urllib.parse import quote

import chainlit as cl
import httpx
from chainlit.server import app as cl_app
from dotenv import load_dotenv
from fastapi import Request
from fastapi.responses import HTMLResponse, RedirectResponse

load_dotenv()

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

SERVER_URL = os.getenv("ADFEL_SERVER_URL", "http://localhost:8080")
COOKIE_SECURE = os.getenv("ADFEL_COOKIE_SECURE", "").lower() in ("1", "true", "yes")
SESSION_COOKIE = "adfel_session"
MS_STATE_COOKIE = "adfel_ms_state"

# Microsoft Entra ID (preferred auth provider)
MS_TENANT_ID = os.getenv("MS_TENANT_ID", "")
MS_CLIENT_ID = os.getenv("MS_CLIENT_ID", "")
MS_REDIRECT_URI = os.getenv("MS_REDIRECT_URI", "")
MS_MOCK = os.getenv("MS_MOCK", "").lower() in ("1", "true", "yes")
MS_CONFIGURED = MS_MOCK or bool(MS_TENANT_ID and MS_CLIENT_ID and MS_REDIRECT_URI)

# Cal Poly CAS (fallback when MS is not configured)
CAS_BASE_URL = os.getenv("CAS_BASE_URL", "")
CAS_SERVICE_URL = os.getenv("CAS_SERVICE_URL", "")
CAS_MOCK = os.getenv("CAS_MOCK", "").lower() in ("1", "true", "yes")
CAS_CONFIGURED = CAS_MOCK or bool(CAS_BASE_URL)

# Which login path to use — MS is preferred; falls back to CAS; None = dev bypass
_LOGIN_PATH = "/login/ms" if MS_CONFIGURED else "/login/cas" if CAS_CONFIGURED else None

_http: httpx.AsyncClient | None = None


def _client() -> httpx.AsyncClient:
    global _http
    if _http is None:
        _http = httpx.AsyncClient(base_url=SERVER_URL, timeout=httpx.Timeout(300.0))
    return _http


# -------------------------------------------------- Microsoft login routes

@cl_app.get("/login/ms")
async def login_ms():
    if MS_MOCK:
        return RedirectResponse("/login/ms/callback?code=MOCK&state=mock", status_code=302)
    if not (MS_TENANT_ID and MS_CLIENT_ID and MS_REDIRECT_URI):
        return HTMLResponse(
            "Microsoft auth is not configured on the client. Set MS_TENANT_ID, "
            "MS_CLIENT_ID, and MS_REDIRECT_URI (or MS_MOCK=1 for local dev).",
            status_code=500,
        )
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
    response = RedirectResponse(auth_url, status_code=302)
    response.set_cookie(
        MS_STATE_COOKIE, state,
        max_age=600, httponly=True, samesite="lax", secure=COOKIE_SECURE,
    )
    return response


@cl_app.get("/login/ms/callback")
async def login_ms_callback(
    request: Request,
    code: str = "",
    state: str = "",
    error: str = "",
    error_description: str = "",
):
    if error:
        logger.warning("Microsoft auth error: %s — %s", error, error_description)
        return HTMLResponse(
            f"Microsoft sign-in was declined: {error_description or error}. "
            "<a href='/login/ms'>Try again</a>.",
            status_code=401,
        )
    if not code:
        return RedirectResponse("/login/ms", status_code=302)

    # Verify CSRF state (skip for mock)
    if not MS_MOCK:
        expected = request.cookies.get(MS_STATE_COOKIE)
        if not expected or expected != state:
            logger.warning("Microsoft auth: state mismatch (CSRF?)")
            return HTMLResponse(
                "Sign-in failed: invalid state. <a href='/login/ms'>Try again</a>.",
                status_code=401,
            )

    try:
        resp = await _client().post("/api/v1/auth/ms/validate", json={"code": code})
        resp.raise_for_status()
        data = resp.json()
    except (httpx.HTTPStatusError, httpx.RequestError):
        logger.exception("Microsoft token validation failed")
        return HTMLResponse(
            "Sign-in failed. <a href='/login/ms'>Try again</a>.", status_code=401
        )

    redirect = RedirectResponse("/", status_code=302)
    redirect.set_cookie(
        SESSION_COOKIE,
        data["token"],
        max_age=data.get("expires_in"),
        httponly=True,
        samesite="lax",
        secure=COOKIE_SECURE,
        path="/",
    )
    redirect.delete_cookie(MS_STATE_COOKIE)
    return redirect


# ------------------------------------------------------- CAS login routes
# Mounted on Chainlit's own FastAPI app so the browser-redirect flow lives
# at the browser-facing tier without a separate proxy.

@cl_app.get("/login/cas")
async def login_cas():
    if CAS_MOCK:
        # Skip the real redirect; the server's mock accepts any ticket.
        return RedirectResponse("/login/cas/callback?ticket=MOCK", status_code=302)
    if not CAS_BASE_URL or not CAS_SERVICE_URL:
        return HTMLResponse(
            "CAS is not configured on the client. Set CAS_BASE_URL and "
            "CAS_SERVICE_URL (or CAS_MOCK=1 for local dev).",
            status_code=500,
        )
    # `service` must match the server's cas_service_url byte-for-byte.
    target = f"{CAS_BASE_URL.rstrip('/')}/login?service={quote(CAS_SERVICE_URL, safe='')}"
    return RedirectResponse(target, status_code=302)


@cl_app.get("/login/cas/callback")
async def login_cas_callback(ticket: str = ""):
    if not ticket:
        return RedirectResponse("/login/cas", status_code=302)
    try:
        resp = await _client().post(
            "/api/v1/auth/cas/validate", json={"ticket": ticket}
        )
        resp.raise_for_status()
        data = resp.json()
    except (httpx.HTTPStatusError, httpx.RequestError):
        logger.exception("CAS ticket validation failed")
        return HTMLResponse(
            "Sign-in failed. <a href='/login/cas'>Try again</a>.", status_code=401
        )

    redirect = RedirectResponse("/", status_code=302)
    redirect.set_cookie(
        SESSION_COOKIE,
        data["token"],
        max_age=data.get("expires_in"),
        httponly=True,
        samesite="lax",
        secure=COOKIE_SECURE,
        path="/",
    )
    return redirect


@cl_app.middleware("http")
async def _bounce_unauthenticated(request, call_next):
    """Redirect an unauthenticated browser hitting the app root to the login page."""
    if (
        _LOGIN_PATH
        and request.method == "GET"
        and request.url.path == "/"
        and SESSION_COOKIE not in request.cookies
    ):
        return RedirectResponse(_LOGIN_PATH, status_code=302)
    return await call_next(request)


# ------------------------------------------------------------------ auth

@cl.header_auth_callback
def header_auth_callback(headers: dict) -> cl.User | None:
    if not _LOGIN_PATH:
        # No auth provider configured — assume the server is in dev bypass.
        return cl.User(identifier="dev")

    token = _cookie_value(headers, SESSION_COOKIE)
    if not token:
        return None
    claims = _jwt_claims_unverified(token)  # server verifies the signature on every API call
    email = claims.get("email")
    if not email:
        return None
    return cl.User(
        identifier=email,
        metadata={"token": token, "role": claims.get("role", "student")},
    )


def _cookie_value(headers: dict, name: str) -> str | None:
    raw = headers.get("cookie") or headers.get("Cookie")
    if not raw:
        return None
    jar = SimpleCookie()
    try:
        jar.load(raw)
    except Exception:
        return None
    morsel = jar.get(name)
    return morsel.value if morsel else None


def _jwt_claims_unverified(token: str) -> dict:
    """Decode the JWT payload for display only (signature NOT verified here —
    the server verifies it on every API call)."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)  # restore base64 padding
        return json.loads(base64.urlsafe_b64decode(payload))
    except Exception:
        return {}


def _auth_headers() -> dict:
    """Per-user Bearer header pulled from the authed cl.User (never the
    shared httpx client, which would leak one user's token to all)."""
    user = cl.user_session.get("user")
    meta = getattr(user, "metadata", None) or {}
    token = meta.get("token")
    return {"Authorization": f"Bearer {token}"} if token else {}


# ------------------------------------------------------------------ callbacks

@cl.on_chat_start
async def on_start() -> None:
    client = _client()
    resp = await client.post("/api/v1/sessions", headers=_auth_headers())
    resp.raise_for_status()
    data = resp.json()

    cl.user_session.set("session_id", data["session_id"])

    intro = (
        "Welcome to the CSC 580 Lab Companion. I'll help you work through "
        "your assignments by giving hints, not answers. Ask me about the "
        "lab manual, concepts you want to understand, or errors you're "
        "running into."
    )
    if data.get("resuming"):
        intro += (
            f"\n\n*(Picking up where we left off — "
            f"{data['total_questions']} prior questions on file.)*"
        )
    await cl.Message(content=intro).send()


@cl.on_message
async def on_message(message: cl.Message) -> None:
    session_id: str | None = cl.user_session.get("session_id")
    if session_id is None:
        await on_start()
        session_id = cl.user_session.get("session_id")

    client = _client()
    url = f"/api/v1/sessions/{session_id}/turn"
    result_data: dict | None = None

    try:
        async with client.stream(
            "POST", url, json={"message": message.content}, headers=_auth_headers()
        ) as resp:
            resp.raise_for_status()
            event_type: str | None = None

            async for line in resp.aiter_lines():
                if line.startswith("event: "):
                    event_type = line[7:]
                elif line.startswith("data: ") and event_type:
                    data = json.loads(line[6:])

                    if event_type == "step":
                        async with cl.Step(name=data["name"], type=data["type"]) as step:
                            step.output = data["output"]
                    elif event_type == "result":
                        result_data = data
                    elif event_type == "error":
                        logger.error("Server error: %s", data.get("detail"))
                        await cl.Message(
                            content="Sorry — something went wrong on my side. Please try again."
                        ).send()
                        return

                    event_type = None

    except (httpx.HTTPStatusError, httpx.RequestError):
        logger.exception("Request to server failed")
        await cl.Message(
            content="Sorry — something went wrong on my side. Please try again."
        ).send()
        return

    if result_data:
        await cl.Message(content=result_data["response"]).send()


@cl.on_chat_end
async def on_end() -> None:
    session_id: str | None = cl.user_session.get("session_id")
    if session_id is None:
        return
    client = _client()
    try:
        await client.delete(
            f"/api/v1/sessions/{session_id}", headers=_auth_headers()
        )
    except Exception:
        logger.exception("end_session failed")
