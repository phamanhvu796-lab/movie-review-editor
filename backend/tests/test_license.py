"""Giấy phép: tool Admin ký (mã thật của admin/admin_app/core.py) <-> app khách kiểm tra."""
import json
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "admin"))
from admin_app import core as admin  # noqa: E402

from app import auth as A  # noqa: E402
from app.services import license_service as L  # noqa: E402

MK = "MRE-ABCD-EFGH-IJKL-MNOP"


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("MRE_DATA_DIR", tempfile.mkdtemp())
    monkeypatch.setenv("MRE_ADMIN_DIR", tempfile.mkdtemp())
    monkeypatch.setattr(L, "machine_key", lambda: MK)
    monkeypatch.setattr(L, "PUBKEY_OVERRIDE", admin.public_key())
    monkeypatch.setattr(L, "_device_info", lambda: {"mk": MK, "host": "pc", "os": "Windows 11", "ver": "1.4.0", "hw": ""})
    L._valid_cache["t"] = 0
    L._status.update(server_ok=None, server_msg="")
    monkeypatch.setattr(A, "ITERATIONS", 1000)


def dev(**kw):
    d = {"mk": MK, "name": "An", "status": "active", "exp": int(time.time()) + 10 * 86400, "rp": 0}
    d.update(kw)
    return d


def test_machine_key_format_is_stable():
    k = L.format_key(b"\x01" * 32)
    assert k.startswith("MRE-") and len(k) == 23 and k == L.format_key(b"\x01" * 32)


def test_not_required_without_pubkey(monkeypatch):
    monkeypatch.setattr(L, "PUBKEY_OVERRIDE", "")
    assert L.state()["valid"] and L.state()["status"] == "free"


def test_activate_with_admin_code_and_expiry():
    st = L.state()
    assert not st["valid"] and st["status"] == "unregistered"
    st = L.activate(admin.license_token(dev()))
    assert st["valid"] and 9 <= st["days_left"] <= 10
    # mã của máy khác bị từ chối
    with pytest.raises(ValueError):
        L.activate(admin.license_token(dev(mk="MRE-ZZZZ-ZZZZ-ZZZZ-ZZZZ")))
    # sửa nội dung (kéo dài hạn) -> chữ ký sai
    tok = admin.license_token(dev())
    body, sig = tok.split(".")
    p = json.loads(admin.b64d(body))
    p["exp"] += 999999
    with pytest.raises(ValueError):
        L.activate(admin.b64e(json.dumps(p).encode()) + "." + sig)


def test_expired_blocked_and_lifetime():
    L.activate(admin.license_token(dev(exp=int(time.time()) - 5)))
    assert L.state()["status"] == "expired" and not L.state()["valid"]
    time.sleep(1.01)
    L.activate(admin.license_token(dev(status="blocked")))
    assert L.state()["status"] == "blocked"
    time.sleep(1.01)
    L.activate(admin.license_token(dev(exp=0)))
    assert L.state()["valid"] and L.state()["days_left"] is None


def test_older_token_cannot_replace_newer(monkeypatch):
    old = admin.license_token(dev())                       # bản "còn hạn" cũ
    time.sleep(1.01)
    L.activate(admin.license_token(dev(status="blocked")))  # Admin khoá máy
    L.activate(old)                                          # cố dán lại mã cũ
    assert L.state()["status"] == "blocked"


def test_clock_rollback_detected(monkeypatch):
    L.activate(admin.license_token(dev()))
    real = time.time
    monkeypatch.setattr(L.time, "time", lambda: real() - 3 * 86400)
    assert L.state()["status"] == "clock" and not L.state()["valid"]


def test_register_and_sync_over_http(monkeypatch):
    devices = {}

    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            d = devices.setdefault(body["mk"], {"mk": body["mk"], "status": "pending", "exp": 0, "rp": 0, "name": ""})
            d["name"] = body.get("name") or d["name"]
            out = json.dumps({"token": admin.license_token(d), "server_time": time.time()}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)

        def log_message(self, *a):
            pass
    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        L.set_server(f"http://127.0.0.1:{srv.server_port}")
        st = L.register("Nguyễn An")
        assert st["status"] == "pending" and not st["valid"] and devices[MK]["name"] == "Nguyễn An"
        time.sleep(1.01)
        devices[MK].update(status="active", exp=int(time.time()) + 30 * 86400)   # Admin duyệt 30 ngày
        st = L.sync()
        assert st["valid"] and st["days_left"] >= 29 and st["server_ok"]
    finally:
        srv.shutdown()
    L.set_server("http://127.0.0.1:1")                      # máy chủ tắt -> vẫn dùng được nhờ giấy phép đã lưu
    st = L.sync()
    assert st["valid"] and st["server_ok"] is False


def test_password_setup_change_and_admin_reset():
    L.activate(admin.license_token(dev()))
    a = A.Auth()
    assert not a.configured()
    tok = a.setup("matkhau1", MK)
    assert a.check(tok) and a.configured()
    with pytest.raises(A.AuthError):
        a.login("matkhau1", "MRE-ZZZZ-ZZZZ-ZZZZ-ZZZZ")    # key không phải mã máy này
    with pytest.raises(A.AuthError):
        a.change(tok, "sai-mat-khau", "matkhau2")
    a.change(tok, "matkhau1", "matkhau2")
    with pytest.raises(A.AuthError):
        a.login("matkhau1")
    assert a.check(a.login("matkhau2", MK.lower()))
    # Admin bấm "Đặt lại MK" -> app cho tạo mật khẩu mới
    time.sleep(1.01)
    L.activate(admin.license_token(dev(rp=1)))
    assert not a.configured() and a.reset_pending()
    a.setup("matkhau3")
    assert a.configured() and a.check(a.login("matkhau3"))


def test_legacy_v1_record_upgrades(monkeypatch):
    monkeypatch.setattr(L, "PUBKEY_OVERRIDE", "")
    salt = "00" * 16
    rec = {"v": 1, "iter": 1000, "key_salt": salt, "key_hash": "x", "pw_salt": salt,
           "pw_hash": A._hash("cu-123456", salt, 1000)}
    A.Auth.user_file().write_text(json.dumps(rec))
    a = A.Auth()
    assert a.configured() and a.check(a.login("cu-123456"))
    assert json.loads(A.Auth.user_file().read_text())["v"] == 2
