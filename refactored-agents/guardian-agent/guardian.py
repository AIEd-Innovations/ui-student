"""Standalone Guardian policy gate.

The caller owns durable session state. This agent accepts one state snapshot and
returns the next snapshot after classification; it does not access a database.
"""

from __future__ import annotations

from base_agent import BaseAgent, LLMClient
from models import GuidanceLevel, QuestionClassification
from policy import classify_question, derive_guidance_level, verify_response


def _state(raw: object) -> dict:
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ValueError("session_state must be an object")
    question_count = raw.get("question_count", 0)
    violation_count = raw.get("violation_count", 0)
    escalated = raw.get("escalated", False)
    if (type(question_count) is not int or type(violation_count) is not int
            or question_count < 0 or violation_count < 0
            or violation_count > question_count or type(escalated) is not bool):
        raise ValueError("session_state has invalid counters")
    return {"question_count": question_count,
            "violation_count": violation_count, "escalated": escalated}


def _nonempty(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a nonempty string")
    return value.strip()


class Guardian(BaseAgent):
    def __init__(self, *, llm: LLMClient) -> None:
        super().__init__(llm=llm)

    def process_request(self, request: dict) -> dict:
        try:
            if not isinstance(request, dict):
                raise ValueError("Request must be a JSON object")
            action = request.get("action", "validate")
            self.update_status("working")
            if action == "validate":
                result = self.validate(request)
            elif action == "verify":
                result = self.verify(request)
            else:
                raise ValueError("action must be validate or verify")
            self.update_status("completed")
            return {"status": self.status, **result}
        except (ValueError, TypeError, KeyError) as exc:
            self.update_status("error")
            return {"status": self.status, "error": str(exc)}
        except RuntimeError as exc:
            self.update_status("error")
            return {"status": self.status, "error": str(exc), "error_type": "model"}

    def validate(self, request: dict) -> dict:
        question = _nonempty(request.get("question"), "question")
        state = _state(request.get("session_state"))
        history = request.get("conversation_history", [])
        if not isinstance(history, list) or any(
            not isinstance(item, dict) or item.get("role") not in ("user", "assistant")
            or not isinstance(item.get("content"), str) for item in history
        ):
            raise ValueError("conversation_history must contain role/content objects")
        context = request.get("context", "")
        if not isinstance(context, str):
            raise ValueError("context must be a string")
        lab_id = request.get("lab_id", "unknown")
        if not isinstance(lab_id, str):
            raise ValueError("lab_id must be a string")

        question_count = state["question_count"] + 1
        violation_count = state["violation_count"]
        try:
            decision = classify_question(
                question_text=question,
                conversation_history=history,
                session_context={"lab_id": lab_id, "question_count": question_count,
                                 "violation_count": violation_count},
                llm=self._llm,
                rag_context=context[:800],
            )
        except Exception as exc:
            raise RuntimeError(f"Classifier could not make a decision: {exc}") from exc

        classification = decision.classification
        violation_type = None
        if classification == QuestionClassification.DIRECT_SOLUTION:
            violation_type = "DIRECT_SOLUTION_REQUEST"
        elif classification == QuestionClassification.ANSWER_FARMING:
            violation_type = "ANSWER_FARMING"
        if violation_type:
            violation_count += 1
        escalated = state["escalated"] or violation_count >= 3
        guidance = derive_guidance_level(
            classification, question_count, violation_count, escalated
        )
        next_state = {"question_count": question_count,
                      "violation_count": violation_count, "escalated": escalated}
        return {
            "classification": classification.value,
            "confidence": decision.confidence,
            "reasoning": decision.reasoning,
            "concept_tags": decision.concept_tags,
            "guidance_level": guidance.value,
            "violation_detected": bool(violation_type),
            "violation_type": violation_type,
            "session_state": next_state,
            "classifier_fallback": False,
        }

    def verify(self, request: dict) -> dict:
        question = _nonempty(request.get("question"), "question")
        draft = _nonempty(request.get("draft_response"), "draft_response")
        try:
            guidance = GuidanceLevel(request["guidance_level"])
        except (KeyError, ValueError, TypeError) as exc:
            raise ValueError("guidance_level must be FULL, MODERATE, MINIMAL, or REJECTED") from exc
        raw_classification = request.get("classification")
        try:
            classification = (QuestionClassification(raw_classification)
                              if raw_classification is not None else None)
        except ValueError as exc:
            raise ValueError("classification is invalid") from exc
        try:
            decision = verify_response(question, draft, guidance, classification, self._llm)
        except Exception as exc:
            raise RuntimeError(f"Verifier could not make a decision: {exc}") from exc
        return {"passes": decision.passes, "confidence": decision.confidence,
                "reason": decision.reason,
                "feedback_for_companion": decision.feedback_for_companion if not decision.passes else None,
                "verifier_fallback": False}
