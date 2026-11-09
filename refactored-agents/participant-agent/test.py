"""Unit tests for the Participant agent.

Run offline:  python3 test.py
Run live:     python3 test.py --live
"""

from __future__ import annotations

import json
import sys
import os

sys.path.insert(0, os.path.dirname(__file__))

from participant import ParticipantAgent


# ------------------------------------------------------------------ fakes
class FakeLLM:
    """Returns deterministic JSON for classification and plain text for summarize."""

    def __init__(self, classify_response: dict | None = None, summary_text: str = "") -> None:
        self._classify = classify_response or {
            "question_type": "concept",
            "hint_level": 2,
            "difficulty": "medium",
        }
        self._summary = summary_text or "Student is working on circuit analysis concepts."
        self.calls: list[dict] = []

    def complete(self, messages, *, temperature=0.3, max_tokens=200,
                 json_mode=False, response_schema=None) -> str:
        self.calls.append({"messages": messages, "temperature": temperature})
        # detect which action by looking at the system prompt
        system = messages[0].get("content", "")
        if "question_type" in system or json_mode or response_schema:
            return json.dumps(self._classify)
        return self._summary


class FailingLLM:
    def complete(self, *args, **kwargs) -> str:
        raise RuntimeError("Simulated model failure")


# ------------------------------------------------------------------ tests
def test_classify_basic():
    agent = ParticipantAgent(llm=FakeLLM())
    result = agent.process_request({"action": "classify", "question": "What is KVL?"})
    assert result["status"] == "completed", result
    assert result["question_type"] == "concept"
    assert result["hint_level"] == 2
    assert result["difficulty"] == "medium"
    assert result["classifier_fallback"] is False
    print("PASS test_classify_basic")


def test_classify_fallback_on_model_error():
    agent = ParticipantAgent(llm=FailingLLM())
    result = agent.process_request({"action": "classify", "question": "What is KVL?"})
    assert result["status"] == "completed", result
    assert result["classifier_fallback"] is True
    assert result["question_type"] == "other"
    print("PASS test_classify_fallback_on_model_error")


def test_classify_missing_question():
    agent = ParticipantAgent(llm=FakeLLM())
    result = agent.process_request({"action": "classify"})
    assert result["status"] == "error"
    assert "question" in result["error"]
    print("PASS test_classify_missing_question")


def test_summarize_no_history():
    agent = ParticipantAgent(llm=FakeLLM())
    result = agent.process_request({
        "action": "summarize",
        "conversation_history": [],
        "session_state": {"question_count": 0, "violation_count": 0},
        "hint_levels": [],
    })
    assert result["status"] == "completed"
    assert result["summary"] == "No prior context."
    assert result["avg_hint_level"] == 1.0
    print("PASS test_summarize_no_history")


def test_summarize_with_history():
    history = [
        {"role": "user", "content": "What is KVL?"},
        {"role": "assistant", "content": "KVL says voltages sum to zero in a loop."},
        {"role": "user", "content": "How do I apply it?"},
        {"role": "assistant", "content": "Label each element and write the loop equation."},
    ]
    agent = ParticipantAgent(llm=FakeLLM(summary_text="Student is exploring KVL."))
    result = agent.process_request({
        "action": "summarize",
        "conversation_history": history,
        "session_state": {"question_count": 2, "violation_count": 0},
        "hint_levels": [1, 2],
    })
    assert result["status"] == "completed"
    assert result["avg_hint_level"] == 1.5
    assert "Student" in result["summary"]
    print("PASS test_summarize_with_history")


def test_summarize_fallback_on_model_error():
    history = [
        {"role": "user", "content": "Help with my circuit."},
        {"role": "assistant", "content": "What have you tried?"},
    ]
    agent = ParticipantAgent(llm=FailingLLM())
    result = agent.process_request({
        "action": "summarize",
        "conversation_history": history,
        "session_state": {"question_count": 1, "violation_count": 0},
        "hint_levels": [2],
    })
    assert result["status"] == "completed"
    assert result["summarizer_fallback"] is True
    assert "1 question" in result["summary"]
    print("PASS test_summarize_fallback_on_model_error")


def test_violation_in_summary():
    agent = ParticipantAgent(llm=FailingLLM())
    result = agent.process_request({
        "action": "summarize",
        "conversation_history": [{"role": "user", "content": "Give me the answer."}],
        "session_state": {"question_count": 1, "violation_count": 1},
        "hint_levels": [3],
    })
    assert result["status"] == "completed"
    assert "1 integrity concern" in result["summary"]
    print("PASS test_violation_in_summary")


def test_invalid_action():
    agent = ParticipantAgent(llm=FakeLLM())
    result = agent.process_request({"action": "unknown"})
    assert result["status"] == "error"
    print("PASS test_invalid_action")


def test_gateway_request_shape():
    llm = FakeLLM()
    agent = ParticipantAgent(llm=llm)
    agent.process_request({"action": "classify", "question": "What is KVL?"})
    call = llm.calls[0]
    assert call["messages"][0]["role"] == "system"
    assert call["messages"][1]["role"] == "user"
    assert call["temperature"] == 0.1
    print("PASS test_gateway_request_shape")


def _live_test():
    from llm import GatewayLLM
    llm = GatewayLLM.from_env()
    agent = ParticipantAgent(llm=llm)

    print("\n--- LIVE: classify ---")
    result = agent.process_request({
        "action": "classify",
        "question": "Why isn't my resistor voltage matching KVL?"
    })
    print(json.dumps(result, indent=2))

    print("\n--- LIVE: summarize ---")
    history = [
        {"role": "user", "content": "What is KVL?"},
        {"role": "assistant", "content": "KVL says voltages around a loop sum to zero."},
        {"role": "user", "content": "How do I set up the equation?"},
        {"role": "assistant", "content": "Label elements, pick a direction, and sum the drops."},
    ]
    result = agent.process_request({
        "action": "summarize",
        "conversation_history": history,
        "session_state": {"question_count": 2, "violation_count": 0},
        "hint_levels": [1, 2],
    })
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    test_classify_basic()
    test_classify_fallback_on_model_error()
    test_classify_missing_question()
    test_summarize_no_history()
    test_summarize_with_history()
    test_summarize_fallback_on_model_error()
    test_violation_in_summary()
    test_invalid_action()
    test_gateway_request_shape()
    print("\nAll offline tests passed.")

    if "--live" in sys.argv:
        _live_test()
