from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from ..auth import COOKIE, MIN_PW, AuthError, auth
from ..services import license_service

router = APIRouter(prefix="/api")
# Gọi được khi CHƯA đăng nhập (middleware trong main.py dùng danh sách này).
PUBLIC_PATHS = {"/api/health", "/api/auth/status", "/api/auth/login", "/api/auth/setup", "/api/auth/register",
                "/api/auth/logout", "/api/license/check", "/api/license/activate", "/api/license/server"}
# Gọi được khi đã đăng nhập nhưng giấy phép hết hạn / bị thu hồi (để xem tình trạng / gia hạn)
LICENSE_FREE = PUBLIC_PATHS | {"/api/license/status"}


class LoginIn(BaseModel):
    username: str = ""
    password: str = ""
    security_key: str = ""          # bản cũ gửi kèm (bỏ qua)


class RegisterIn(BaseModel):
    username: str
    password: str
    name: str = ""
    phone: str = ""


class ChangeIn(BaseModel):
    old_password: str = ""
    new_password: str = ""


class CodeIn(BaseModel):
    code: str


class ServerIn(BaseModel):
    url: str


def _with_cookie(body: dict, token: str) -> JSONResponse:
    r = JSONResponse(body)
    # Cookie phiên (không có hạn): đóng app là mất. SameSite=Strict chặn trang web lạ gọi API cục bộ.
    r.set_cookie(COOKIE, token, httponly=True, samesite="strict", path="/")
    return r


def _err(e: AuthError) -> JSONResponse:
    return JSONResponse({"detail": str(e), **e.extra}, status_code=e.status)


@router.get("/auth/status")
def status(request: Request):
    lic = license_service.state()
    tok = request.cookies.get(COOKIE)
    return {"mode": "account" if auth.account_mode() else "local", "configured": auth.configured(),
            "authenticated": auth.check(tok), "user": auth.session_user(tok) or "",
            "machine_key": lic["machine_key"], "min_password": MIN_PW, "license": lic}


@router.post("/auth/register")
def register(body: RegisterIn):
    if not auth.account_mode():
        return JSONResponse({"detail": "Bản này không dùng tài khoản"}, status_code=400)
    try:
        res = auth.register(body.username, body.password, body.name, body.phone)
    except AuthError as e:
        return _err(e)
    sess = res.pop("session", None)
    return _with_cookie({"ok": True, "license": res}, sess) if sess else JSONResponse({"ok": True, "license": res})


@router.post("/auth/setup")
def setup(body: LoginIn):
    try:
        return _with_cookie({"ok": True}, auth.setup(body.password))
    except AuthError as e:
        return _err(e)


@router.post("/auth/login")
def login(body: LoginIn):
    try:
        tok = auth.login_account(body.username, body.password) if auth.account_mode() else auth.login_local(body.password)
        return _with_cookie({"ok": True}, tok)
    except AuthError as e:
        return _err(e)


@router.post("/auth/logout")
def logout(request: Request):
    auth.logout(request.cookies.get(COOKIE))
    r = JSONResponse({"ok": True})
    r.delete_cookie(COOKIE, path="/")
    return r


@router.post("/auth/change")
def change(body: ChangeIn, request: Request):
    try:
        auth.change(request.cookies.get(COOKIE), body.old_password, body.new_password)
        return {"ok": True}
    except AuthError as e:
        return _err(e)


# ------------------------------------------------------------------ giấy phép
@router.get("/license/status")
def license_status():
    return license_service.state()


@router.post("/license/check")
def license_check():
    return license_service.sync()


@router.post("/license/activate")
def license_activate(body: CodeIn):
    try:
        return license_service.activate(body.code)
    except ValueError as e:
        return JSONResponse({"detail": str(e)}, status_code=400)
    except license_service.ServerError as e:
        return JSONResponse({"detail": str(e)}, status_code=e.status or 503)


@router.post("/license/server")
def license_server(body: ServerIn):
    url = body.url.strip()
    if url and not url.startswith(("http://", "https://")):
        return JSONResponse({"detail": "Địa chỉ phải bắt đầu bằng http:// hoặc https://"}, status_code=400)
    license_service.set_server(url)
    return license_service.state()
