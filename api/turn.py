"""Vercel serverless function — POST /api/turn

Three-agent pipeline: Guardian (validate) → Lab Companion → Guardian (verify) → Participant.
Session state is owned by the browser and round-tripped on every request.

Request JSON:
    {
        "question": "...",
        "session_state": {               # pass back what you received last turn
            "question_count": 0,
            "violation_count": 0,
            "escalated": false
        },
        "conversation_history": [...],   # [{role, content}, ...]
        "rag_context":    "...",         # optional — course material for this turn
        "learning_summary": "...",       # optional — from previous response
        "hint_levels": [1, 2, ...]       # optional — from previous responses
    }

Response JSON:
    {
        "status": "completed" | "error",
        "response": "...",
        "guidance_level": "FULL" | "MODERATE" | "MINIMAL" | "REJECTED",
        "classification": "CONCEPTUAL" | ...,
        "violation_detected": false,
        "session_state": { ... },        # store and send back next turn
        "learning_summary": "...",       # store and send back next turn
        "hint_levels": [...]             # store and send back next turn
    }
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import sys
import time
from http.server import BaseHTTPRequestHandler

# ---- session JWT verification (stdlib only, mirrors api/me.py) --------------

_SESSION_JWT_SECRET = os.environ.get("SESSION_JWT_SECRET", "")
_SESSION_COOKIE = "adfel_session"


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _b64url_decode(s: str) -> bytes:
    padding = 4 - len(s) % 4
    return base64.urlsafe_b64decode(s + "=" * padding)


_ALLOWED_ORIGIN = os.environ.get("ALLOWED_ORIGIN", "https://ui-student-ashen.vercel.app")
_MAX_PAYLOAD = 512 * 1024          # 512 KB hard cap on request body
_MAX_QUESTION_LEN = 2000           # characters
_MIN_REQUEST_GAP = 3               # seconds between turns per user
_RL_COOKIE = "adfel_rl"
_COOKIE_SECURE = os.environ.get("ADFEL_COOKIE_SECURE", "1").lower() in ("1", "true", "yes")


def _get_cookie(cookie_header: str, name: str) -> str:
    for part in cookie_header.split(";"):
        k, _, v = part.strip().partition("=")
        if k.strip() == name:
            return v.strip()
    return ""


def _verify_session(cookie_header: str) -> dict | None:
    token = _get_cookie(cookie_header, _SESSION_COOKIE)
    if not _SESSION_JWT_SECRET or not token:
        return None
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return None
        header, payload, sig = parts
        msg = f"{header}.{payload}".encode()
        expected = _b64url(hmac.new(_SESSION_JWT_SECRET.encode(), msg, hashlib.sha256).digest())
        if not hmac.compare_digest(sig, expected):
            return None
        claims = json.loads(_b64url_decode(payload))
        if claims.get("exp", 0) < time.time():
            return None
        return claims
    except Exception:
        return None


# ---- session state signing — prevents browser from tampering violation counts

def _sign_state(state: dict) -> dict:
    if not _SESSION_JWT_SECRET:
        return state
    clean = {k: v for k, v in state.items() if k != "_sig"}
    payload = json.dumps(clean, sort_keys=True)
    sig = _b64url(hmac.new(_SESSION_JWT_SECRET.encode(), payload.encode(), hashlib.sha256).digest())[:24]
    return {**clean, "_sig": sig}


def _verify_state(state: dict) -> dict:
    _default = {"question_count": 0, "violation_count": 0, "escalated": False}
    if not _SESSION_JWT_SECRET:
        return {k: state.get(k, v) for k, v in _default.items()}
    sig = state.get("_sig")
    if not sig:
        return _default  # no sig = fresh session, start clean
    clean = {k: v for k, v in state.items() if k != "_sig"}
    payload = json.dumps(clean, sort_keys=True)
    expected = _b64url(hmac.new(_SESSION_JWT_SECRET.encode(), payload.encode(), hashlib.sha256).digest())[:24]
    if not hmac.compare_digest(sig, expected):
        return _default  # tampered — reset state as penalty
    return clean


# ---- rate limiting via signed cookie (stateless, no external store needed)

def _make_rl_value() -> str:
    ts = str(int(time.time()))
    sig = _b64url(hmac.new(_SESSION_JWT_SECRET.encode(), ts.encode(), hashlib.sha256).digest())[:16]
    return f"{ts}|{sig}"


def _rate_limit_ok(cookie_header: str) -> bool:
    val = _get_cookie(cookie_header, _RL_COOKIE)
    if not val or not _SESSION_JWT_SECRET:
        return True
    try:
        ts, sig = val.split("|", 1)
        expected = _b64url(hmac.new(_SESSION_JWT_SECRET.encode(), ts.encode(), hashlib.sha256).digest())[:16]
        if not hmac.compare_digest(sig, expected):
            return True  # forged cookie — treat as first request
        return (time.time() - int(ts)) >= _MIN_REQUEST_GAP
    except Exception:
        return True

# ---------------------------------------------------------------------------
# Load refactored agents by absolute file path via importlib so we don't
# depend on sys.path resolution (the hyphenated directory names can't be
# Python packages, and Vercel's runtime cwd is unpredictable).
#
# Load order matters: guardian's llm.py has the richest complete() signature
# (json_mode + response_schema), so it must be registered as "llm" first.
# base_agent and models from guardian are shared by all three agents.
# ---------------------------------------------------------------------------
import importlib.util as _ilu

def _load(reg_name: str, file_path: str):
    """Load a .py file and register it in sys.modules under reg_name."""
    spec = _ilu.spec_from_file_location(reg_name, file_path)
    mod = _ilu.module_from_spec(spec)
    sys.modules[reg_name] = mod
    spec.loader.exec_module(mod)
    return mod

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.abspath(os.path.join(_HERE, "..", "refactored-agents"))
_GA   = os.path.join(_ROOT, "guardian-agent")
_LC   = os.path.join(_ROOT, "lab-companion-agent")
_PA   = os.path.join(_ROOT, "participant-agent")

# Shared infrastructure (guardian's versions — richest signatures)
_load("llm",        os.path.join(_GA, "llm.py"))
_load("models",     os.path.join(_GA, "models.py"))
_load("base_agent", os.path.join(_GA, "base_agent.py"))
_load("policy",     os.path.join(_GA, "policy.py"))

# Agents
_load("guardian",      os.path.join(_GA,  "guardian.py"))
_load("lab_companion", os.path.join(_LC,  "lab_companion.py"))
_load("participant",   os.path.join(_PA,  "participant.py"))

from llm import GatewayLLM
from guardian import Guardian
from lab_companion import LabCompanion as _LabCompanion
from participant import ParticipantAgent


# Guardian's base_agent.generate_response uses temperature=0.0 / json_mode=True,
# which is correct for Guardian but wrong for LabCompanion (plain text, temp=0.4).
# Override with a single line so the companion generates readable prose.
class LabCompanion(_LabCompanion):
    def generate_response(self, messages: list[dict]) -> str:
        return self._llm.complete(messages, temperature=0.4)


SAFE_FALLBACK = (
    "Let me rephrase — what part of the problem are you stuck on? "
    "Tell me what you've tried so far and where it broke down."
)
VERIFIER_MAX_RETRIES = 1
# Run Participant.summarize every N questions to keep LLM calls reasonable.
SUMMARIZE_EVERY = 3


def _build_agents(llm: GatewayLLM) -> tuple[Guardian, LabCompanion, ParticipantAgent]:
    return Guardian(llm=llm), LabCompanion(llm=llm), ParticipantAgent(llm=llm)


def _handle_turn(body: dict) -> dict:
    question = body.get("question", "").strip()
    if not question:
        raise ValueError("question must be a nonempty string")
    if len(question) > _MAX_QUESTION_LEN:
        raise ValueError(f"question exceeds {_MAX_QUESTION_LEN} character limit")

    session_state   = _verify_state(body.get("session_state") or {})
    conversation    = body.get("conversation_history") or []
    rag_context     = body.get("rag_context", "")
    learning_summary = body.get("learning_summary", "No prior context.")
    hint_levels     = body.get("hint_levels") or []

    llm = GatewayLLM.from_env()
    guardian, companion, participant = _build_agents(llm)

    # ------------------------------------------------------------------ 1. validate
    val = guardian.validate({
        "action": "validate",
        "question": question,
        "session_state": session_state,
        "conversation_history": conversation,
        "context": rag_context,
    })
    guidance_level  = val["guidance_level"]
    classification  = val["classification"]
    next_state      = val["session_state"]
    avg_hint = (sum(hint_levels) / len(hint_levels)) if hint_levels else 1.0

    # ------------------------------------------------------------------ 2. respond
    try:
        draft = companion.respond(
            question=question,
            conversation_history=conversation,
            learning_summary=learning_summary,
            avg_hint_level=avg_hint,
            guidance_level=guidance_level,
            rag_context=rag_context,
        )
    except Exception:
        draft = SAFE_FALLBACK

    # ------------------------------------------------------------------ 3. verify (skip for REJECTED)
    final_response = draft
    if guidance_level != "REJECTED":
        for attempt in range(VERIFIER_MAX_RETRIES + 1):
            ver = guardian.verify({
                "action": "verify",
                "question": question,
                "draft_response": draft,
                "guidance_level": guidance_level,
                "classification": classification,
            })
            if ver["passes"]:
                break
            if attempt < VERIFIER_MAX_RETRIES and ver.get("feedback_for_companion"):
                try:
                    draft = companion.respond(
                        question=question,
                        conversation_history=conversation,
                        learning_summary=learning_summary,
                        avg_hint_level=avg_hint,
                        guidance_level=guidance_level,
                        rag_context=rag_context,
                        verifier_feedback=ver["feedback_for_companion"],
                    )
                    final_response = draft
                except Exception:
                    final_response = SAFE_FALLBACK
                    break
            else:
                final_response = SAFE_FALLBACK
                break

    # ------------------------------------------------------------------ 4. classify (Participant)
    classify_result = participant.process_request({
        "action": "classify",
        "question": question,
    })
    if classify_result.get("status") == "completed":
        hint_levels = hint_levels + [classify_result.get("hint_level", 1)]

    # ------------------------------------------------------------------ 5. summarize (every N turns)
    new_summary = learning_summary
    q_count = next_state.get("question_count", 0)
    if q_count > 0 and q_count % SUMMARIZE_EVERY == 0:
        updated_history = list(conversation) + [
            {"role": "user", "content": question},
            {"role": "assistant", "content": final_response},
        ]
        summ_result = participant.process_request({
            "action": "summarize",
            "conversation_history": updated_history,
            "session_state": next_state,
            "hint_levels": hint_levels,
        })
        if summ_result.get("status") == "completed":
            new_summary = summ_result.get("summary", learning_summary)

    return {
        "status": "completed",
        "response": final_response,
        "guidance_level": guidance_level,
        "classification": classification,
        "violation_detected": val.get("violation_detected", False),
        "violation_type": val.get("violation_type"),
        "concept_tags": val.get("concept_tags", []),
        "session_state": _sign_state(next_state),
        "learning_summary": new_summary,
        "hint_levels": hint_levels[-20:],  # keep last 20 so payload stays small
    }


class handler(BaseHTTPRequestHandler):
    def do_OPTIONS(self) -> None:
        self.send_response(200)
        self._cors()
        self.end_headers()

    def do_POST(self) -> None:
        self._rl_cookie: str | None = None
        cookie_header = self.headers.get("Cookie", "")

        # Auth — 500 if misconfigured, 401 if session missing/expired
        if not _SESSION_JWT_SECRET:
            self._json({"status": "error", "error": "server misconfigured: SESSION_JWT_SECRET not set"}, 500)
            return
        if not _verify_session(cookie_header):
            self._json({"status": "error", "error": "unauthorized"}, 401)
            return

        # Rate limit — 3-second minimum gap per user (signed cookie, no DB needed)
        if not _rate_limit_ok(cookie_header):
            self._json({"status": "error", "error": "too many requests"}, 429)
            return
        secure = "; Secure" if _COOKIE_SECURE else ""
        self._rl_cookie = (
            f"{_RL_COOKIE}={_make_rl_value()}; HttpOnly; SameSite=Strict; Max-Age=60; Path=/{secure}"
        )

        # Payload size cap
        try:
            length = int(self.headers.get("Content-Length", 0))
        except (ValueError, TypeError):
            length = 0
        if length > _MAX_PAYLOAD:
            self._json({"status": "error", "error": "request body too large"}, 413)
            return

        try:
            body = json.loads(self.rfile.read(length)) if length else {}
        except (ValueError, TypeError) as exc:
            self._json({"status": "error", "error": str(exc)}, 400)
            return

        try:
            result = _handle_turn(body)
            self._json(result, 200)
        except ValueError as exc:
            self._json({"status": "error", "error": str(exc)}, 400)
        except Exception as exc:
            self._json({"status": "error", "error": str(exc)}, 502)

    def _cors(self) -> None:
        self.send_header("Access-Control-Allow-Origin", _ALLOWED_ORIGIN)
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Credentials", "true")

    def _json(self, data: dict, status: int) -> None:
        payload = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self._cors()
        if getattr(self, "_rl_cookie", None):
            self.send_header("Set-Cookie", self._rl_cookie)
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, fmt, *args):
        pass
