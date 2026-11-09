"""Standalone Participant — stateless learning-context tracker.

The caller owns conversation history and session counters. This agent accepts
one request snapshot and returns learning metadata; it does not access a database.

Two actions:
  classify  — LLM-tags one student question (type, hint_level, difficulty).
  summarize — generates a 2-3 sentence learning summary from conversation history
              and session counters, for the Lab Companion to use as context.
"""

from __future__ import annotations

import json
import logging

from base_agent import BaseAgent, LLMClient

logger = logging.getLogger(__name__)

CLASSIFICATION_SCHEMA = {
    "name": "participant_classification",
    "schema": {
        "type": "object",
        "properties": {
            "question_type": {
                "type": "string",
                "enum": ["debugging", "concept", "setup", "other"],
            },
            "hint_level": {"type": "integer", "enum": [1, 2, 3]},
            "difficulty": {"type": "string", "enum": ["low", "medium", "high"]},
        },
        "required": ["question_type", "hint_level", "difficulty"],
        "additionalProperties": False,
    },
}

CLASSIFICATION_SYSTEM_PROMPT = """You are a learning analytics assistant for a STEM lab tutoring system.

Classify the student's question into structured metadata used to track learning patterns.

Fields:
- question_type: one of "debugging" (error/bug in code or circuit), "concept" (theoretical understanding),
  "setup" (environment, tooling, lab logistics), "other" (anything else)
- hint_level: 1 (student just needs a nudge), 2 (student needs an explanation of the error pattern),
  3 (student needs a pointer to documentation or examples)
- difficulty: "low" (basic recall), "medium" (requires some reasoning), "high" (complex multi-step)

Respond with ONLY valid JSON matching the required schema."""

SUMMARIZE_SYSTEM_PROMPT = """You are a learning analytics assistant for a STEM lab tutoring system.

You will be given a student's conversation history and session counters.
Generate a concise 2-3 sentence summary of the student's learning behavior.
The summary will be used by the Lab Companion tutor to calibrate its hints.

Focus on:
- What topics the student is working on (from the conversation)
- How much assistance the student has needed
- Any patterns (e.g. confusion about a specific concept, repeated similar questions)

Be specific and actionable. No bullet points. Plain sentences only."""


def _nonempty(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a nonempty string")
    return value.strip()


def _avg_hint_level(hint_levels: list[int]) -> float:
    if not hint_levels:
        return 1.0
    return round(sum(hint_levels) / len(hint_levels), 2)


class ParticipantAgent(BaseAgent):
    def __init__(self, *, llm: LLMClient) -> None:
        super().__init__(llm=llm)

    def process_request(self, request: dict) -> dict:
        try:
            if not isinstance(request, dict):
                raise ValueError("Request must be a JSON object")
            action = request.get("action", "classify")
            self.update_status("working")
            if action == "classify":
                result = self._classify(request)
            elif action == "summarize":
                result = self._summarize(request)
            else:
                raise ValueError("action must be classify or summarize")
            self.update_status("completed")
            return {"status": self.status, **result}
        except (ValueError, TypeError, KeyError) as exc:
            self.update_status("error")
            return {"status": self.status, "error": str(exc)}
        except RuntimeError as exc:
            self.update_status("error")
            return {"status": self.status, "error": str(exc), "error_type": "model"}

    # ------------------------------------------------------------------ classify
    def _classify(self, request: dict) -> dict:
        question = _nonempty(request.get("question"), "question")
        try:
            raw = self._llm.complete(
                [
                    {"role": "system", "content": CLASSIFICATION_SYSTEM_PROMPT},
                    {"role": "user", "content": f"Student question:\n{question[:500]}"},
                ],
                temperature=0.1,
                max_tokens=120,
                json_mode=True,
                response_schema=CLASSIFICATION_SCHEMA,
            )
            parsed = json.loads(raw)
            return {
                "question_type": parsed.get("question_type", "other"),
                "hint_level": int(parsed.get("hint_level", 1)),
                "difficulty": parsed.get("difficulty", "medium"),
                "classifier_fallback": False,
            }
        except Exception as exc:
            logger.warning("Classification LLM failed; using fallback: %s", exc)
            return {
                "question_type": "other",
                "hint_level": 1,
                "difficulty": "medium",
                "classifier_fallback": True,
            }

    # ------------------------------------------------------------------ summarize
    def _summarize(self, request: dict) -> dict:
        history = request.get("conversation_history", [])
        if not isinstance(history, list):
            raise ValueError("conversation_history must be a list")

        session_state = request.get("session_state", {})
        if not isinstance(session_state, dict):
            raise ValueError("session_state must be an object")

        hint_levels = request.get("hint_levels", [])
        if not isinstance(hint_levels, list):
            raise ValueError("hint_levels must be a list")

        question_count = int(session_state.get("question_count", 0))
        violation_count = int(session_state.get("violation_count", 0))
        avg_hint = _avg_hint_level([int(h) for h in hint_levels if isinstance(h, (int, float))])

        if question_count == 0 or not history:
            return {
                "summary": "No prior context.",
                "avg_hint_level": 1.0,
                "summarizer_fallback": False,
            }

        history_text = "\n".join(
            f"{(t.get('role') or 'unknown').capitalize()}: {t.get('content', '')[:200]}"
            for t in history[-10:]
        )
        user_content = (
            f"Session stats:\n"
            f"- Questions asked: {question_count}\n"
            f"- Integrity concerns: {violation_count}\n"
            f"- Average hint level needed: {avg_hint:.1f} (1=minimal, 2=explain error, 3=point to docs)\n\n"
            f"Recent conversation (last {min(len(history), 10)} turns):\n{history_text}"
        )

        try:
            summary = self._llm.complete(
                [
                    {"role": "system", "content": SUMMARIZE_SYSTEM_PROMPT},
                    {"role": "user", "content": user_content},
                ],
                temperature=0.3,
                max_tokens=200,
            )
            return {
                "summary": summary,
                "avg_hint_level": avg_hint,
                "summarizer_fallback": False,
            }
        except Exception as exc:
            logger.warning("Summarize LLM failed; using rule-based fallback: %s", exc)
            summary = f"Student has asked {question_count} question(s) this session."
            if violation_count > 0:
                summary += f" {violation_count} integrity concern(s) flagged."
            if avg_hint >= 2.5:
                summary += " Often needs detailed explanations."
            elif avg_hint >= 1.5:
                summary += " Benefits from moderate hints."
            else:
                summary += " Usually understands with minimal hints."
            return {
                "summary": summary,
                "avg_hint_level": avg_hint,
                "summarizer_fallback": True,
            }
