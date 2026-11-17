from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from ..auth import COOKIE, MIN_PW, AuthError, auth
from ..services import license_service

router = APIRouter(prefix="/api")
# Gọi được khi CHƯA đăng nhập (middleware trong main.py dùng danh sách này).
PUBLIC_PATHS = {"/api/health", "/api/auth/status", "/api/auth/login", "/api/auth/setup", "/api/auth/logout",
                "/api/license/register", "/api/license/check", "/api/license/activate", "/api/license/server"}
# Gọi được khi đã đăng nhập nhưng giấy phép hết hạn (để xem tình trạng / gia hạn)
LICENSE_FREE = PUBLIC_PATHS | {"/api/license/status"}


class Creds(BaseModel):
    security_key: str = ""          # = mã máy (tự điền)
    password: str = ""


class ChangeIn(BaseModel):
    old_password: str = ""
    new_password: str = ""


class RegisterIn(BaseModel):
    name: str = ""


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
    return JSONResponse({"detail": str(e)}, status_code=e.status)


def _need_license():
    if not license_service.is_valid():
        raise AuthError(license_service.state()["message"] or "Máy chưa được cấp phép", 402)


@router.get("/auth/status")
def status(request: Request):
    lic = license_service.state()
    return {"configured": auth.configured(), "reset_pending": auth.reset_pending(), "fixed": False,
            "authenticated": auth.check(request.cookies.get(COOKIE)), "machine_key": lic["machine_key"],
            "min_password": MIN_PW, "license": lic}


@router.post("/auth/setup")
def setup(body: Creds):
    try:
        _need_license()
        return _with_cookie({"ok": True}, auth.setup(body.password, body.security_key))
    except AuthError as e:
        return _err(e)


@router.post("/auth/login")
def login(body: Creds):
    try:
        _need_license()
        return _with_cookie({"ok": True}, auth.login(body.password, body.security_key))
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


@router.post("/license/register")
def license_register(body: RegisterIn):
    try:
        return license_service.register(body.name)
    except RuntimeError as e:
        return JSONResponse({"detail": str(e), **license_service.state()}, status_code=502)


@router.post("/license/check")
def license_check():
    return license_service.sync()


@router.post("/license/activate")
def license_activate(body: CodeIn):
    try:
        return license_service.activate(body.code)
    except ValueError as e:
        return JSONResponse({"detail": str(e)}, status_code=400)


@router.post("/license/server")
def license_server(body: ServerIn):
    url = body.url.strip()
    if url and not url.startswith(("http://", "https://")):
        return JSONResponse({"detail": "Địa chỉ phải bắt đầu bằng http:// hoặc https://"}, status_code=400)
    license_service.set_server(url)
    return license_service.state()
