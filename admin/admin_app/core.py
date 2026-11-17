"""Lõi tool Admin: khoá ký giấy phép (Ed25519), TÀI KHOẢN + MÁY (SQLite), mã kích hoạt, mật khẩu Admin.

Mô hình:
- Tài khoản (username + mật khẩu) do người dùng ĐĂNG KÝ trong app. Hạn dùng / khoá đặt theo TÀI KHOẢN.
- Mỗi tài khoản dùng trên một số máy (mặc định 1). Mỗi máy phải được Admin KÍCH HOẠT bằng mã ngắn (XXXX-XXXX).
- Kích hoạt máy mới khi đã đủ số máy -> máy cũ nhất bị thu hồi (tài khoản "chuyển" sang máy mới).
- Mật khẩu: app băm trước (PBKDF2 theo tên đăng nhập) rồi mới gửi; máy chủ băm thêm lần nữa với muối ngẫu nhiên.
"""
import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

_lock = threading.RLock()
CODE_TTL = 7 * 86400
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"        # bỏ I, O, 0, 1 cho khỏi nhầm
USER_RE = re.compile(r"^[a-z0-9_.-]{3,32}$")


def data_dir() -> Path:
    d = Path(os.getenv("MRE_ADMIN_DIR") or (Path.home() / "MovieReviewEditorAdmin"))
    d.mkdir(parents=True, exist_ok=True)
    return d


def b64e(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def b64d(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def norm_user(u: str) -> str:
    return (u or "").strip().lower()


def prehash(username: str, password: str) -> str:
    """Giống hệt hàm trong app khách (app gửi chuỗi này thay cho mật khẩu thật)."""
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), ("MRE-ACC|" + norm_user(username)).encode(),
                               100_000).hex()


# ------------------------------------------------------------------ khoá ký
def _key_file() -> Path:
    return data_dir() / "signing_key.json"


def load_or_create_key() -> Ed25519PrivateKey:
    with _lock:
        f = _key_file()
        if f.exists():
            return Ed25519PrivateKey.from_private_bytes(b64d(json.loads(f.read_text("utf-8"))["private"]))
        sk = Ed25519PrivateKey.generate()
        raw = sk.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption())
        f.write_text(json.dumps({"private": b64e(raw), "public": b64e(sk.public_key().public_bytes_raw()),
                                 "created": int(time.time())}), "utf-8")
        return sk


def public_key() -> str:
    return b64e(load_or_create_key().public_key().public_bytes_raw())


def export_keys() -> dict:
    return json.loads(_key_file().read_text("utf-8")) if _key_file().exists() else {}


def import_keys(d: dict) -> str:
    sk = Ed25519PrivateKey.from_private_bytes(b64d(d["private"]))
    pub = b64e(sk.public_key().public_bytes_raw())
    with _lock:
        _key_file().write_text(json.dumps({"private": d["private"], "public": pub, "created": int(time.time())}), "utf-8")
    return pub


def sign(payload: dict) -> str:
    body = b64e(json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode())
    return body + "." + b64e(load_or_create_key().sign(body.encode()))


# ------------------------------------------------------------------ cơ sở dữ liệu
@contextmanager
def db():
    c = _connect()
    try:
        yield c
        c.commit()
    finally:
        c.close()


def _connect() -> sqlite3.Connection:
    c = sqlite3.connect(data_dir() / "admin.db", check_same_thread=False, timeout=10)
    c.row_factory = sqlite3.Row
    cols = [r[1] for r in c.execute("PRAGMA table_info(devices)")]
    if cols and "username" not in cols:                       # bảng của bản 1.4.0 (chưa có tài khoản)
        c.execute("ALTER TABLE devices RENAME TO devices_v140")
    c.execute("""CREATE TABLE IF NOT EXISTS accounts(
        username TEXT PRIMARY KEY, name TEXT DEFAULT '', phone TEXT DEFAULT '', note TEXT DEFAULT '',
        pw_salt TEXT, pw_hash TEXT, status TEXT DEFAULT 'pending', exp INTEGER DEFAULT 0, max_devices INTEGER DEFAULT 1,
        created REAL, last_login REAL DEFAULT 0)""")
    c.execute("""CREATE TABLE IF NOT EXISTS devices(
        username TEXT, mk TEXT, host TEXT DEFAULT '', os TEXT DEFAULT '', ver TEXT DEFAULT '', hw TEXT DEFAULT '',
        ip TEXT DEFAULT '', first_seen REAL, last_seen REAL, status TEXT DEFAULT 'pending', code TEXT DEFAULT '',
        code_exp REAL DEFAULT 0, activated_at REAL DEFAULT 0, seen_new INTEGER DEFAULT 1, PRIMARY KEY(username, mk))""")
    c.execute("CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, v TEXT)")
    c.execute("CREATE TABLE IF NOT EXISTS log(id INTEGER PRIMARY KEY AUTOINCREMENT, t REAL, who TEXT, action TEXT, detail TEXT)")
    return c


def kv_get(k: str, default=None):
    with _lock, db() as c:
        r = c.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
        return json.loads(r["v"]) if r else default


def kv_set(k: str, v) -> None:
    with _lock, db() as c:
        c.execute("INSERT INTO kv(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, json.dumps(v)))


def add_log(who: str, action: str, detail: str = "") -> None:
    with _lock, db() as c:
        c.execute("INSERT INTO log(t,who,action,detail) VALUES(?,?,?,?)", (time.time(), who, action, detail))


def _row(r) -> dict | None:
    return dict(r) if r else None


def get_account(username: str) -> dict | None:
    with _lock, db() as c:
        return _row(c.execute("SELECT * FROM accounts WHERE username=?", (norm_user(username),)).fetchone())


def save_account(a: dict) -> None:
    cols = ["username", "name", "phone", "note", "pw_salt", "pw_hash", "status", "exp", "max_devices", "created", "last_login"]
    with _lock, db() as c:
        c.execute(f"INSERT OR REPLACE INTO accounts({','.join(cols)}) VALUES({','.join('?' * len(cols))})",
                  [a.get(k) for k in cols])


def delete_account(username: str) -> None:
    with _lock, db() as c:
        c.execute("DELETE FROM devices WHERE username=?", (norm_user(username),))
        c.execute("DELETE FROM accounts WHERE username=?", (norm_user(username),))


def get_device(username: str, mk: str) -> dict | None:
    with _lock, db() as c:
        return _row(c.execute("SELECT * FROM devices WHERE username=? AND mk=?", (norm_user(username), mk)).fetchone())


def devices_of(username: str) -> list[dict]:
    with _lock, db() as c:
        return [dict(r) for r in c.execute("SELECT * FROM devices WHERE username=? ORDER BY activated_at DESC, last_seen DESC",
                                           (norm_user(username),))]


def save_device(d: dict) -> None:
    cols = ["username", "mk", "host", "os", "ver", "hw", "ip", "first_seen", "last_seen", "status", "code", "code_exp",
            "activated_at", "seen_new"]
    with _lock, db() as c:
        c.execute(f"INSERT OR REPLACE INTO devices({','.join(cols)}) VALUES({','.join('?' * len(cols))})",
                  [d.get(k) for k in cols])


def delete_device(username: str, mk: str) -> None:
    with _lock, db() as c:
        c.execute("DELETE FROM devices WHERE username=? AND mk=?", (norm_user(username), mk))


def list_accounts() -> list[dict]:
    with _lock, db() as c:
        accs = [dict(r) for r in c.execute("SELECT * FROM accounts ORDER BY created DESC")]
        devs = [dict(r) for r in c.execute("SELECT * FROM devices")]
    by: dict[str, list] = {}
    for d in devs:
        by.setdefault(d["username"], []).append(d)
    for a in accs:
        a.pop("pw_salt", None)
        a.pop("pw_hash", None)
        a["devices"] = sorted(by.get(a["username"], []), key=lambda d: -(d.get("last_seen") or 0))
    return accs


# ------------------------------------------------------------------ mật khẩu tài khoản
def _pw(pre: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", pre.encode(), bytes.fromhex(salt), 60_000).hex()


def set_account_password(a: dict, pre: str) -> None:
    a["pw_salt"] = secrets.token_hex(16)
    a["pw_hash"] = _pw(pre, a["pw_salt"])


def check_account_password(a: dict, pre: str) -> bool:
    return bool(a.get("pw_hash")) and hmac.compare_digest(_pw(pre or "", a["pw_salt"]), a["pw_hash"])


# ------------------------------------------------------------------ mã kích hoạt + giấy phép
def new_code() -> str:
    s = "".join(secrets.choice(CODE_ALPHABET) for _ in range(8))
    return s[:4] + "-" + s[4:]


def norm_code(c: str) -> str:
    c = re.sub(r"[^A-Z0-9]", "", (c or "").upper())
    return c[:4] + "-" + c[4:] if len(c) == 8 else c


def device_state(a: dict | None, d: dict | None) -> str:
    if a is None or d is None:
        return "unknown"
    if a["status"] == "blocked":
        return "blocked"
    if d["status"] == "revoked":
        return "revoked"
    if d["status"] != "active" or a["status"] != "active":
        return "pending"
    return "active"


def license_token(a: dict, d: dict) -> str:
    """Giấy phép đã ký cho 1 máy của 1 tài khoản (mọi trạng thái đều ký -> app tin được cả lệnh khoá/thu hồi)."""
    return sign({"v": 2, "mk": d["mk"], "u": a["username"], "st": device_state(a, d), "exp": int(a.get("exp") or 0),
                 "iat": int(time.time()), "nm": a.get("name", "")})


def activate_device(a: dict, d: dict) -> list[str]:
    """Kích hoạt máy; quá số máy cho phép -> thu hồi máy cũ nhất. Trả danh sách mã máy bị thu hồi."""
    now = time.time()
    d.update(status="active", code="", code_exp=0, activated_at=now, seen_new=0)
    save_device(d)
    active = [x for x in devices_of(a["username"]) if x["status"] == "active" and x["mk"] != d["mk"]]
    active.sort(key=lambda x: x.get("activated_at") or 0)
    revoked = []
    while len(active) + 1 > max(1, int(a.get("max_devices") or 1)):
        old = active.pop(0)
        old["status"] = "revoked"
        save_device(old)
        revoked.append(old["mk"])
    return revoked


# ------------------------------------------------------------------ mật khẩu Admin
def _admin_hash(pw: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(salt), 240_000).hex()


def admin_configured() -> bool:
    return bool(kv_get("admin_pw"))


def set_admin_password(pw: str) -> None:
    if len(pw) < 8:
        raise ValueError("Mật khẩu Admin cần ít nhất 8 ký tự")
    salt = secrets.token_hex(16)
    kv_set("admin_pw", {"salt": salt, "hash": _admin_hash(pw, salt)})


def check_admin_password(pw: str) -> bool:
    rec = kv_get("admin_pw")
    return bool(rec) and hmac.compare_digest(_admin_hash(pw or "", rec["salt"]), rec["hash"])
