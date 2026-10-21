"""Small local server for trying the agent in a browser."""

import os
from http.server import HTTPServer
from pathlib import Path

from api.index import handler
from llm import GatewayLLM


class LocalHandler(handler):
    def do_GET(self):
        if self.path != "/":
            self.send_error(404)
            return
        payload = Path(__file__).with_name("index.html").read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


if __name__ == "__main__":
    try:
        GatewayLLM.from_env()
    except ValueError as exc:
        raise SystemExit(f"{exc}. Set it in this terminal before running npm run dev.")
    port = int(os.getenv("PORT", "3000"))
    server = HTTPServer(("127.0.0.1", port), LocalHandler)
    print(f"Open http://localhost:{port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
