"""Máy chủ của tool Admin.

/api/v1/*    : app Movie Review Editor ở các máy khách gọi (đăng ký mã máy, kiểm tra / gia hạn giấy phép).
/admin/api/* : giao diện Admin (cần mật khẩu Admin; mặc định chỉ mở từ chính máy Admin).
"""
import logging
import os
import secrets
import socket
import time
from collections import defaultdict, deque
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import core

log = logging.getLogger("mre.admin")
APP_NAME = "MovieReviewEditorAdmin"
COOKIE = "mre_admin"
SESSION_TTL = 12 * 3600
STATIC = Path(__file__).parent / "static"
ALLOW_REMOTE_ADMIN = os.getenv("MRE_ADMIN_REMOTE") == "1"

app = FastAPI(title="Movie Review Editor Admin")
_sessions: dict[str, float] = {}
_hits: dict[str, deque] = defaultdict(deque)
_fails = {"n": 0, "until": 0.0}


def _ip(request: Request) -> str:
    return request.client.host if request.client else ""


def _rate_limit(ip: str, per_min: int = 30):
    q, now = _hits[ip], time.time()
    while q and q[0] < now - 60:
        q.popleft()
    if len(q) >= per_min:
        raise HTTPException(429, "Gửi quá nhiều yêu cầu, thử lại sau 1 phút")
    q.append(now)


@app.middleware("http")
async def guard(request: Request, call_next):
    path = request.url.path
    if path.startswith("/admin"):
        local = _ip(request) in ("127.0.0.1", "::1", "localhost")
        if not local and not ALLOW_REMOTE_ADMIN:
            return JSONResponse({"detail": "Trang quản trị chỉ mở được trên máy Admin (bật MRE_ADMIN_REMOTE=1 để mở từ xa)"},
                                status_code=403)
        if path.startswith("/admin/api/") and path not in ("/admin/api/status", "/admin/api/login", "/admin/api/setup"):
            tok = request.cookies.get(COOKIE)
            if not tok or _sessions.get(tok, 0) < time.time():
                return JSONResponse({"detail": "Cần đăng nhập Admin", "login_required": True}, status_code=401)
    return await call_next(request)


# ================================================================== API cho máy khách
class DeviceIn(BaseModel):
    mk: str = Field(min_length=10, max_length=40)
    name: str = Field("", max_length=80)
    host: str = Field("", max_length=80)
    os: str = Field("", max_length=80)
    ver: str = Field("", max_length=20)
    hw: str = Field("", max_length=300)


def _norm(mk: str) -> str:
    mk = mk.strip().upper()
    if not mk.startswith("MRE-") or len(mk) != 23:
        raise HTTPException(400, "Mã máy không hợp lệ")
    return mk


def _touch(body: DeviceIn, ip: str, create: bool) -> dict | None:
    mk, now = _norm(body.mk), time.time()
    d = core.get_device(mk)
    if d is None:
        if not create:
            return None
        trial = int(core.kv_get("trial_days", 0) or 0)
        d = {"mk": mk, "first_seen": now, "status": "active" if trial > 0 else "pending",
             "exp": int(now + trial * 86400) if trial > 0 else 0, "note": "", "rp": 0, "seen_new": 1}
        core.add_log(mk, "register", f"{body.host} {ip}")
    d.update(name=body.name or d.get("name", ""), host=body.host, os=body.os, ver=body.ver, hw=body.hw or d.get("hw", ""),
             ip=ip, last_seen=now)
    core.save_device(d)
    return d


@app.post("/api/v1/register")
def register(body: DeviceIn, request: Request):
    _rate_limit(_ip(request))
    d = _touch(body, _ip(request), create=True)
    return {"token": core.license_token(d), "server_time": time.time()}


@app.post("/api/v1/check")
def check(body: DeviceIn, request: Request):
    _rate_limit(_ip(request), 60)
    d = _touch(body, _ip(request), create=True)          # máy bị xoá khỏi danh sách -> đăng ký lại (chờ duyệt)
    return {"token": core.license_token(d), "server_time": time.time()}


@app.get("/api/health")
def health():
    return {"ok": True, "app": APP_NAME}


# ================================================================== API quản trị
class PwIn(BaseModel):
    password: str


@app.get("/admin/api/status")
def admin_status(request: Request):
    tok = request.cookies.get(COOKIE)
    return {"configured": core.admin_configured(), "authenticated": bool(tok and _sessions.get(tok, 0) > time.time())}


def _login_cookie() -> JSONResponse:
    tok = secrets.token_urlsafe(32)
    _sessions[tok] = time.time() + SESSION_TTL
    r = JSONResponse({"ok": True})
    r.set_cookie(COOKIE, tok, httponly=True, samesite="strict", path="/")
    return r


@app.post("/admin/api/setup")
def admin_setup(body: PwIn):
    if core.admin_configured():
        raise HTTPException(409, "Đã có mật khẩu Admin")
    try:
        core.set_admin_password(body.password)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return _login_cookie()


@app.post("/admin/api/login")
def admin_login(body: PwIn):
    if _fails["until"] > time.time():
        raise HTTPException(429, f"Sai quá nhiều lần, thử lại sau {int(_fails['until'] - time.time()) + 1} giây")
    if not core.check_admin_password(body.password):
        _fails["n"] += 1
        if _fails["n"] >= 5:
            _fails["until"] = time.time() + min(900, 30 * 2 ** (_fails["n"] - 5))
        raise HTTPException(401, "Sai mật khẩu Admin")
    _fails.update(n=0, until=0.0)
    return _login_cookie()


@app.post("/admin/api/logout")
def admin_logout(request: Request):
    _sessions.pop(request.cookies.get(COOKIE, ""), None)
    return {"ok": True}


def _lan_ips() -> list[str]:
    ips = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(info[4][0])
    except OSError:
        pass
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ips.add(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    return sorted(i for i in ips if not i.startswith("127."))


@app.get("/admin/api/info")
def admin_info():
    port = int(os.getenv("MRE_ADMIN_PORT", "8800"))
    return {"public_key": core.public_key(), "port": port, "lan_urls": [f"http://{ip}:{port}" for ip in _lan_ips()],
            "public_url": core.kv_get("public_url", ""), "trial_days": int(core.kv_get("trial_days", 0) or 0),
            "data_dir": str(core.data_dir()), "server_time": time.time()}


class SettingsIn(BaseModel):
    trial_days: int | None = Field(None, ge=0, le=3650)
    public_url: str | None = None


@app.put("/admin/api/settings")
def admin_settings(body: SettingsIn):
    if body.trial_days is not None:
        core.kv_set("trial_days", body.trial_days)
    if body.public_url is not None:
        core.kv_set("public_url", body.public_url.strip().rstrip("/"))
    return admin_info()


def _view(d: dict) -> dict:
    now = time.time()
    exp = int(d.get("exp") or 0)
    online = (now - (d.get("last_seen") or 0)) < 45 * 60
    eff = d["status"]
    if eff == "active" and exp and exp <= now:
        eff = "expired"
    return {**d, "effective": eff, "online": online, "days_left": None if not exp else round((exp - now) / 86400, 1)}


@app.get("/admin/api/devices")
def devices():
    items = [_view(d) for d in core.list_devices()]
    return {"devices": items, "pending": sum(1 for d in items if d["status"] == "pending"),
            "new": sum(1 for d in items if d.get("seen_new")), "server_time": time.time()}


class ActionIn(BaseModel):
    action: str        # activate | extend | set_days | set_date | lifetime | block | unblock | reset_pw | note | rename | seen
    days: float | None = None
    date: float | None = None          # unix (giây)
    text: str | None = None


@app.post("/admin/api/devices/{mk}")
def device_action(mk: str, body: ActionIn):
    d = core.get_device(mk.upper())
    if d is None:
        raise HTTPException(404, "Không có máy này")
    now, a = time.time(), body.action
    if a in ("activate", "set_days"):
        if not body.days or body.days <= 0:
            raise HTTPException(400, "Nhập số ngày > 0")
        d.update(status="active", exp=int(now + body.days * 86400))
    elif a == "extend":
        if not body.days:
            raise HTTPException(400, "Nhập số ngày")
        base = max(int(d.get("exp") or 0), int(now)) if int(d.get("exp") or 0) else int(now)
        d.update(status="active", exp=int(base + body.days * 86400))
    elif a == "set_date":
        if not body.date or body.date <= now:
            raise HTTPException(400, "Ngày hết hạn phải ở tương lai")
        d.update(status="active", exp=int(body.date))
    elif a == "lifetime":
        d.update(status="active", exp=0)
    elif a == "block":
        d["status"] = "blocked"
    elif a == "unblock":
        d["status"] = "active"
    elif a == "reset_pw":
        d["rp"] = int(d.get("rp") or 0) + 1
    elif a == "note":
        d["note"] = (body.text or "")[:300]
    elif a == "rename":
        d["name"] = (body.text or "")[:80]
    elif a == "seen":
        d["seen_new"] = 0
    else:
        raise HTTPException(400, "Thao tác không hợp lệ")
    if a not in ("note", "rename", "seen"):
        d["seen_new"] = 0
        core.add_log(d["mk"], a, f"days={body.days} date={body.date}")
    core.save_device(d)
    return _view(d)


class AddIn(BaseModel):
    mk: str
    name: str = ""
    days: float | None = None        # None/0 = vĩnh viễn


@app.post("/admin/api/devices")
def device_add(body: AddIn):
    mk = _norm(body.mk)
    now = time.time()
    d = core.get_device(mk) or {"mk": mk, "first_seen": now, "last_seen": 0, "note": "", "rp": 0, "host": "", "os": "",
                                 "ver": "", "hw": "", "ip": ""}
    d.update(name=body.name or d.get("name", ""), status="active", exp=int(now + body.days * 86400) if body.days else 0,
             seen_new=0)
    core.save_device(d)
    core.add_log(mk, "add", f"days={body.days}")
    return _view(d)


@app.delete("/admin/api/devices/{mk}")
def device_delete(mk: str):
    core.delete_device(mk.upper())
    core.add_log(mk.upper(), "delete")
    return {"ok": True}


@app.get("/admin/api/devices/{mk}/code")
def device_code(mk: str):
    """Mã kích hoạt offline: gửi cho người dùng dán vào app khi máy họ không kết nối được máy chủ."""
    d = core.get_device(mk.upper())
    if d is None:
        raise HTTPException(404, "Không có máy này")
    return {"code": core.license_token(d)}


@app.get("/admin/api/keys/export")
def keys_export():
    return JSONResponse(core.export_keys(), headers={"Content-Disposition": 'attachment; filename="mre_admin_signing_key.json"'})


class KeysIn(BaseModel):
    private: str


@app.post("/admin/api/keys/import")
def keys_import(body: KeysIn):
    try:
        return {"public_key": core.import_keys(body.model_dump())}
    except Exception:
        raise HTTPException(400, "File khoá không hợp lệ")


@app.get("/admin/api/log")
def admin_log(limit: int = 200):
    with core.db() as c:
        return [dict(r) for r in c.execute("SELECT * FROM log ORDER BY id DESC LIMIT ?", (min(limit, 1000),))]


# ================================================================== giao diện
@app.get("/")
def root():
    return RedirectResponse("/admin/")


@app.get("/admin/")
def admin_page():
    return FileResponse(STATIC / "index.html")


app.mount("/admin/static", StaticFiles(directory=STATIC), name="static")
