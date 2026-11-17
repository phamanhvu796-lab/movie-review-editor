"""Tài khoản + mã kích hoạt: app khách <-> máy chủ Admin THẬT (admin/admin_app/server.py) qua HTTP.

Máy test không có FastAPI thì dùng bản giả tối thiểu (thư mục testshim) chỉ để gọi được các hàm của server."""
import importlib
import json
import os
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "admin"))
try:
    import fastapi  # noqa: F401
except ImportError:
    sys.path.insert(0, os.environ.get("MRE_FASTAPI_SHIM", "/home/claude/testshim"))
from fastapi import HTTPException, Request  # noqa: E402

from app import auth as A  # noqa: E402
from app.services import license_service as L  # noqa: E402

MK1, MK2 = "MRE-AAAA-BBBB-CCCC-DDD1", "MRE-AAAA-BBBB-CCCC-DDD2"
STATE = {"mk": MK1}


@pytest.fixture()
def world(monkeypatch, tmp_path):
    monkeypatch.setenv("MRE_ADMIN_DIR", str(tmp_path / "admin"))
    monkeypatch.setenv("MRE_DATA_DIR", str(tmp_path / "pc1"))
    from admin_app import core, server
    importlib.reload(core)
    server = importlib.reload(server)
    server._user_fails.clear()
    server._hits.clear()

    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            path = self.path.split("?")[0]
            f = server.app.routes.get(("POST", path))
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])) or b"{}")
            try:
                model = next(iter(f.__annotations__.values()))
                out, code = f(model(**body), Request()), 200
            except HTTPException as e:
                out, code = {"detail": e.detail}, e.status_code
            data = json.dumps(out).encode()
            self.send_response(code)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *a):
            pass
    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    STATE["mk"] = MK1
    monkeypatch.setattr(L, "machine_key", lambda: STATE["mk"])
    monkeypatch.setattr(L, "PUBKEY_OVERRIDE", core.public_key())
    monkeypatch.setattr(L, "_device_info", lambda: {"mk": STATE["mk"], "host": "PC", "os": "Windows 11", "ver": "1.5.0", "hw": ""})
    monkeypatch.setattr(A, "ITERATIONS", 1000)
    L._valid_cache["t"] = 0
    L.set_server(f"http://127.0.0.1:{srv.server_port}")
    yield core, server, srv
    srv.shutdown()


def admin(server, fn, *path_args, **body):
    f = getattr(server, fn)
    model = list(f.__annotations__.values())[len(path_args)] if body or len(f.__annotations__) > len(path_args) else None
    return f(*path_args, model(**body)) if model else f(*path_args)


def switch_pc(monkeypatch, tmp_path, mk, name):
    STATE["mk"] = mk
    monkeypatch.setenv("MRE_DATA_DIR", str(tmp_path / name))
    srv_url = L.server_url
    L._valid_cache["t"] = 0
    return srv_url


def test_full_account_flow(world, monkeypatch, tmp_path):
    core, server, srv = world
    url = f"http://127.0.0.1:{srv.server_port}"
    a = A.Auth()
    assert a.account_mode() and L.state()["status"] == "unregistered"

    # 1) Đăng ký -> chờ Admin
    st = a.register("PhuongNg", "matkhau1", "Phương", "0901")
    assert st["status"] == "pending" and not st["valid"]
    with pytest.raises(A.AuthError) as e:
        a.register("phuongng", "khac123")                  # trùng tên (không phân biệt hoa thường)
    assert e.value.status == 409
    acc = core.get_account("phuongng")
    assert acc["status"] == "pending" and acc["pw_hash"] and "matkhau1" not in json.dumps(acc)

    # 2) Admin duyệt 30 ngày -> có mã kích hoạt
    res = admin(server, "device_action", "phuongng", MK1, action="approve", days=30)
    code = res["code"]
    assert len(code) == 9 and code[4] == "-"
    with pytest.raises(L.ServerError):
        L.activate("ZZZZ-ZZZZ")                             # mã sai
    st = L.activate(code.lower().replace("-", " "))        # gõ thường / có dấu cách vẫn nhận
    assert st["valid"] and st["user"] == "phuongng" and 29 <= st["days_left"] <= 30
    with pytest.raises(L.ServerError):
        L.activate(code)                                   # mã chỉ dùng 1 lần

    # 3) Đăng nhập trên máy đã kích hoạt: KHÔNG cần mạng (lần đầu cần mạng để lưu bản băm)
    assert a.check(a.login_account("phuongng", "matkhau1"))
    L.set_server("http://127.0.0.1:1")                     # máy chủ tắt
    assert a.check(a.login_account("PhuongNg", "matkhau1"))
    with pytest.raises(A.AuthError):
        a.login_account("phuongng", "sai-mat-khau")
    L.set_server(url)

    # 4) Đổi mật khẩu (qua máy chủ)
    tok = a.login_account("phuongng", "matkhau1")
    a.change(tok, "matkhau1", "matkhau2")
    L.set_server("http://127.0.0.1:1")
    assert a.check(a.login_account("phuongng", "matkhau2"))   # bản băm trên máy đã cập nhật
    L.set_server(url)

    # 5) Đăng nhập trên MÁY KHÁC -> yêu cầu kích hoạt mới -> Admin tạo mã -> máy cũ bị thu hồi
    switch_pc(monkeypatch, tmp_path, MK2, "pc2")
    L.set_server(url)
    b = A.Auth()
    with pytest.raises(A.AuthError) as e:
        b.login_account("phuongng", "matkhau2")
    assert e.value.status == 403 and e.value.extra["need_activation"]
    assert L.state()["status"] == "pending"
    code2 = admin(server, "device_action", "phuongng", MK2, action="code")["code"]
    assert L.activate(code2)["valid"]
    assert b.check(b.login_account("phuongng", "matkhau2"))
    assert core.get_device("phuongng", MK1)["status"] == "revoked"

    # máy cũ đồng bộ -> bị thu hồi
    switch_pc(monkeypatch, tmp_path, MK1, "pc1")
    L.set_server(url)
    assert L.sync()["status"] == "revoked"

    # 6) Cài lại Windows trên máy 2 (mất dữ liệu, cùng mã máy) -> đăng nhập lại là dùng tiếp, KHÔNG cần mã
    switch_pc(monkeypatch, tmp_path, MK2, "pc2-reinstall")
    L.set_server(url)
    c = A.Auth()
    assert c.check(c.login_account("phuongng", "matkhau2"))

    # 7) Admin đặt lại mật khẩu (người dùng quên) -> đăng nhập bằng mật khẩu mới (máy hỏi máy chủ)
    admin(server, "account_action", "phuongng", action="password", text="moi12345")
    assert c.check(c.login_account("phuongng", "moi12345"))

    # 8) Gia hạn / hết hạn / khoá
    admin(server, "account_action", "phuongng", action="set_date", date=time.time() + 2)
    time.sleep(1.1)
    L.sync()
    assert L.state()["valid"]
    time.sleep(2)
    L._valid_cache["t"] = 0
    assert L.state()["status"] == "expired"
    admin(server, "account_action", "phuongng", action="lifetime")
    time.sleep(1.01)
    assert L.sync()["valid"] and L.state()["days_left"] is None
    admin(server, "account_action", "phuongng", action="block")
    time.sleep(1.01)
    assert L.sync()["status"] == "blocked"
    with pytest.raises(A.AuthError):
        c.login_account("phuongng", "moi12345")


def test_trial_and_admin_created_account(world, monkeypatch, tmp_path):
    core, server, srv = world
    admin(server, "admin_settings", trial_days=3)
    a = A.Auth()
    st = a.register("dungthu", "matkhau1")
    assert st["valid"] and st["session"]                    # dùng thử: vào luôn
    # Admin tạo sẵn tài khoản -> người dùng đăng nhập -> chờ mã cho máy
    switch_pc(monkeypatch, tmp_path, MK2, "pc2")
    L.set_server(f"http://127.0.0.1:{srv.server_port}")
    admin(server, "account_add", username="khach01", password="abc123", days=10)
    b = A.Auth()
    with pytest.raises(A.AuthError) as e:
        b.login_account("khach01", "abc123")
    assert e.value.extra["need_activation"]
    off = admin(server, "device_offline_code", "khach01", MK2)["code"]  # mã offline (chuỗi dài)
    assert L.activate(off)["valid"]


def test_tampered_or_foreign_token_rejected(world):
    core, server, srv = world
    a_rec = {"username": "x", "status": "active", "exp": int(time.time()) + 99, "name": ""}
    tok = core.license_token(a_rec, {"mk": MK2, "status": "active"})          # mã của máy khác
    with pytest.raises(ValueError):
        L.activate(tok)
    tok = core.license_token(a_rec, {"mk": MK1, "status": "active"})
    body, sig = tok.split(".")
    p = json.loads(core.b64d(body))
    p["exp"] += 10 ** 7
    with pytest.raises(ValueError):
        L.activate(core.b64e(json.dumps(p).encode()) + "." + sig)


def test_clock_rollback(world, monkeypatch):
    core, server, srv = world
    a_rec = {"username": "x", "status": "active", "exp": 0, "name": ""}
    L.activate(core.license_token(a_rec, {"mk": MK1, "status": "active"}))
    real = time.time
    monkeypatch.setattr(L.time, "time", lambda: real() - 3 * 86400)
    assert L.state()["status"] == "clock"


def test_local_mode_without_license(monkeypatch, tmp_path):
    monkeypatch.setenv("MRE_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(L, "PUBKEY_OVERRIDE", "")
    monkeypatch.setattr(A, "ITERATIONS", 1000)
    a = A.Auth()
    assert not a.account_mode() and not a.configured()
    tok = a.setup("matkhau1")
    a.change(tok, "matkhau1", "matkhau2")
    with pytest.raises(A.AuthError):
        a.login_local("matkhau1")
    assert a.check(a.login_local("matkhau2"))
