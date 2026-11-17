"""Đăng nhập ứng dụng.

CHẾ ĐỘ TÀI KHOẢN (bản build có gắn khoá giấy phép của Admin):
- Đăng ký: tên đăng nhập + mật khẩu (+ họ tên, SĐT) -> gửi lên máy chủ Admin, chờ Admin gửi mã kích hoạt.
- Đăng nhập trên máy ĐÃ kích hoạt: kiểm tra ngay trên máy (bản băm lưu sẵn) -> không cần mạng.
- Đăng nhập trên máy CHƯA kích hoạt (máy khác / tài khoản khác): hỏi máy chủ -> tạo yêu cầu kích hoạt máy này.
- Đổi mật khẩu: qua máy chủ (dùng được ở mọi máy của tài khoản). Quên mật khẩu: Admin đặt mật khẩu mới.

CHẾ ĐỘ CỤC BỘ (bản build không gắn khoá giấy phép): key = mã máy + mật khẩu đặt trên chính máy này.

Phiên đăng nhập chỉ nằm trong bộ nhớ: đóng app là phải đăng nhập lại. Sai nhiều lần bị khoá tạm (tăng dần).
"""
import hashlib
import hmac
import json
import secrets
import threading
import time

from .services import license_service as L
from .utils.paths import data_dir

COOKIE = "mre_session"
ITERATIONS = 240_000
SESSION_TTL = 24 * 3600
MIN_PW = 6
FREE_FAILS = 5
MAX_LOCK = 15 * 60


class AuthError(Exception):
    def __init__(self, msg: str, status: int = 400, **extra):
        super().__init__(msg)
        self.status = status
        self.extra = extra


def _hash(secret: str, salt_hex: str, iterations: int) -> str:
    return hashlib.pbkdf2_hmac("sha256", secret.encode("utf-8"), bytes.fromhex(salt_hex), iterations).hex()


def make_record(password: str) -> dict:
    sp, it = secrets.token_hex(16), ITERATIONS
    return {"v": 2, "iter": it, "pw_salt": sp, "pw_hash": _hash(password, sp, it)}


class Auth:
    def __init__(self):
        self._lock = threading.Lock()
        self._sessions: dict[str, tuple[float, str]] = {}
        self._fails = 0
        self._locked_until = 0.0

    # ---------------------------------------------------------------- lưu trữ
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
    def account_mode() -> bool:
        return L.required()

    def configured(self) -> bool:
        """Chế độ cục bộ: đã đặt mật khẩu chưa. Chế độ tài khoản: luôn True (đăng ký/đăng nhập qua tài khoản)."""
        if self.account_mode():
            return True
        rec = self._record()
        return rec is not None and rec.get("mode") != "account"

    # ---------------------------------------------------------------- khoá tạm khi sai nhiều
    def _check_lock(self):
        left = self._locked_until - time.time()
        if left > 0:
            raise AuthError(f"Sai quá nhiều lần. Thử lại sau {int(left) + 1} giây.", 429)

    def _fail(self):
        self._fails += 1
        if self._fails >= FREE_FAILS:
            self._locked_until = time.time() + min(MAX_LOCK, 30 * 2 ** (self._fails - FREE_FAILS))

    def _ok(self):
        self._fails, self._locked_until = 0, 0.0

    @staticmethod
    def _verify(rec: dict, secret: str) -> bool:
        it = int(rec.get("iter", ITERATIONS))
        return hmac.compare_digest(_hash(secret or "", rec["pw_salt"], it), rec["pw_hash"])

    # ---------------------------------------------------------------- chế độ TÀI KHOẢN
    def _cache_account(self, user: str, password: str):
        """Lưu bản băm mật khẩu tài khoản trên máy đã kích hoạt -> lần sau đăng nhập không cần mạng."""
        rec = make_record(L.prehash(user, password))
        rec.update(mode="account", user=L.norm_user(user))
        self._write(rec)

    @staticmethod
    def _server_error(e: L.ServerError) -> AuthError:
        if e.status == 401:
            return AuthError(str(e), 401)
        if e.status == 0:
            return AuthError(str(e), 503)
        return AuthError(str(e), e.status or 400)

    def register(self, username: str, password: str, name: str = "", phone: str = "") -> dict:
        u = L.norm_user(username)
        if len(password or "") < MIN_PW:
            raise AuthError(f"Mật khẩu cần ít nhất {MIN_PW} ký tự")
        try:
            st = L.register(u, password, name, phone)
        except L.ServerError as e:
            raise self._server_error(e) from e
        if st["valid"]:                                  # Admin bật dùng thử -> vào luôn
            self._cache_account(u, password)
            return {**st, "session": self._new_session(u)}
        return st

    def login_account(self, username: str, password: str) -> str:
        u = L.norm_user(username)
        if not u or not password:
            raise AuthError("Nhập tên đăng nhập và mật khẩu")
        with self._lock:
            self._check_lock()
            st = L.state()
            rec = self._record()
            activated_here = st["valid"] and st.get("user") == u
            if activated_here and rec and rec.get("mode") == "account" and rec.get("user") == u \
                    and self._verify(rec, L.prehash(u, password)):
                self._ok()
                threading.Thread(target=L.sync, daemon=True).start()      # cập nhật hạn dùng ở nền
                return self._new_session(u)
        # Máy chưa kích hoạt cho tài khoản này, hoặc mật khẩu vừa đổi ở máy khác / Admin đặt lại -> hỏi máy chủ
        try:
            res = L.login_online(u, password)
        except L.ServerError as e:
            with self._lock:
                if e.status == 401:
                    self._fail()
            if e.status == 0 and activated_here:
                raise AuthError("Mật khẩu không đúng.", 401) from e
            raise self._server_error(e) from e
        with self._lock:
            self._ok()
            if res["valid"] and res.get("user") == u:
                self._cache_account(u, password)
                return self._new_session(u)
        raise AuthError(res.get("server_message") or res.get("message") or "Máy này chưa được kích hoạt", 403,
                        need_activation=res.get("status") == "pending", license=res)

    # ---------------------------------------------------------------- chế độ CỤC BỘ
    def setup(self, password: str) -> str:
        with self._lock:
            if self.account_mode():
                raise AuthError("Bản này dùng tài khoản: hãy Đăng ký hoặc Đăng nhập.", 409)
            if self.configured():
                raise AuthError("Đã có mật khẩu. Hãy đăng nhập.", 409)
            if len(password or "") < MIN_PW:
                raise AuthError(f"Mật khẩu cần ít nhất {MIN_PW} ký tự")
            self._write(make_record(password))
            self._ok()
            return self._new_session("")

    def login_local(self, password: str) -> str:
        with self._lock:
            self._check_lock()
            rec = self._record()
            if rec is None or not self.configured():
                raise AuthError("Chưa đặt mật khẩu cho máy này", 409)
            if not self._verify(rec, password):
                self._fail()
                left = FREE_FAILS - self._fails
                raise AuthError("Mật khẩu không đúng." + (f" Còn {left} lần thử." if left > 0 else " Đã bị khoá tạm thời."), 401)
            self._ok()
            if int(rec.get("v", 1)) < 2:                   # nâng cấp dữ liệu bản cũ
                self._write(make_record(password))
            return self._new_session("")

    # ---------------------------------------------------------------- chung
    def change(self, token: str | None, old_pw: str, new_pw: str):
        with self._lock:
            sess = self._sessions.get(token or "")
            if not sess or sess[0] < time.time():
                raise AuthError("Cần đăng nhập", 401)
            self._check_lock()
        if len(new_pw or "") < MIN_PW:
            raise AuthError(f"Mật khẩu mới cần ít nhất {MIN_PW} ký tự")
        if new_pw == old_pw:
            raise AuthError("Mật khẩu mới phải khác mật khẩu cũ")
        if self.account_mode():
            user = sess[1]
            try:
                L.change_password(user, old_pw, new_pw)
            except L.ServerError as e:
                with self._lock:
                    if e.status == 401:
                        self._fail()
                raise self._server_error(e) from e
            with self._lock:
                self._cache_account(user, new_pw)
                self._sessions = {token: (time.time() + SESSION_TTL, user)}
            return
        with self._lock:
            rec = self._record()
            if rec is None or not self._verify(rec, old_pw):
                self._fail()
                raise AuthError("Mật khẩu hiện tại không đúng.", 401)
            self._write(make_record(new_pw))
            self._sessions = {token: (time.time() + SESSION_TTL, "")}

    def _new_session(self, user: str) -> str:
        now = time.time()
        self._sessions = {t: v for t, v in self._sessions.items() if v[0] > now}
        tok = secrets.token_urlsafe(32)
        self._sessions[tok] = (now + SESSION_TTL, user)
        return tok

    def session_user(self, token: str | None) -> str | None:
        with self._lock:
            v = self._sessions.get(token or "")
            if not v:
                return None
            if v[0] < time.time():
                self._sessions.pop(token, None)
                return None
            return v[1]

    def check(self, token: str | None) -> bool:
        return self.session_user(token) is not None

    def logout(self, token: str | None):
        with self._lock:
            self._sessions.pop(token or "", None)

    def logout_all(self):
        with self._lock:
            self._sessions.clear()


auth = Auth()
