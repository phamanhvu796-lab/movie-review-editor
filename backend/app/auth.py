"""Đăng nhập: KEY = MÃ MÁY (cố định theo phần cứng, tự điền) + MẬT KHẨU do người dùng đặt lần đầu.

- Mật khẩu lưu dạng băm PBKDF2 ở <data_dir>/auth.json (không gửi đi đâu, kể cả máy chủ Admin).
- Đổi mật khẩu trong Cài đặt (bản Setup và Portable như nhau).
- Quên mật khẩu: Admin bấm "Cho đặt lại mật khẩu" trong tool Admin -> lần mở sau app cho tạo mật khẩu mới.
- Phiên đăng nhập chỉ nằm trong bộ nhớ: đóng app là phải đăng nhập lại. Sai nhiều lần bị khoá tạm (tăng dần).
- Tương thích dữ liệu bản cũ (v1: key tự đặt + mật khẩu): đăng nhập bằng mật khẩu cũ rồi tự nâng cấp.
"""
import hashlib
import hmac
import json
import secrets
import threading
import time

from .services import license_service
from .utils.paths import data_dir

COOKIE = "mre_session"
ITERATIONS = 240_000
SESSION_TTL = 24 * 3600
MIN_PW = 6
MIN_KEY = 0                        # giữ cho tương thích giao diện cũ (key nay là mã máy)
FREE_FAILS = 5
MAX_LOCK = 15 * 60


class AuthError(Exception):
    def __init__(self, msg: str, status: int = 400):
        super().__init__(msg)
        self.status = status


def _hash(secret: str, salt_hex: str, iterations: int) -> str:
    return hashlib.pbkdf2_hmac("sha256", secret.encode("utf-8"), bytes.fromhex(salt_hex), iterations).hex()


def make_record(password: str, reset_seen: int = 0) -> dict:
    sp, it = secrets.token_hex(16), ITERATIONS
    return {"v": 2, "iter": it, "pw_salt": sp, "pw_hash": _hash(password, sp, it), "rp": reset_seen}


class Auth:
    def __init__(self):
        self._lock = threading.Lock()
        self._sessions: dict[str, float] = {}
        self._fails = 0
        self._locked_until = 0.0

    @staticmethod
    def user_file():
        return data_dir() / "auth.json"

    def _record(self) -> dict | None:
        try:
            rec = json.loads(self.user_file().read_text("utf-8"))
            return rec if rec.get("pw_hash") else None
        except Exception:
            return None

    def _write(self, rec: dict):
        self.user_file().write_text(json.dumps(rec), "utf-8")

    @staticmethod
    def _admin_reset() -> int:
        try:
            return int(license_service.state().get("reset_pw") or 0)
        except Exception:
            return 0

    def configured(self) -> bool:
        """Đã có mật khẩu? (False khi Admin vừa cho đặt lại mật khẩu)."""
        rec = self._record()
        if rec is None:
            return False
        rp = self._admin_reset()
        return not (rp and rp > int(rec.get("rp", 0)))

    def reset_pending(self) -> bool:
        return self._record() is not None and not self.configured()

    @staticmethod
    def fixed() -> bool:
        return False

    # ---- thao tác ----
    @staticmethod
    def _validate(password: str):
        if len(password or "") < MIN_PW:
            raise AuthError(f"Mật khẩu cần ít nhất {MIN_PW} ký tự")

    @staticmethod
    def _check_key(key: str | None):
        k = license_service.normalize_key(key)
        if k and k != license_service.machine_key():
            raise AuthError("Key không đúng với máy này (key chính là mã máy, đã được điền sẵn).", 401)

    def setup(self, password: str, key: str | None = None) -> str:
        with self._lock:
            if self.configured():
                raise AuthError("Đã có mật khẩu. Hãy đăng nhập.", 409)
            self._check_key(key)
            self._validate(password)
            self._write(make_record(password, self._admin_reset()))
            self._fails, self._locked_until = 0, 0.0
            return self._new_session()

    def _check_lock(self):
        left = self._locked_until - time.time()
        if left > 0:
            raise AuthError(f"Sai quá nhiều lần. Thử lại sau {int(left) + 1} giây.", 429)

    def _fail(self):
        self._fails += 1
        if self._fails >= FREE_FAILS:
            self._locked_until = time.time() + min(MAX_LOCK, 30 * 2 ** (self._fails - FREE_FAILS))

    @staticmethod
    def _verify(rec: dict, password: str) -> bool:
        it = int(rec.get("iter", ITERATIONS))
        return hmac.compare_digest(_hash(password or "", rec["pw_salt"], it), rec["pw_hash"])

    def login(self, password: str, key: str | None = None) -> str:
        with self._lock:
            self._check_lock()
            rec = self._record()
            if rec is None or not self.configured():
                raise AuthError("Chưa đặt mật khẩu cho máy này", 409)
            self._check_key(key)
            if not self._verify(rec, password):
                self._fail()
                left = FREE_FAILS - self._fails
                hint = f" Còn {left} lần thử." if left > 0 else " Đã bị khoá tạm thời."
                raise AuthError("Mật khẩu không đúng." + hint, 401)
            self._fails, self._locked_until = 0, 0.0
            if int(rec.get("v", 1)) < 2:                    # nâng cấp dữ liệu bản cũ
                self._write(make_record(password, int(rec.get("rp", 0))))
            return self._new_session()

    def change(self, token: str | None, old_pw: str, new_pw: str):
        with self._lock:
            if not self._valid(token):
                raise AuthError("Cần đăng nhập", 401)
            self._check_lock()
            rec = self._record()
            if rec is None or not self._verify(rec, old_pw):
                self._fail()
                raise AuthError("Mật khẩu hiện tại không đúng.", 401)
            self._validate(new_pw)
            if new_pw == old_pw:
                raise AuthError("Mật khẩu mới phải khác mật khẩu cũ")
            self._write(make_record(new_pw, int(rec.get("rp", 0))))
            self._sessions = {token: time.time() + SESSION_TTL}      # đăng xuất mọi phiên khác

    def _new_session(self) -> str:
        now = time.time()
        self._sessions = {t: e for t, e in self._sessions.items() if e > now}
        tok = secrets.token_urlsafe(32)
        self._sessions[tok] = now + SESSION_TTL
        return tok

    def _valid(self, token: str | None) -> bool:
        if not token:
            return False
        exp = self._sessions.get(token)
        if exp is None:
            return False
        if exp < time.time():
            self._sessions.pop(token, None)
            return False
        return True

    def check(self, token: str | None) -> bool:
        with self._lock:
            return self._valid(token)

    def logout(self, token: str | None):
        with self._lock:
            self._sessions.pop(token or "", None)

    def logout_all(self):
        with self._lock:
            self._sessions.clear()


auth = Auth()
