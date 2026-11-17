"""Lõi tool Admin: khoá ký giấy phép (Ed25519), cơ sở dữ liệu máy khách (SQLite), mật khẩu Admin."""
import base64
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

_lock = threading.RLock()


def data_dir() -> Path:
    d = Path(os.getenv("MRE_ADMIN_DIR") or (Path.home() / "MovieReviewEditorAdmin"))
    d.mkdir(parents=True, exist_ok=True)
    return d


def b64e(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def b64d(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


# ------------------------------------------------------------------ khoá ký
def _key_file() -> Path:
    return data_dir() / "signing_key.json"


def load_or_create_key() -> Ed25519PrivateKey:
    with _lock:
        f = _key_file()
        if f.exists():
            raw = b64d(json.loads(f.read_text("utf-8"))["private"])
            return Ed25519PrivateKey.from_private_bytes(raw)
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
    raw = b64d(d["private"])
    sk = Ed25519PrivateKey.from_private_bytes(raw)                 # kiểm tra hợp lệ
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
    c.execute("""CREATE TABLE IF NOT EXISTS devices(
        mk TEXT PRIMARY KEY, name TEXT DEFAULT '', host TEXT DEFAULT '', os TEXT DEFAULT '', ver TEXT DEFAULT '',
        hw TEXT DEFAULT '', ip TEXT DEFAULT '', first_seen REAL, last_seen REAL, status TEXT DEFAULT 'pending',
        exp INTEGER DEFAULT 0, note TEXT DEFAULT '', rp INTEGER DEFAULT 0, seen_new INTEGER DEFAULT 0)""")
    c.execute("CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, v TEXT)")
    c.execute("""CREATE TABLE IF NOT EXISTS log(id INTEGER PRIMARY KEY AUTOINCREMENT, t REAL, mk TEXT, action TEXT, detail TEXT)""")
    return c


def kv_get(k: str, default=None):
    with _lock, db() as c:
        r = c.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
        return json.loads(r["v"]) if r else default


def kv_set(k: str, v) -> None:
    with _lock, db() as c:
        c.execute("INSERT INTO kv(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, json.dumps(v)))


def add_log(mk: str, action: str, detail: str = "") -> None:
    with _lock, db() as c:
        c.execute("INSERT INTO log(t,mk,action,detail) VALUES(?,?,?,?)", (time.time(), mk, action, detail))


def get_device(mk: str) -> dict | None:
    with _lock, db() as c:
        r = c.execute("SELECT * FROM devices WHERE mk=?", (mk,)).fetchone()
        return dict(r) if r else None


def list_devices() -> list[dict]:
    with _lock, db() as c:
        return [dict(r) for r in c.execute("SELECT * FROM devices ORDER BY seen_new DESC, last_seen DESC")]


def save_device(d: dict) -> None:
    cols = ["mk", "name", "host", "os", "ver", "hw", "ip", "first_seen", "last_seen", "status", "exp", "note", "rp", "seen_new"]
    with _lock, db() as c:
        c.execute(f"INSERT OR REPLACE INTO devices({','.join(cols)}) VALUES({','.join('?' * len(cols))})",
                  [d.get(k) for k in cols])


def delete_device(mk: str) -> None:
    with _lock, db() as c:
        c.execute("DELETE FROM devices WHERE mk=?", (mk,))


def license_token(d: dict) -> str:
    """Giấy phép đã ký cho một máy (mọi trạng thái đều ký -> app khách tin được cả lệnh khoá)."""
    return sign({"v": 1, "mk": d["mk"], "st": d["status"], "exp": int(d.get("exp") or 0), "iat": int(time.time()),
                 "rp": int(d.get("rp") or 0), "nm": d.get("name", "")})


# ------------------------------------------------------------------ mật khẩu Admin
def _pw_hash(pw: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(salt), 240_000).hex()


def admin_configured() -> bool:
    return bool(kv_get("admin_pw"))


def set_admin_password(pw: str) -> None:
    if len(pw) < 8:
        raise ValueError("Mật khẩu Admin cần ít nhất 8 ký tự")
    salt = secrets.token_hex(16)
    kv_set("admin_pw", {"salt": salt, "hash": _pw_hash(pw, salt)})


def check_admin_password(pw: str) -> bool:
    rec = kv_get("admin_pw")
    return bool(rec) and hmac.compare_digest(_pw_hash(pw or "", rec["salt"]), rec["hash"])
