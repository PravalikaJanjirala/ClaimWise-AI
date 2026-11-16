"""Tiny mock carrier REST API (standard library only).

    python mock_api/server.py            # serves http://127.0.0.1:8001
    export CLAIMWISE_POLICY_API_URL=http://127.0.0.1:8001

GET /policies/<policy_no>  ->  200 JSON record, or 404.
"""
import json
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from claimwise import config  # noqa: E402
from claimwise.policy_service import PolicyService  # noqa: E402

SERVICE = PolicyService(path=config.CARRIER_DATA)


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if not self.path.startswith("/policies/"):
            return self._send(404, {"error": "not found"})
        rec = SERVICE.get_policy(unquote(self.path[len("/policies/"):]))
        self._send(200 if rec else 404, rec or {"error": "policy not found"})

    def _send(self, code, body):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):  # keep the console quiet
        pass


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8001
    print(f"Mock carrier API on http://127.0.0.1:{port}")
    HTTPServer(("127.0.0.1", port), Handler).serve_forever()
