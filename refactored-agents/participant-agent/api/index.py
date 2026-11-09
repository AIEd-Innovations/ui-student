"""POST /api for the standalone Participant preview."""

import json
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from http.server import BaseHTTPRequestHandler

from participant import ParticipantAgent
from llm import GatewayLLM


class handler(BaseHTTPRequestHandler):
    def do_OPTIONS(self) -> None:
        self.send_response(200)
        self._cors()
        self.end_headers()

    def do_POST(self) -> None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > 65536:
                raise ValueError("Request body must be between 1 and 65536 bytes")
            body = json.loads(self.rfile.read(length))
            if not isinstance(body, dict):
                raise ValueError("Request body must be a JSON object")
        except (ValueError, TypeError) as exc:
            self._json({"status": "error", "error": str(exc)}, 400)
            return

        try:
            llm = GatewayLLM.from_env()
        except ValueError as exc:
            self._json({"status": "error", "error": str(exc)}, 503)
            return

        result = ParticipantAgent(llm=llm).process_request(body)
        status_code = (
            200 if result["status"] == "completed"
            else 502 if result.get("error_type") == "model"
            else 400
        )
        self._json(result, status_code)

    def _cors(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def _json(self, data: dict, status: int) -> None:
        payload = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self._cors()
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, fmt, *args):
        pass
