"""Local checks for standalone lab companion.

Run ``python3 test.py`` without credentials, or ``python3 test.py --live`` with
an AI Gateway API key.
"""

from __future__ import annotations

import io
import json
import sys
import unittest

from lab_companion import LabCompanion
from llm import GatewayLLM


class FakeLLM:
    def __init__(self, response: str = "Try checking your circuit setup first.") -> None:
        self.response = response
        self.messages = None
        self.temperature = None

    def complete(self, messages: list[dict], *, temperature: float = 0.4) -> str:
        self.messages = messages
        self.temperature = temperature
        return self.response


class FailingLLM:
    def complete(self, messages: list[dict], *, temperature: float = 0.4) -> str:
        raise RuntimeError("API unavailable")


class LabCompanionTests(unittest.TestCase):
    def test_local_request_builds_prompt_and_returns_status(self) -> None: #this tests labcompanion builds the prompt correctly and returns the expected status and response when a request is processed
        llm = FakeLLM()
        agent = LabCompanion(llm=llm, history_keep_turns=2)
        self.assertEqual(agent.get_status(), "standby")

        result = agent.process_request({
            "question": "How do I start finding a Thevenin equivalent?",
            "conversation_history": [
                {"role": "user", "content": "older turn"},
                {"role": "assistant", "content": "older reply"},
                {"role": "user", "content": "recent turn"},
            ],
            "learning_summary": "Needs help with circuit setup.",
            "avg_hint_level": 2.0,
            "guidance_level": "MINIMAL",
            "rag_context": "Course note: identify the terminals first.",
            "verifier_feedback": "Give one hint only.",
        })

        self.assertEqual(result, {"status": "completed", "response": llm.response})
        self.assertEqual(agent.get_status(), "completed")
        self.assertEqual(llm.temperature, 0.4)
        self.assertEqual([m["content"] for m in llm.messages[1:]], [
            "older reply", "recent turn", "How do I start finding a Thevenin equivalent?"
        ])
        prompt = llm.messages[0]["content"]
        for expected in (
            "Guidance level for THIS turn: MINIMAL",
            "Needs help with circuit setup.",
            "Course note: identify the terminals first.",
            "Give one hint only.",
        ):
            self.assertIn(expected, prompt)

    def test_provider_failure_is_reported(self) -> None:# this checks that if the LLM fails, the agent reports an error status and message
        agent = LabCompanion(llm=FailingLLM())
        result = agent.process_request({"question": "Can you help?"})
        self.assertEqual(result, {"status": "error", "error": "API unavailable"})
        self.assertEqual(agent.get_status(), "error")

    def test_gateway_adapter_sends_messages_and_reads_hint(self) -> None:
        sent = {}

        def fake_open(request, *, timeout):
            sent["request"] = request
            sent["timeout"] = timeout
            return io.BytesIO(b'{"choices":[{"message":{"content":"  Hint text  "}}]}')

        llm = GatewayLLM(api_key="test-key", model="google/gemini-2.5-flash", opener=fake_open)
        self.assertEqual(llm.complete([{"role": "user", "content": "hello"}]), "Hint text")
        payload = json.loads(sent["request"].data)
        self.assertEqual(payload["model"], "google/gemini-2.5-flash")
        self.assertEqual(payload["messages"][0]["content"], "hello")
        self.assertEqual(sent["request"].get_header("Authorization"), "Bearer test-key")
        self.assertEqual(sent["timeout"], 30)


def run_live() -> None:
    llm = GatewayLLM.from_env()
    agent = LabCompanion(llm=llm)
    result = agent.process_request({
        "question": "What is the first thing I should check when setting up a circuit simulation?",
        "guidance_level": "MINIMAL",
        "rag_context": "Course note: identify the circuit terminals and check the setup before solving.",
    })
    print(result)
    if result["status"] != "completed":
        raise SystemExit(1)


if __name__ == "__main__":
    if sys.argv[1:] == ["--live"]:
        run_live()
    else:
        unittest.main()
