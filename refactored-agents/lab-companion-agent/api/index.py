import json
from http.server import BaseHTTPRequestHandler

from lab_companion import LabCompanion
from llm import GatewayLLM


class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path != "/api":
            self.send_error(404)
            return
        try:
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            question = body["question"]
        except (ValueError, KeyError, TypeError) as exc:
            result = {"status": "error", "error": str(exc)}
            status_code = 400
        else:
            try:
                llm = GatewayLLM.from_env()
            except ValueError as exc:
                result = {"status": "error", "error": str(exc)}
                status_code = 503
            else:
                result = LabCompanion(llm=llm).process_request({
                    "question": question,
                    "rag_context": body.get("context", ""),
                })
                status_code = 200 if result["status"] == "completed" else 502
        payload = json.dumps(result).encode()
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)
