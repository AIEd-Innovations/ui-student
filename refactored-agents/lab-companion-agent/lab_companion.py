"""
Lab Companion — the only student-facing agent.

Runs independently of the Student UI. The caller supplies optional course
resources and learning context; this agent does not perform retrieval.
"""

from __future__ import annotations

from typing import Optional

from base_agent import BaseAgent, LLMClient


GUIDANCE_INSTRUCTIONS = {
    "FULL": (
        "Use normal hint-style tutoring. You may explain concepts, walk through "
        "general procedures, and ask clarifying questions. Do NOT write a complete "
        "solution to the student's specific assignment problem; nudge them to "
        "produce the answer themselves."
    ),
    "MODERATE": (
        "Nudge harder toward independence. Prefer asking the student a clarifying "
        "question or pointing them to the relevant section/formula over giving a "
        "direct explanation. Keep the response short."
    ),
    "MINIMAL": (
        "Provide AT MOST ONE short hint, no elaboration. Do not write code. "
        "Do not list steps. One or two sentences. Encourage the student to think."
    ),
    "REJECTED": (
        "Politely decline to answer this specific question. Briefly note that "
        "the student should attempt the problem themselves first, or rephrase "
        "their question in a more conceptual way. Do not provide hints, code, "
        "values, or step-by-step guidance."
    ),
}


SAFE_FALLBACK = (
    "Let me rephrase — what part of the problem are you stuck on? "
    "Tell me what you've tried so far and where it broke down."
)


def _hint_level_descriptor(avg: float) -> str:
    if avg >= 2.5:
        return "tends to need detailed explanations and pointers to documentation"
    if avg >= 1.5:
        return "benefits from moderate hints and explanations of errors"
    return "usually understands with minimal hints"


def _build_system_prompt(
    *,
    learning_summary: str,
    avg_hint_level: float,
    guidance_level: str,
    rag_context: str,
    verifier_feedback: Optional[str] = None,
) -> str:
    guidance_block = GUIDANCE_INSTRUCTIONS.get(
        guidance_level, GUIDANCE_INSTRUCTIONS["FULL"]
    )

    parts = [
        "You are the Lab Companion, a tutoring assistant for a Cal Poly CSC "
        "lab course. You help students work through assignments by giving HINTS, "
        "not answers. Cal Poly's Honor Code requires that all submitted work be "
        "the student's own.",
        "",
        "Hard rules (apply at every guidance level):",
        "  - Never give a complete solution to the student's specific lab problem.",
        "  - Never paste a copy-pasteable code block that solves the assigned task.",
        "  - Never reveal the literal numerical or symbolic answer.",
        "  - You may give tiered hints: (1) check naming/setup, (2) explain the "
        "    error pattern, (3) point to a section/page of the docs.",
        "",
        f"Guidance level for THIS turn: {guidance_level}.",
        f"  -> {guidance_block}",
        "",
        "Student learning context:",
        f"  - Summary: {learning_summary}",
        f"  - Behavior: {_hint_level_descriptor(avg_hint_level)}",
        "",
        "If the question can't be answered from the course resources below, say so.",
        "",
        "--- BEGIN COURSE RESOURCES ---",
        rag_context,
        "--- END COURSE RESOURCES ---",
    ]

    if verifier_feedback:
        parts += [
            "",
            "IMPORTANT — your previous draft was rejected by the integrity reviewer.",
            "Reviewer feedback (incorporate exactly):",
            f"  {verifier_feedback}",
            "Regenerate the response so it satisfies this feedback.",
        ]
    return "\n".join(parts)


class LabCompanion(BaseAgent):
    def __init__(self, *, llm: LLMClient, history_keep_turns: int = 6) -> None:
        super().__init__(llm=llm)
        self.history_keep_turns = history_keep_turns

    def create_prompt(
        self,
        *,
        learning_summary: str,
        avg_hint_level: float,
        guidance_level: str,
        rag_context: str,
        verifier_feedback: Optional[str] = None,
    ) -> str:
        """Build this turn's system prompt from the supplied context."""
        return _build_system_prompt(
            learning_summary=learning_summary,
            avg_hint_level=avg_hint_level,
            guidance_level=guidance_level,
            rag_context=rag_context,
            verifier_feedback=verifier_feedback,
        )

    def process_request(self, request: dict) -> dict:
        """Run one local request and report the result and current status."""
        try:
            response = self.respond(
                question=request["question"],
                conversation_history=request.get("conversation_history", []),
                learning_summary=request.get("learning_summary", "No prior context."),
                avg_hint_level=request.get("avg_hint_level", 1.0),
                guidance_level=request.get("guidance_level", "FULL"),
                rag_context=request.get("rag_context", "No course resources supplied."),
                verifier_feedback=request.get("verifier_feedback"),
            )
            return {"status": self.status, "response": response}
        except Exception as exc:
            self.update_status("error")
            return {"status": self.status, "error": str(exc)}

    def respond(
        self,
        *,
        question: str,
        conversation_history: list[dict],
        learning_summary: str,
        avg_hint_level: float,
        guidance_level: str,
        rag_context: str,
        verifier_feedback: Optional[str] = None,
    ) -> str:
        """Generate one draft response. May raise on hard infra errors."""
        system_prompt = self.create_prompt(
            learning_summary=learning_summary,
            avg_hint_level=avg_hint_level,
            guidance_level=guidance_level,
            rag_context=rag_context,
            verifier_feedback=verifier_feedback,
        )

        keep = self.history_keep_turns
        history_trimmed = conversation_history[-keep:] if keep > 0 else []

        messages = [{"role": "system", "content": system_prompt}]
        messages.extend(history_trimmed)
        messages.append({"role": "user", "content": question})

        self.update_status("working")
        try:
            response = self.generate_response(messages)
        except Exception:
            self.update_status("error")
            raise
        self.update_status("completed")
        return response
