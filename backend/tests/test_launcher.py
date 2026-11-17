import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import launcher


class Health(BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps({"ok": True, "app": "MovieReviewEditor"}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def test_reuses_running_instance_instead_of_opening_second_port():
    srv = HTTPServer(("127.0.0.1", 0), Health)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        assert launcher.pick_port(srv.server_address[1]) == ("existing", srv.server_address[1])
    finally:
        srv.shutdown()


def test_picks_free_port_when_nothing_runs():
    kind, port = launcher.pick_port(8999)
    assert kind == "new" and port > 0
