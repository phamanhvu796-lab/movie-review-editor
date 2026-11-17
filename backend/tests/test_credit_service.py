import json
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from app.services import credit_service


def _serve(routes):
    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            path = self.path.split("?")[0]
            if path in routes and self.headers.get("Authorization") == "Bearer sk-test":
                body = json.dumps(routes[path]).encode()
                self.send_response(200)
            else:
                body, _ = b'{"error":"nf"}', self.send_response(404)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass
    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def test_relay_billing_remaining(monkeypatch):
    monkeypatch.setenv("MRE_DATA_DIR", tempfile.mkdtemp())
    srv = _serve({"/v1/dashboard/billing/subscription": {"hard_limit_usd": 20.0},
                  "/v1/dashboard/billing/usage": {"total_usage": 525.0}})          # cent
    try:
        conn = {"id": "r1", "name": "relay", "base_url": f"http://127.0.0.1:{srv.server_port}", "api_key": "sk-test"}
        c = credit_service.llm_credit(conn)
        assert c["balance"]["remaining_usd"] == 14.75 and c["balance"]["limit_usd"] == 20.0
    finally:
        srv.shutdown()


def test_newapi_token_usage_fallback(monkeypatch):
    monkeypatch.setenv("MRE_DATA_DIR", tempfile.mkdtemp())
    srv = _serve({"/api/usage/token": {"code": True, "data": {"total_available": 2_500_000, "total_granted": 5_000_000,
                                                                "total_used": 2_500_000}}})
    try:
        conn = {"id": "r2", "base_url": f"http://127.0.0.1:{srv.server_port}/v1", "api_key": "sk-test"}
        # /api/usage/token được gọi qua https://host -> ở test HTTP thì thử trực tiếp hàm với gốc http
        monkeypatch.setattr(credit_service, "_roots", lambda c: [f"http://127.0.0.1:{srv.server_port}/v1"])
        orig = credit_service._get
        monkeypatch.setattr(credit_service, "_get", lambda url, *a, **k: orig(url.replace("https://", "http://"), *a, **k))
        c = credit_service.llm_credit(conn)
        assert c["balance"]["remaining_usd"] == 5.0 and c["balance"]["approx"]
    finally:
        srv.shutdown()


def test_official_anthropic_uses_budget(monkeypatch):
    monkeypatch.setenv("MRE_DATA_DIR", tempfile.mkdtemp())
    from app.services import usage_service
    usage_service.record({"id": "a1"}, "claude-sonnet", {"input_tokens": 1_000_000, "output_tokens": 0})
    c = credit_service.llm_credit({"id": "a1", "base_url": "", "api_key": "sk-ant", "budget_usd": 10})
    assert c["balance"] is None and "Anthropic" in c["note"]
    assert c["budget"] == {"limit_usd": 10.0, "remaining_usd": 7.0}
