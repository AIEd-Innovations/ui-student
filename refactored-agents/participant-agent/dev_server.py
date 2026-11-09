"""Local development server for the Participant agent.

Serves the test HTML form at GET / and routes POST /api to the Vercel handler.

Usage:
    AI_GATEWAY_API_KEY=<key> python3 dev_server.py
    # then open http://localhost:3000
"""

import json
import os
import sys
from http.server import HTTPServer

sys.path.insert(0, os.path.dirname(__file__))

from api.index import handler as _VercelHandler
from llm import GatewayLLM


def _check_key() -> None:
    key = os.getenv("AI_GATEWAY_API_KEY") or os.getenv("VERCEL_OIDC_TOKEN")
    if not key:
        print("ERROR: set AI_GATEWAY_API_KEY before starting the dev server")
        sys.exit(1)
    try:
        GatewayLLM(api_key=key)
        print(f"AI Gateway key found (len={len(key)})")
    except ValueError as exc:
        print(f"ERROR: {exc}")
        sys.exit(1)


class DevHandler(_VercelHandler):
    def do_GET(self) -> None:
        if self.path not in ("/", "/index.html"):
            self.send_error(404)
            return
        html_path = os.path.join(os.path.dirname(__file__), "index.html")
        if not os.path.exists(html_path):
            self.send_error(404, "index.html not found")
            return
        with open(html_path, "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        if self.path == "/api":
            super().do_POST()
        else:
            self.send_error(404)


if __name__ == "__main__":
    _check_key()
    port = int(os.getenv("PORT", "3000"))
    server = HTTPServer(("localhost", port), DevHandler)
    print(f"Participant dev server running at http://localhost:{port}")
    print("POST /api with {{\"action\": \"classify\", \"question\": \"...\"}} to test")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
