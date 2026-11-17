"""Giấy phép sử dụng theo MÃ MÁY, do tool Admin cấp.

- Mã máy: tính từ phần cứng (UUID mainboard, serial bo mạch, mã CPU) -> không đổi kể cả khi cài lại Windows.
- Lần đầu mở app: tự gửi mã máy về máy chủ của Admin (đăng ký). Admin đặt hạn dùng trong tool Admin.
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


def store_token(token: str) -> dict:
    """Lưu giấy phép mới nếu hợp lệ và không cũ hơn giấy phép đang có (chống dùng lại bản cũ)."""
    p = verify_token(token)
    if p is None:
        raise ValueError("Mã kích hoạt không hợp lệ hoặc không phải của máy này")
    with _lock:
        d = _load()
        cur = verify_token(d.get("token", "")) if d.get("token") else None
        if cur and int(cur.get("iat", 0)) > int(p.get("iat", 0)):
            return cur
        d["token"] = token.strip()
        d["seen"] = max(float(d.get("seen", 0)), float(p.get("iat", 0)), time.time())
        _save(d)
    return p


def _now_checked(d: dict, p: dict | None) -> tuple[float, bool]:
    """Thời gian hiện tại + cờ 'đồng hồ bị lùi' (so với lần cuối app thấy / thời điểm cấp giấy phép)."""
    now = time.time()
    floor = max(float(d.get("seen", 0)), float((p or {}).get("iat", 0)))
    return now, now + CLOCK_SLACK < floor


def state() -> dict:
    """Tình trạng giấy phép cho giao diện và middleware."""
    base = {"required": required(), "machine_key": machine_key(), "server": server_url(),
            "server_ok": _status["server_ok"], "server_msg": _status["server_msg"], "name": _load().get("name", "")}
    if not base["required"]:
        return {**base, "valid": True, "status": "free", "message": ""}
    d = _load()
    p = verify_token(d.get("token", "")) if d.get("token") else None
    now, rolled = _now_checked(d, p)
    if p is None:
        return {**base, "valid": False, "status": "unregistered" if not d.get("registered") else "pending",
                "message": "Máy này chưa được kích hoạt. Đã gửi mã máy cho Admin — chờ Admin duyệt, hoặc dán mã kích hoạt."
                if d.get("registered") else "Máy này chưa đăng ký sử dụng. Bấm “Gửi yêu cầu kích hoạt”."}
    st, exp = p.get("st"), int(p.get("exp") or 0)
    out = {**base, "status": st, "expires_at": exp, "issued_at": p.get("iat"), "reset_pw": int(p.get("rp") or 0),
           "days_left": None if not exp else max(0, int((exp - now) // 86400)), "note": p.get("msg", "")}
    if rolled:
        return {**out, "valid": False, "status": "clock", "message": "Đồng hồ máy tính đang bị chỉnh lùi. Hãy đặt lại ngày giờ đúng."}
    if st == "blocked":
        return {**out, "valid": False, "message": "Máy này đã bị Admin khoá. Liên hệ Admin."}
    if st == "pending":
        return {**out, "valid": False, "message": "Đã gửi yêu cầu — đang chờ Admin kích hoạt và đặt hạn dùng."}
    if st != "active":
        return {**out, "valid": False, "message": "Giấy phép không hợp lệ."}
    if exp and now >= exp:
        return {**out, "valid": False, "status": "expired", "message": "Đã hết hạn sử dụng. Liên hệ Admin để gia hạn."}
    return {**out, "valid": True, "message": ""}


_valid_cache = {"t": 0.0, "v": False}


def is_valid() -> bool:
    """Kiểm tra nhanh cho middleware (nhớ 5 giây)."""
    if not required():
        return True
    if time.time() - _valid_cache["t"] > 5:
        _valid_cache.update(t=time.time(), v=state()["valid"])
    return _valid_cache["v"]


def touch_clock():
    """Ghi lại thời điểm lớn nhất app từng thấy (phục vụ chống lùi đồng hồ)."""
    if not required():
        return
    with _lock:
        d = _load()
        now = time.time()
        if now > float(d.get("seen", 0)):
            d["seen"] = now
            _save(d)


# ------------------------------------------------------------------ máy chủ Admin
def _post(path: str, body: dict, timeout: float = 12) -> dict:
    url = server_url()
    if not url:
        raise RuntimeError("Bản này chưa có địa chỉ máy chủ Admin — hãy nhập địa chỉ, hoặc dán mã kích hoạt.")
    req = urllib.request.Request(url + path, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json", "User-Agent": f"MovieReviewEditor/{__version__}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(e.read().decode("utf-8", "replace")).get("detail")
        except Exception:
            msg = None
        raise RuntimeError(msg or f"Máy chủ Admin trả lỗi {e.code}") from e
    except (urllib.error.URLError, OSError) as e:
        raise RuntimeError("Không kết nối được máy chủ Admin (máy Admin đang tắt hoặc sai địa chỉ).") from e


def _device_info() -> dict:
    try:
        from . import sysinfo
        hw = sysinfo.scan()
        summary = f"{hw.get('cpu', '')} · RAM {hw.get('ram_gb', '?')} GB · " + ", ".join(g["name"] for g in hw.get("gpus", []))
    except Exception:
        summary = ""
    return {"mk": machine_key(), "host": socket.gethostname(), "os": f"{platform.system()} {platform.release()}",
            "ver": __version__, "hw": summary[:300]}


def _accept(resp: dict) -> None:
    tok = resp.get("token")
    if tok:
        p = store_token(tok)
        srv = float(resp.get("server_time") or 0)
        if srv:                                     # đồng hồ thật của máy chủ cũng làm mốc chống lùi giờ
            with _lock:
                d = _load()
                d["seen"] = max(float(d.get("seen", 0)), srv)
                _save(d)
        _status.update(server_ok=True, server_msg="")
        _valid_cache["t"] = 0
        log.info("Giấy phép: %s, hết hạn %s", p.get("st"), p.get("exp"))


def register(name: str = "") -> dict:
    """Gửi mã máy (+ tên người dùng) về máy chủ Admin. Gọi tự động lần đầu và khi người dùng bấm gửi."""
    with _lock:
        d = _load()
        if name.strip():
            d["name"] = name.strip()[:80]
        _save(d)
    try:
        resp = _post("/api/v1/register", {**_device_info(), "name": d.get("name", "")})
        with _lock:
            d = _load()
            d["registered"] = True
            _save(d)
        _accept(resp)
    except RuntimeError as e:
        _status.update(server_ok=False, server_msg=str(e))
        raise
    return state()


def sync() -> dict:
    if not required():
        return state()
    try:
        d = _load()
        if not d.get("registered") and not d.get("token"):
            return register()
        resp = _post("/api/v1/check", {**_device_info(), "name": d.get("name", "")})
        _accept(resp)
    except RuntimeError as e:
        _status.update(server_ok=False, server_msg=str(e))
    finally:
        _status["last_sync"] = time.time()
        touch_clock()
    return state()


def activate(code: str) -> dict:
    store_token(code)
    _valid_cache["t"] = 0
    return state()


def set_server(url: str):
    settings.update(license_server=url.strip().rstrip("/"))


def start_background():
    if not required():
        return

    def loop():
        machine_key()                       # tính mã máy sớm (PowerShell ~1 giây)
        while True:
            try:
                sync()
            except Exception:
                log.exception("Đồng bộ giấy phép lỗi")
            time.sleep(SYNC_EVERY)

    threading.Thread(target=loop, name="license-sync", daemon=True).start()
