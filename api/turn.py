"""Vercel serverless function - POST /api/turn

Processes one student turn using the Lab Companion agent backed by the Vercel AI Gateway (google/gemini-2.5-flash by default).

Request JSON:
    {
        "question": "...",
        "conversation_history": [{"role": "user"|"assistant", "content": "..."}, ...],
        "guidance_level": "FULL" | "MODERATE" | "MINIMAL" | "REJECTED", 
        "learning_summary": "...",
        "avg_hint_level": 1.0,
        "rag_context": "..."
    }
        
Response JSON: 
    {"status": "completed", "response": "..."}
  | {"status": "error",      "error": "..."}
"""

from __future__ import annotations

import json
import os
import sys
from http.server import BaseHTTPRequestHandler

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from agentic_system.agents.lab_companion import LabCompanion
from agentic_system.config import SystemConfig
from agentic_system.llm.gateway import GatewayLLM

def _companion() -> LabCompanion:
    api_key = os.getenv("AI_GATEWAY_API_KEY") or os.getenv("VERCEL_OIDC_TOKEN", "")
    if not api_key:
        raise ValueError("AI_GATEWAY_API_KEY is not set")
    llm = GatewayLLM(
        api_key=api_key,
        model=os.getenv("AI_GATEWAY_MODEL", "google/gemini-2.5-flash"),
    )
    return LabCompanion(llm=llm, config=SystemConfig())

class handler(BaseHTTPRequestHandler):
    def do_OPTIONS(self) -> None:
        self.send_response(200)
        self._cors()
        self.end_headers()

    def do_POST(self) -> None: 
        try: 
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length)) if length else {}
            question = body["question"]
        except (ValueError, KeyError, TypeError) as exc:
            self._json({"status": "error", "error": str(exc)}, 400)
            return 
    
        try:
            companion = _companion()
        except ValueError as exc:
            self._json({"status": "error", "error": str(exc)}, 503)
            return
        
        try:
            response = companion.respond(
                question=question,
                conversation_history=body.get("conversation_history", []),
                learning_summary=body.get("learning_summary", "No prior context."),
                avg_hint_level=float(body.get("avg_hint_level", 1.0)),
                guidance_level=body.get("guidance_level", "FULL"),
                rag_context=body.get("rag_context", "No course resources supplied."),
            )
            self._json({"status": "completed", "response": response}, 200)
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

