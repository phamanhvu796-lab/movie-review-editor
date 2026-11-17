"""Giấy phép theo TÀI KHOẢN + MÁY, do tool Admin cấp.

- Người dùng ĐĂNG KÝ tài khoản (tên đăng nhập + mật khẩu) trong app -> yêu cầu gửi lên máy chủ Admin.
- Admin duyệt, đặt hạn dùng và gửi MÃ KÍCH HOẠT (XXXX-XXXX) -> người dùng nhập vào app -> máy này được kích hoạt.
- Máy đã kích hoạt: đăng nhập không cần mạng. Đăng nhập tài khoản trên MÁY KHÁC -> tự gửi yêu cầu kích hoạt máy mới.
- Mã máy: tính từ phần cứng (UUID mainboard, serial bo mạch, mã CPU) -> cài lại Windows trên cùng máy vẫn cùng mã:
  đăng nhập lại là dùng tiếp, không cần mã kích hoạt mới.
- Giấy phép là chuỗi có CHỮ KÝ SỐ Ed25519 của Admin: app kiểm tra được khi không có mạng, không sửa được hạn dùng.
  App chỉ chứa khoá công khai (build_info.LICENSE_PUBKEY); khoá bí mật chỉ nằm ở máy Admin.
- Đồng bộ với máy chủ lúc mở app và mỗi 30 phút (gia hạn / khoá máy / cho đặt lại mật khẩu có hiệu lực ngay).
- Không có mạng tới máy chủ: Admin gửi "mã kích hoạt" (chính là chuỗi giấy phép) để dán vào app.
- Chống chỉnh lùi đồng hồ máy để kéo dài hạn dùng.
- Bản build KHÔNG có LICENSE_PUBKEY: không yêu cầu giấy phép (dùng tự do như trước).
"""
import base64
import hashlib
import json
import logging
import os
import platform
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request

from .. import build_info
from ..config import settings
from ..utils.paths import data_dir
from ..version import __version__

log = logging.getLogger("mre.license")
SYNC_EVERY = 30 * 60
CLOCK_SLACK = 2 * 3600          # cho phép lệch đồng hồ 2 giờ
_lock = threading.Lock()
_mk_cache: dict = {}
_status: dict = {"server_ok": None, "server_msg": "", "last_sync": 0.0}
PUBKEY_OVERRIDE: str | None = None     # chỉ dùng cho --selftest (khoá thử tạo trong tiến trình)

JUNK = {"", "none", "null", "to be filled by o.e.m.", "default string", "system serial number", "not specified",
        "not applicable", "0", "00000000", "0123456789", "base board serial number", "invalid",
        "ffffffff-ffff-ffff-ffff-ffffffffffff", "00000000-0000-0000-0000-000000000000",
        "03000200-0400-0500-0006-000700080009"}


# ------------------------------------------------------------------ mã máy
def _ps(cmd: str) -> str:
    try:
        p = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", cmd], capture_output=True,
                           text=True, encoding="utf-8", errors="replace", timeout=20, creationflags=0x08000000)
        return p.stdout.strip()
    except Exception:
        return ""


def _hardware_ids() -> list[str]:
    ids: list[str] = []
    if os.name == "nt":
        out = _ps("$u=(Get-CimInstance Win32_ComputerSystemProduct).UUID;"
                  "$b=(Get-CimInstance Win32_BaseBoard).SerialNumber;"
                  "$c=(Get-CimInstance Win32_Processor | Select-Object -First 1).ProcessorId;"
                  "Write-Output \"$u|$b|$c\"")
        ids = [x.strip() for x in out.split("|")]
        if not any(x.lower() not in JUNK for x in ids):
            try:
                import winreg
                with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography",
                                    0, winreg.KEY_READ | 0x0100) as k:              # KEY_WOW64_64KEY
                    ids = [winreg.QueryValueEx(k, "MachineGuid")[0]]
            except OSError:
                pass
    else:
        for f in ("/etc/machine-id", "/var/lib/dbus/machine-id"):
            try:
                ids = [open(f).read().strip()]
                break
            except OSError:
                continue
    ids = [x for x in ids if x and x.lower() not in JUNK]
    if not ids:
        import uuid
        ids = [f"{uuid.getnode():012x}"]
    return ids


def format_key(raw: bytes) -> str:
    s = base64.b32encode(raw).decode().rstrip("=")[:16]
    return "MRE-" + "-".join(s[i:i + 4] for i in range(0, 16, 4))


def machine_key() -> str:
    """MRE-XXXX-XXXX-XXXX-XXXX, cố định theo phần cứng máy."""
    with _lock:
        if "mk" not in _mk_cache:
            hw = "|".join(_hardware_ids()).upper()
            _mk_cache["mk"] = format_key(hashlib.sha256(("MRE1|" + hw).encode()).digest())
        return _mk_cache["mk"]


def normalize_key(k: str | None) -> str:
    return (k or "").strip().upper()


# ------------------------------------------------------------------ chữ ký
def _b64d(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def pubkey() -> str:
    return (PUBKEY_OVERRIDE if PUBKEY_OVERRIDE is not None else build_info.__dict__.get("LICENSE_PUBKEY", "")).strip()


def required() -> bool:
    return bool(pubkey())


def server_url() -> str:
    return (settings._load().get("license_server") or build_info.__dict__.get("LICENSE_SERVER", "") or "").strip().rstrip("/")  # noqa: SLF001


def verify_token(token: str) -> dict | None:
    """Trả payload nếu chữ ký đúng và đúng máy này; ngược lại None."""
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        body, sig = token.strip().split(".")
        Ed25519PublicKey.from_public_bytes(_b64d(pubkey())).verify(_b64d(sig), body.encode())
        p = json.loads(_b64d(body))
        return p if normalize_key(p.get("mk")) == machine_key() else None
    except Exception:
        return None


# ------------------------------------------------------------------ lưu trữ
def _file():
    return data_dir() / "license.json"


def _load() -> dict:
    try:
        return json.loads(_file().read_text("utf-8"))
    except Exception:
        return {}


def _save(d: dict):
    _file().write_text(json.dumps(d), "utf-8")


def _update(**kw):
    with _lock:
        d = _load()
        d.update(kw)
        _save(d)


def norm_user(u: str | None) -> str:
    return (u or "").strip().lower()


def prehash(username: str, password: str) -> str:
    """Mật khẩu được băm trước khi gửi đi (máy chủ không bao giờ thấy mật khẩu thật). Giống hệt bên tool Admin."""
    return hashlib.pbkdf2_hmac("sha256", (password or "").encode("utf-8"), ("MRE-ACC|" + norm_user(username)).encode(),
                               100_000).hex()


def store_token(token: str) -> dict:
    """Lưu giấy phép nếu hợp lệ và không cũ hơn giấy phép đang có (chống dán lại bản cũ)."""
    p = verify_token(token)
    if p is None:
        raise ValueError("Mã kích hoạt không hợp lệ hoặc không phải của máy này")
    with _lock:
        d = _load()
        cur = verify_token(d.get("token", "")) if d.get("token") else None
        if cur and norm_user(cur.get("u")) == norm_user(p.get("u")) and int(cur.get("iat", 0)) > int(p.get("iat", 0)):
            return cur
        d["token"] = token.strip()
        d["seen"] = max(float(d.get("seen", 0)), float(p.get("iat", 0)), time.time())
        if p.get("u"):
            d["user"] = norm_user(p["u"])
            if p.get("st") == "active":
                d.pop("pending_user", None)
        _save(d)
    _valid_cache["t"] = 0
    return p


def _now_checked(d: dict, p: dict | None) -> tuple[float, bool]:
    now = time.time()
    floor = max(float(d.get("seen", 0)), float((p or {}).get("iat", 0)))
    return now, now + CLOCK_SLACK < floor


MESSAGES = {
    "unregistered": "Chưa có tài khoản trên máy này. Đăng ký tài khoản mới, hoặc đăng nhập nếu đã có.",
    "pending": "Đã gửi yêu cầu kích hoạt cho Admin. Khi Admin gửi mã kích hoạt, hãy nhập vào ô bên dưới.",
    "blocked": "Tài khoản đã bị Admin khoá. Liên hệ Admin.",
    "revoked": "Tài khoản đã được đăng nhập và kích hoạt trên máy khác. Đăng nhập lại để gửi yêu cầu kích hoạt máy này.",
    "expired": "Tài khoản đã hết hạn sử dụng. Liên hệ Admin để gia hạn.",
    "clock": "Đồng hồ máy tính đang bị chỉnh lùi. Hãy đặt lại ngày giờ đúng.",
}


def state() -> dict:
    d = _load()
    base = {"required": required(), "machine_key": machine_key(), "server": server_url(), "server_ok": _status["server_ok"],
            "server_msg": _status["server_msg"], "user": d.get("user", ""), "pending_user": d.get("pending_user", "")}
    if not base["required"]:
        return {**base, "valid": True, "status": "free", "message": ""}
    p = verify_token(d.get("token", "")) if d.get("token") else None
    if p is None:
        st = "pending" if d.get("pending_user") else "unregistered"
        return {**base, "valid": False, "status": st, "message": MESSAGES[st]}
    now, rolled = _now_checked(d, p)
    st, exp = p.get("st"), int(p.get("exp") or 0)
    out = {**base, "user": norm_user(p.get("u")) or base["user"], "status": st, "expires_at": exp, "issued_at": p.get("iat"),
           "days_left": None if not exp else max(0, -int(-(exp - now) // 86400)), "name": p.get("nm", "")}
    if rolled:
        st = "clock"
    elif st == "active" and exp and now >= exp:
        st = "expired"
    if st != "active":
        st = st if st in MESSAGES else "pending"
        return {**out, "valid": False, "status": st, "message": MESSAGES[st]}
    return {**out, "valid": True, "message": ""}


_valid_cache = {"t": 0.0, "v": False}


def is_valid() -> bool:
    if not required():
        return True
    if time.time() - _valid_cache["t"] > 5:
        _valid_cache.update(t=time.time(), v=state()["valid"])
    return _valid_cache["v"]


def touch_clock():
    if not required():
        return
    with _lock:
        d = _load()
        if time.time() > float(d.get("seen", 0)):
            d["seen"] = time.time()
            _save(d)


# ------------------------------------------------------------------ máy chủ Admin
class ServerError(RuntimeError):
    def __init__(self, msg: str, status: int = 0):
        super().__init__(msg)
        self.status = status          # 0 = không kết nối được


def _post(path: str, body: dict, timeout: float = 15) -> dict:
    url = server_url()
    if not url:
        raise ServerError("Bản này chưa có địa chỉ máy chủ Admin — nhập ở mục “Địa chỉ máy chủ Admin”.")
    req = urllib.request.Request(url + path, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json", "User-Agent": f"MovieReviewEditor/{__version__}",
                                          "ngrok-skip-browser-warning": "1"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            out = json.loads(r.read().decode("utf-8", "replace"))
        _status.update(server_ok=True, server_msg="")
        return out
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(e.read().decode("utf-8", "replace")).get("detail")
        except Exception:
            msg = None
        _status.update(server_ok=True, server_msg="")
        if isinstance(msg, list):                 # lỗi kiểm tra dữ liệu của FastAPI
            msg = "Dữ liệu gửi lên không hợp lệ"
        raise ServerError(msg or f"Máy chủ Admin trả lỗi {e.code}", e.code) from e
    except (urllib.error.URLError, OSError, ValueError) as e:
        _status.update(server_ok=False, server_msg="Không kết nối được máy chủ Admin (máy Admin đang tắt hoặc sai địa chỉ).")
        raise ServerError(_status["server_msg"]) from e


def _device_info() -> dict:
    try:
        from . import sysinfo
        hw = sysinfo.scan()
        summary = f"{hw.get('cpu', '')} · RAM {hw.get('ram_gb', '?')} GB · " + ", ".join(g["name"] for g in hw.get("gpus", []))
    except Exception:
        summary = ""
    return {"mk": machine_key(), "host": socket.gethostname()[:80], "os": f"{platform.system()} {platform.release()}"[:80],
            "ver": __version__, "hw": summary[:300]}


def _accept(resp: dict) -> dict:
    tok = resp.get("token")
    p = store_token(tok) if tok else {}
    srv = float(resp.get("server_time") or 0)
    if srv:
        with _lock:
            d = _load()
            d["seen"] = max(float(d.get("seen", 0)), srv)
            _save(d)
    return p


def register(username: str, password: str, name: str = "", phone: str = "") -> dict:
    u = norm_user(username)
    resp = _post("/api/v1/register", {**_device_info(), "username": u, "pre": prehash(u, password), "name": name.strip()[:80],
                                      "phone": phone.strip()[:30]})
    _update(user=u, pending_user=u)
    _accept(resp)
    return {**state(), "server_message": resp.get("message", "")}


def login_online(username: str, password: str) -> dict:
    """Đăng nhập qua máy chủ. Máy chưa kích hoạt cho tài khoản này -> máy chủ tạo yêu cầu kích hoạt (chờ Admin)."""
    u = norm_user(username)
    resp = _post("/api/v1/login", {**_device_info(), "username": u, "pre": prehash(u, password)})
    _update(user=u)
    if resp.get("state") != "active":
        _update(pending_user=u)
    _accept(resp)
    return {**state(), "server_state": resp.get("state"), "server_message": resp.get("message", "")}


def activate(code: str) -> dict:
    """Mã ngắn XXXX-XXXX (Admin gửi) -> kích hoạt qua máy chủ. Chuỗi dài có dấu chấm -> mã offline đã ký."""
    code = (code or "").strip()
    if "." in code and len(code) > 40:
        store_token(code)
        return state()
    d = _load()
    u = d.get("pending_user") or d.get("user")
    if not u:
        raise ServerError("Hãy đăng ký hoặc đăng nhập trước, rồi mới nhập mã kích hoạt.", 400)
    resp = _post("/api/v1/activate", {**_device_info(), "username": u, "code": code})
    _accept(resp)
    return state()


def change_password(username: str, old: str, new: str) -> None:
    u = norm_user(username)
    _post("/api/v1/password", {"username": u, "old_pre": prehash(u, old), "new_pre": prehash(u, new)})


def sync() -> dict:
    if not required():
        return state()
    d = _load()
    u = d.get("user") or d.get("pending_user")
    try:
        if u:
            _accept(_post("/api/v1/check", {**_device_info(), "username": u}))
    except ServerError:
        pass
    finally:
        _status["last_sync"] = time.time()
        touch_clock()
    return state()


def set_server(url: str):
    settings.update(license_server=url.strip().rstrip("/"))


def start_background():
    if not required():
        return

    def loop():
        machine_key()
        while True:
            try:
                sync()
            except Exception:
                log.exception("Đồng bộ giấy phép lỗi")
            time.sleep(SYNC_EVERY)

    threading.Thread(target=loop, name="license-sync", daemon=True).start()
