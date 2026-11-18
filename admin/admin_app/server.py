"""Máy chủ của tool Admin.

/api/v1/*    : app Movie Review Editor gọi — đăng ký tài khoản, đăng nhập, kích hoạt máy bằng mã, kiểm tra giấy phép.
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
_user_fails: dict[str, list] = defaultdict(lambda: [0, 0.0])


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
            return JSONResponse({"detail": "Trang quản trị chỉ mở được trên máy Admin (thêm --allow-remote-admin để mở từ xa)"},
                                status_code=403)
        if path.startswith("/admin/api/") and path not in ("/admin/api/status", "/admin/api/login", "/admin/api/setup"):
            tok = request.cookies.get(COOKIE)
            if not tok or _sessions.get(tok, 0) < time.time():
                return JSONResponse({"detail": "Cần đăng nhập Admin", "login_required": True}, status_code=401)
    resp = await call_next(request)
    if not path.startswith("/api/"):
        resp.headers["Cache-Control"] = "no-cache, must-revalidate"      # luôn tải giao diện Admin mới nhất
    return resp


# ================================================================== API cho app khách
class DeviceInfo(BaseModel):
    mk: str = Field(min_length=10, max_length=40)
    host: str = Field("", max_length=80)
    os: str = Field("", max_length=80)
    ver: str = Field("", max_length=20)
    hw: str = Field("", max_length=300)


class RegisterIn(DeviceInfo):
    username: str
    pre: str = Field(min_length=64, max_length=64)
    name: str = Field("", max_length=80)
    phone: str = Field("", max_length=30)


class LoginIn(DeviceInfo):
    username: str
    pre: str = Field(min_length=64, max_length=64)


class ActivateIn(DeviceInfo):
    username: str
    code: str


class CheckIn(DeviceInfo):
    username: str


class PasswordIn(BaseModel):
    username: str
    old_pre: str
    new_pre: str = Field(min_length=64, max_length=64)


def _mk(mk: str) -> str:
    mk = mk.strip().upper()
    if not mk.startswith("MRE-") or len(mk) != 23:
        raise HTTPException(400, "Mã máy không hợp lệ")
    return mk


def _device(a: dict, info: DeviceInfo, ip: str, create: bool) -> dict | None:
    mk, now = _mk(info.mk), time.time()
    d = core.get_device(a["username"], mk)
    if d is None:
        if not create:
            return None
        d = {"username": a["username"], "mk": mk, "first_seen": now, "status": "pending", "code": "", "code_exp": 0,
             "activated_at": 0, "seen_new": 1}
        core.add_log(a["username"], "device_request", f"{mk} {info.host} {ip}")
    d.update(host=info.host or d.get("host", ""), os=info.os or d.get("os", ""), ver=info.ver or d.get("ver", ""),
             hw=info.hw or d.get("hw", ""), ip=ip, last_seen=now)
    core.save_device(d)
    return d


def _reply(a: dict, d: dict, msg: str = "") -> dict:
    st = core.device_state(a, d)
    return {"state": st, "token": core.license_token(a, d), "server_time": time.time(), "username": a["username"],
            "message": msg or {
                "active": "Đã kích hoạt",
                "pending": "Đã gửi yêu cầu kích hoạt máy này cho Admin. Khi Admin gửi mã kích hoạt, hãy nhập vào app.",
                "blocked": "Tài khoản đã bị Admin khoá.",
                "revoked": "Tài khoản đã được chuyển sang máy khác. Đăng nhập lại để gửi yêu cầu kích hoạt máy này."}.get(st, "")}


def _check_user_lock(u: str):
    n, until = _user_fails[u]
    if until > time.time():
        raise HTTPException(429, f"Sai mật khẩu nhiều lần. Thử lại sau {int(until - time.time()) + 1} giây")


def _user_fail(u: str):
    rec = _user_fails[u]
    rec[0] += 1
    if rec[0] >= 5:
        rec[1] = time.time() + min(900, 30 * 2 ** (rec[0] - 5))


@app.post("/api/v1/register")
def register(body: RegisterIn, request: Request):
    _rate_limit(_ip(request), 10)
    u = core.norm_user(body.username)
    if not core.USER_RE.match(u):
        raise HTTPException(400, "Tên đăng nhập 3-32 ký tự, chỉ gồm chữ không dấu, số, dấu . _ -")
    if core.get_account(u):
        raise HTTPException(409, "Tên đăng nhập đã có người dùng. Chọn tên khác, hoặc bấm Đăng nhập nếu đây là tài khoản của bạn.")
    now = time.time()
    trial = int(core.kv_get("trial_days", 0) or 0)
    a = {"username": u, "name": body.name.strip(), "phone": body.phone.strip(), "note": "", "status": "active" if trial else "pending",
         "exp": int(now + trial * 86400) if trial else 0, "max_devices": int(core.kv_get("default_max_devices", 1) or 1),
         "created": now, "last_login": now}
    core.set_account_password(a, body.pre)
    core.save_account(a)
    core.add_log(u, "register", f"{body.name} {body.phone} {_ip(request)}")
    d = _device(a, body, _ip(request), create=True)
    if trial:                                              # dùng thử: máy đầu tiên được kích hoạt ngay
        core.activate_device(a, d)
    return _reply(a, d)


@app.post("/api/v1/login")
def login(body: LoginIn, request: Request):
    _rate_limit(_ip(request), 20)
    u = core.norm_user(body.username)
    _check_user_lock(u)
    a = core.get_account(u)
    if a is None or not core.check_account_password(a, body.pre):
        _user_fail(u)
        raise HTTPException(401, "Sai tên đăng nhập hoặc mật khẩu")
    _user_fails.pop(u, None)
    if a["status"] == "blocked":
        raise HTTPException(403, "Tài khoản đã bị Admin khoá. Liên hệ Admin.")
    a["last_login"] = time.time()
    core.save_account(a)
    d = _device(a, body, _ip(request), create=True)       # máy lạ -> tạo yêu cầu kích hoạt (chờ Admin)
    if d["status"] == "revoked":                           # máy từng bị thu hồi, nay đăng nhập lại -> yêu cầu lại
        d.update(status="pending", seen_new=1)
        core.save_device(d)
    return _reply(a, d)


@app.post("/api/v1/activate")
def activate(body: ActivateIn, request: Request):
    _rate_limit(_ip(request), 10)
    u = core.norm_user(body.username)
    a = core.get_account(u)
    d = core.get_device(u, _mk(body.mk)) if a else None
    code = core.norm_code(body.code)
    if not a or not d or not d.get("code") or not secrets.compare_digest(d["code"], code):
        raise HTTPException(400, "Mã kích hoạt không đúng, hoặc không phải mã của tài khoản + máy này")
    if (d.get("code_exp") or 0) < time.time():
        raise HTTPException(400, "Mã kích hoạt đã hết hạn (7 ngày). Nhờ Admin tạo mã mới.")
    if a["status"] == "blocked":
        raise HTTPException(403, "Tài khoản đã bị Admin khoá.")
    revoked = core.activate_device(a, d)
    core.add_log(u, "activate", f"{d['mk']} revoked={revoked}")
    return _reply(core.get_account(u), core.get_device(u, d["mk"]))


@app.post("/api/v1/check")
def check(body: CheckIn, request: Request):
    _rate_limit(_ip(request), 60)
    a = core.get_account(body.username)
    d = _device(a, body, _ip(request), create=False) if a else None
    if not a or not d:
        raise HTTPException(404, "Tài khoản / máy không còn trong danh sách của Admin. Đăng nhập lại để gửi yêu cầu.")
    return _reply(a, d)


@app.post("/api/v1/password")
def change_password(body: PasswordIn, request: Request):
    _rate_limit(_ip(request), 10)
    u = core.norm_user(body.username)
    _check_user_lock(u)
    a = core.get_account(u)
    if a is None or not core.check_account_password(a, body.old_pre):
        _user_fail(u)
        raise HTTPException(401, "Mật khẩu hiện tại không đúng")
    core.set_account_password(a, body.new_pre)
    core.save_account(a)
    core.add_log(u, "password_changed")
    return {"ok": True}


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
            "default_max_devices": int(core.kv_get("default_max_devices", 1) or 1),
            "data_dir": str(core.data_dir()), "server_time": time.time()}


class SettingsIn(BaseModel):
    trial_days: int | None = Field(None, ge=0, le=3650)
    default_max_devices: int | None = Field(None, ge=1, le=50)
    public_url: str | None = None


@app.put("/admin/api/settings")
def admin_settings(body: SettingsIn):
    if body.trial_days is not None:
        core.kv_set("trial_days", body.trial_days)
    if body.default_max_devices is not None:
        core.kv_set("default_max_devices", body.default_max_devices)
    if body.public_url is not None:
        core.kv_set("public_url", body.public_url.strip().rstrip("/"))
    return admin_info()


def _view(a: dict) -> dict:
    now = time.time()
    exp = int(a.get("exp") or 0)
    eff = a["status"]
    if eff == "active" and exp and exp <= now:
        eff = "expired"
    devs = []
    for d in a.get("devices", []):
        devs.append({**d, "online": (now - (d.get("last_seen") or 0)) < 45 * 60,
                     "code_valid": bool(d.get("code")) and (d.get("code_exp") or 0) > now})
    return {**a, "devices": devs, "effective": eff, "days_left": None if not exp else round((exp - now) / 86400, 1),
            "pending_devices": sum(1 for d in devs if d["status"] == "pending"),
            "is_new": any(d.get("seen_new") for d in devs) or a["status"] == "pending"}


@app.get("/admin/api/accounts")
def accounts():
    items = [_view(a) for a in core.list_accounts()]
    items.sort(key=lambda a: (not a["pending_devices"], -(a.get("created") or 0)))
    return {"accounts": items, "requests": sum(a["pending_devices"] for a in items), "server_time": time.time()}


class AccActionIn(BaseModel):
    action: str        # extend | set_days | set_date | lifetime | block | unblock | password | max_devices | note | name
    days: float | None = None
    date: float | None = None
    text: str | None = None
    number: int | None = None


def _acc(username: str) -> dict:
    a = core.get_account(username)
    if a is None:
        raise HTTPException(404, "Không có tài khoản này")
    return a


@app.post("/admin/api/accounts/{username}")
def account_action(username: str, body: AccActionIn):
    a, now, act = _acc(username), time.time(), body.action
    if act == "set_days":
        if not body.days or body.days <= 0:
            raise HTTPException(400, "Nhập số ngày > 0")
        a.update(status="active", exp=int(now + body.days * 86400))
    elif act == "extend":
        if not body.days:
            raise HTTPException(400, "Nhập số ngày")
        cur = int(a.get("exp") or 0)
        if a["status"] == "active" and cur == 0:
            raise HTTPException(400, "Tài khoản đang dùng vĩnh viễn")
        a.update(status="active", exp=int(max(cur, now) + body.days * 86400))
    elif act == "set_date":
        if not body.date or body.date <= now:
            raise HTTPException(400, "Ngày hết hạn phải ở tương lai")
        a.update(status="active", exp=int(body.date))
    elif act == "lifetime":
        a.update(status="active", exp=0)
    elif act == "block":
        a["status"] = "blocked"
    elif act == "unblock":
        a["status"] = "active"
    elif act == "password":
        pw = (body.text or "").strip()
        if len(pw) < 6:
            raise HTTPException(400, "Mật khẩu mới cần ít nhất 6 ký tự")
        core.set_account_password(a, core.prehash(a["username"], pw))
    elif act == "max_devices":
        if not body.number or body.number < 1:
            raise HTTPException(400, "Số máy tối thiểu là 1")
        a["max_devices"] = int(body.number)
    elif act == "note":
        a["note"] = (body.text or "")[:300]
    elif act == "name":
        a["name"] = (body.text or "")[:80]
    else:
        raise HTTPException(400, "Thao tác không hợp lệ")
    core.save_account(a)
    core.add_log(a["username"], act, f"days={body.days} date={body.date} n={body.number}")
    return {"ok": True}


class DevActionIn(BaseModel):
    action: str        # code | approve | revoke | delete | seen
    days: float | None = None        # approve tài khoản mới: số ngày dùng


@app.post("/admin/api/accounts/{username}/devices/{mk}")
def device_action(username: str, mk: str, body: DevActionIn):
    a = _acc(username)
    d = core.get_device(a["username"], mk.upper())
    if d is None:
        raise HTTPException(404, "Không có máy này")
    now, act = time.time(), body.action
    out: dict = {"ok": True}
    if act in ("approve", "code"):
        # Duyệt: tài khoản mới (chưa có hạn) -> đặt hạn; rồi tạo MÃ KÍCH HOẠT cho máy để gửi người dùng
        if a["status"] == "pending":
            if not body.days or body.days <= 0:
                raise HTTPException(400, "Tài khoản mới: nhập số ngày được dùng")
            a.update(status="active", exp=int(now + body.days * 86400))
            core.save_account(a)
        d.update(code=core.new_code(), code_exp=now + core.CODE_TTL, seen_new=0)
        if d["status"] == "revoked":
            d["status"] = "pending"
        core.save_device(d)
        out.update(code=d["code"], code_exp=d["code_exp"])
    elif act == "revoke":
        d["status"] = "revoked"
        core.save_device(d)
    elif act == "delete":
        core.delete_device(a["username"], d["mk"])
    elif act == "seen":
        d["seen_new"] = 0
        core.save_device(d)
    else:
        raise HTTPException(400, "Thao tác không hợp lệ")
    core.add_log(a["username"], f"device_{act}", d["mk"])
    return out


@app.get("/admin/api/accounts/{username}/devices/{mk}/offline")
def device_offline_code(username: str, mk: str):
    """Mã kích hoạt OFFLINE (chuỗi dài, đã ký): khi máy người dùng không vào được máy chủ. Kích hoạt luôn máy này."""
    a = _acc(username)
    d = core.get_device(a["username"], mk.upper())
    if d is None:
        raise HTTPException(404, "Không có máy này")
    if a["status"] != "active":
        raise HTTPException(400, "Tài khoản chưa được duyệt / đang bị khoá")
    core.activate_device(a, d)
    return {"code": core.license_token(a, core.get_device(a["username"], d["mk"]))}


@app.delete("/admin/api/accounts/{username}")
def account_delete(username: str):
    core.delete_account(username)
    core.add_log(core.norm_user(username), "delete")
    return {"ok": True}


class AddAccIn(BaseModel):
    username: str
    password: str
    name: str = ""
    phone: str = ""
    days: float | None = None


@app.post("/admin/api/accounts")
def account_add(body: AddAccIn):
    """Admin tạo sẵn tài khoản (người dùng chỉ cần Đăng nhập, rồi nhập mã kích hoạt cho máy)."""
    u = core.norm_user(body.username)
    if not core.USER_RE.match(u):
        raise HTTPException(400, "Tên đăng nhập 3-32 ký tự, chỉ gồm chữ không dấu, số, dấu . _ -")
    if core.get_account(u):
        raise HTTPException(409, "Tên đăng nhập đã tồn tại")
    if len(body.password) < 6:
        raise HTTPException(400, "Mật khẩu cần ít nhất 6 ký tự")
    now = time.time()
    a = {"username": u, "name": body.name, "phone": body.phone, "note": "", "status": "active",
         "exp": int(now + body.days * 86400) if body.days else 0,
         "max_devices": int(core.kv_get("default_max_devices", 1) or 1), "created": now, "last_login": 0}
    core.set_account_password(a, core.prehash(u, body.password))
    core.save_account(a)
    core.add_log(u, "admin_create")
    return {"ok": True}


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
