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

import json
import os
import sys
from http.server import BaseHTTPRequestHandler

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

    session_state   = body.get("session_state")   or {}
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
        else:
            final_response = draft

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
        "session_state": next_state,
        "learning_summary": new_summary,
        "hint_levels": hint_levels[-20:],  # keep last 20 so payload stays small
    }


class handler(BaseHTTPRequestHandler):
    def do_OPTIONS(self) -> None:
        self.send_response(200)
        self._cors()
        self.end_headers()

    def do_POST(self) -> None:
        try:
            length = int(self.headers.get("Content-Length", 0))
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
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def _json(self, data: dict, status: int) -> None:
        payload = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self._cors()
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, fmt, *args):
        pass
