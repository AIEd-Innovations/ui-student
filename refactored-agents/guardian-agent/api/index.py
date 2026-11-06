"""POST /api for the standalone Guardian preview."""

import json
from http.server import BaseHTTPRequestHandler

from guardian import Guardian
from llm import GatewayLLM


class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path != "/api":
            self.send_error(404)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > 65536:
                raise ValueError("Request body must be between 1 and 65536 bytes")
            body = json.loads(self.rfile.read(length))
            if not isinstance(body, dict):
                raise ValueError("Request body must be a JSON object")
        except (ValueError, TypeError) as exc:
            result, status_code = {"status": "error", "error": str(exc)}, 400
        else:
            try:
                llm = GatewayLLM.from_env()
            except ValueError as exc:
                result, status_code = {"status": "error", "error": str(exc)}, 503
            else:
                result = Guardian(llm=llm).process_request(body)
                status_code = (200 if result["status"] == "completed" else
                               502 if result.get("error_type") == "model" else 400)
        payload = json.dumps(result).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)
