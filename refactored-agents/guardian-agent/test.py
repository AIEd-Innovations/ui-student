"""Run local policy checks, or --live for one Gateway classification."""

import io
import json
import sys
import unittest

from guardian import Guardian
from llm import GatewayLLM


class FakeLLM:
    def __init__(self, replies):
        self.replies = iter(replies)
        self.calls = []

    def complete(self, messages, *, temperature=0.0, max_tokens=400, json_mode=True,
                 response_schema=None):
        self.calls.append((messages, temperature, max_tokens, json_mode, response_schema))
        reply = next(self.replies)
        if isinstance(reply, Exception):
            raise reply
        return json.dumps(reply)


def classification(kind):
    return {"classification": kind, "confidence": 0.9,
            "reasoning": "Test decision", "concept_tags": ["circuits"]}


class GuardianTests(unittest.TestCase):
    def test_direct_solution_and_third_violation_escalate(self):
        llm = FakeLLM([classification("DIRECT_SOLUTION")])
        agent = Guardian(llm=llm)
        result = agent.process_request({"question": "Solve this assigned circuit",
            "session_state": {"question_count": 2, "violation_count": 2, "escalated": False}})
        self.assertEqual(result["classification"], "DIRECT_SOLUTION")
        self.assertEqual(result["guidance_level"], "REJECTED")
        self.assertEqual(result["violation_type"], "DIRECT_SOLUTION_REQUEST")
        self.assertEqual(result["session_state"],
                         {"question_count": 3, "violation_count": 3, "escalated": True})
        self.assertIn("Student's current question", llm.calls[0][0][1]["content"])
        self.assertEqual(llm.calls[0][1:4], (0.0, 400, True))
        self.assertEqual(llm.calls[0][4]["name"], "guardian_classification")

    def test_answer_farming_and_question_throttle(self):
        llm = FakeLLM([classification("ANSWER_FARMING"), classification("CONCEPTUAL")])
        agent = Guardian(llm=llm)
        first = agent.process_request({"question": "What next?"})
        self.assertEqual(first["guidance_level"], "MINIMAL")
        self.assertEqual(first["session_state"]["violation_count"], 1)
        late = agent.process_request({"question": "What is voltage?",
            "session_state": {"question_count": 15, "violation_count": 0, "escalated": False}})
        self.assertEqual(late["guidance_level"], "REJECTED")

    def test_verify_rejects_draft_and_returns_feedback(self):
        llm = FakeLLM([{"passes": False, "confidence": 0.95,
                        "reason": "Full answer", "feedback_for_companion": "Give one hint."}])
        result = Guardian(llm=llm).process_request({"action": "verify", "question": "Help?",
            "draft_response": "The answer is 42.", "guidance_level": "MINIMAL",
            "classification": "PROCEDURAL"})
        self.assertFalse(result["passes"])
        self.assertEqual(result["feedback_for_companion"], "Give one hint.")
        self.assertFalse(result["verifier_fallback"])

    def test_model_failures_do_not_approve_or_classify(self):
        llm = FakeLLM([RuntimeError("down"), RuntimeError("down")])
        agent = Guardian(llm=llm)
        validate = agent.process_request({"question": "What is voltage?"})
        verify = agent.process_request({"action": "verify", "question": "What is voltage?",
            "draft_response": "Potential difference.", "guidance_level": "FULL"})
        self.assertEqual(validate["status"], "error")
        self.assertEqual(validate["error_type"], "model")
        self.assertNotIn("classification", validate)
        self.assertEqual(verify["status"], "error")
        self.assertEqual(verify["error_type"], "model")
        self.assertNotIn("passes", verify)

    def test_rejects_invalid_request_state(self):
        agent = Guardian(llm=FakeLLM([]))
        result = agent.process_request({"question": "Hi", "session_state":
            {"question_count": 0, "violation_count": 2, "escalated": False}})
        self.assertEqual(result["status"], "error")

    def test_gateway_request_shape(self):
        sent = {}
        def fake_open(request, *, timeout):
            sent["request"] = request
            sent["timeout"] = timeout
            return io.BytesIO(b'{"choices":[{"message":{"content":" {\\"passes\\":true} "}}]}')
        llm = GatewayLLM(api_key="test-key", opener=fake_open)
        self.assertEqual(llm.complete([{"role": "user", "content": "hello"}]), '{"passes":true}')
        payload = json.loads(sent["request"].data)
        self.assertEqual(payload["temperature"], 0.0)
        self.assertNotIn("max_tokens", payload)
        self.assertEqual(sent["request"].get_header("Authorization"), "Bearer test-key")
        self.assertEqual(sent["timeout"], 30)

    def test_gateway_sends_structured_output_schema(self):
        sent = {}
        def fake_open(request, *, timeout):
            sent["body"] = json.loads(request.data)
            return io.BytesIO(b'{"choices":[{"message":{"content":"{}"}}]}')
        llm = GatewayLLM(api_key="test-key", opener=fake_open)
        llm.complete([{"role": "user", "content": "hello"}],
                     response_schema={"name": "test", "schema": {"type": "object"}})
        self.assertEqual(sent["body"]["response_format"], {
            "type": "json_schema", "json_schema": {"name": "test", "schema": {"type": "object"}}})


def live():
    result = Guardian(llm=GatewayLLM.from_env()).process_request({
        "question": "What is the difference between voltage and current?"})
    print(result)
    if result["status"] != "completed" or result.get("classifier_fallback"):
        raise SystemExit(1)


if __name__ == "__main__":
    if sys.argv[1:] == ["--live"]:
        live()
    else:
        unittest.main()
