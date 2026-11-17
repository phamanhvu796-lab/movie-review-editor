"""Điểm vào của ứng dụng desktop.

Chạy máy chủ nội bộ trong nền (không còn cửa sổ đen) và mở giao diện trong MỘT CỬA SỔ ỨNG DỤNG RIÊNG
(Microsoft Edge WebView2, có sẵn trên Windows 10/11) thay vì mở trình duyệt. Đóng cửa sổ = thoát ứng dụng.
Nếu máy thiếu WebView2: mở Edge ở chế độ ứng dụng (--app, cửa sổ không thanh địa chỉ); cuối cùng mới dùng trình duyệt.
"""
import ctypes
import json
import logging
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from logging.handlers import RotatingFileHandler
from pathlib import Path

PREFERRED_PORT = 8765
WINDOW_TITLE = "Movie Review Editor"
log = logging.getLogger("launcher")

LOADING_HTML = """<!doctype html><html><head><meta charset="utf-8"><style>
html,body{height:100%;margin:0;background:#101318;color:#e6e9ee;font:14px system-ui,'Segoe UI',sans-serif}
body{display:flex;align-items:center;justify-content:center;flex-direction:column;gap:14px}
.s{width:34px;height:34px;border:3px solid #2c3441;border-top-color:#4c7dff;border-radius:50%;animation:r 1s linear infinite}
@keyframes r{to{transform:rotate(360deg)}}</style></head>
<body><div class="s"></div><div>Đang khởi động Movie Review Editor…</div></body></html>"""


# ---------------------------------------------------------------- tiện ích
def fix_streams():
    """Bản exe không có console: sys.stdout/stderr = None làm uvicorn/print bị lỗi. Trỏ chúng về devnull."""
    for name in ("stdout", "stderr"):
        s = getattr(sys, name)
        if s is None:
            setattr(sys, name, open(os.devnull, "w", encoding="utf-8"))
        else:
            try:
                s.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass


def attach_parent_console():
    """--selftest chạy từ CMD: gắn vào console của CMD để in được kết quả (exe không có console riêng)."""
    if os.name != "nt":
        return
    try:
        if ctypes.windll.kernel32.AttachConsole(-1):
            sys.stdout = open("CONOUT$", "w", encoding="utf-8", errors="replace")
            sys.stderr = sys.stdout
    except Exception:
        pass


def message_box(text: str, title: str = WINDOW_TITLE, error: bool = False):
    if os.name == "nt":
        try:
            ctypes.windll.user32.MessageBoxW(None, text, title, 0x10 if error else 0x40)
            return
        except Exception:
            pass
    print(text, file=sys.stderr)


def is_our_app(port: int, timeout: float = 1.5) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=timeout) as r:
            return json.loads(r.read()).get("app") == "MovieReviewEditor"
    except Exception:
        return False


def _free(port: int) -> int | None:
    with socket.socket() as s:
        try:
            s.bind(("127.0.0.1", port))
            return s.getsockname()[1]
        except OSError:
            return None


def pick_port(preferred: int = PREFERRED_PORT) -> tuple[str, int]:
    """('existing', port): đã có một bản đang chạy. ('new', port): chạy mới."""
    if is_our_app(preferred):
        return "existing", preferred
    return "new", _free(preferred) or _free(0)


def wait_ready(port: int, limit: float = 90.0) -> bool:
    end = time.time() + limit
    while time.time() < end:
        if is_our_app(port, 1.0):
            return True
        time.sleep(0.25)
    return False


def focus_existing_window() -> bool:
    if os.name != "nt":
        return False
    try:
        u = ctypes.windll.user32
        h = u.FindWindowW(None, WINDOW_TITLE)
        if h:
            u.ShowWindow(h, 9)                 # SW_RESTORE
            u.SetForegroundWindow(h)
            return True
    except Exception:
        pass
    return False


def webview2_installed() -> bool:
    """WebView2 Runtime (Evergreen) có trong registry? Windows 11 và Windows 10 cập nhật đều có sẵn."""
    if os.name != "nt":
        return True
    try:
        import winreg
    except Exception:
        return False
    guid = r"{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"
    places = [(winreg.HKEY_LOCAL_MACHINE, rf"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{guid}"),
              (winreg.HKEY_LOCAL_MACHINE, rf"SOFTWARE\Microsoft\EdgeUpdate\Clients\{guid}"),
              (winreg.HKEY_CURRENT_USER, rf"Software\Microsoft\EdgeUpdate\Clients\{guid}")]
    for root, path in places:
        try:
            with winreg.OpenKey(root, path) as k:
                v, _ = winreg.QueryValueEx(k, "pv")
                if v and v != "0.0.0.0":
                    return True
        except OSError:
            continue
    return False


def find_edge() -> str | None:
    for base in (os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles"), os.environ.get("LOCALAPPDATA")):
        if base:
            p = Path(base) / "Microsoft" / "Edge" / "Application" / "msedge.exe"
            if p.exists():
                return str(p)
    return shutil.which("msedge") or shutil.which("microsoft-edge")


# ---------------------------------------------------------------- máy chủ
def setup_logging(data: Path) -> Path:
    (data / "logs").mkdir(parents=True, exist_ok=True)
    log_file = data / "logs" / "app.log"
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    handlers = [RotatingFileHandler(log_file, maxBytes=2_000_000, backupCount=3, encoding="utf-8")]
    if sys.stderr is not None and getattr(sys.stderr, "name", "") != os.devnull:
        handlers.append(logging.StreamHandler())
    for h in handlers:
        h.setFormatter(fmt)
        root.addHandler(h)
    return log_file


def start_server(port: int):
    import uvicorn
    from app.main import app
    # log_config=None: uvicorn không tự gắn handler ra console (bản exe không có console), log đi qua root -> app.log
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="info",
                                           timeout_keep_alive=30, log_config=None))
    t = threading.Thread(target=server.run, name="uvicorn", daemon=True)
    t.start()
    return server, t


# ---------------------------------------------------------------- cửa sổ
class DesktopApi:
    """Hàm gọi được từ giao diện (window.pywebview.api.*). Thuộc tính bắt đầu bằng _ không lộ ra JavaScript."""

    def __init__(self):
        self._window = None

    def pick_files(self, kind: str = "media", start_dir: str = ""):
        """Hộp thoại chọn file của Windows (mở sẵn ở thư mục đầu vào) -> trả ĐƯỜNG DẪN (thêm kiểu liên kết, không sao chép)."""
        import webview
        types = ("Video, âm thanh, ảnh (*.mp4;*.mov;*.mkv;*.webm;*.avi;*.mp3;*.wav;*.m4a;*.aac;*.png;*.jpg;*.jpeg;*.webp;*.cube)",
                 "Tất cả (*.*)")
        try:
            res = self._window.create_file_dialog(webview.OPEN_DIALOG, directory=start_dir or "", allow_multiple=True,
                                                  file_types=types)
        except Exception:
            log.exception("Không mở được hộp thoại chọn file")
            return []
        return list(res or [])

    def pick_folder(self, start_dir: str = ""):
        """Hộp thoại chọn THƯ MỤC (thư mục đầu vào / đầu ra)."""
        import webview
        try:
            res = self._window.create_file_dialog(webview.FOLDER_DIALOG, directory=start_dir or "")
        except Exception:
            log.exception("Không mở được hộp thoại chọn thư mục")
            return ""
        return (res[0] if isinstance(res, (list, tuple)) and res else res) or ""


def run_webview(url: str, data: Path, port: int, server_thread: threading.Thread, log_file: Path) -> bool:
    """Mở cửa sổ ứng dụng. Trả False nếu không mở được (để dùng phương án dự phòng)."""
    if not webview2_installed():
        log.warning("Không thấy Microsoft Edge WebView2 Runtime")
        return False
    try:
        import webview
    except Exception:
        log.exception("Không nạp được pywebview")
        return False
    try:
        webview.settings["ALLOW_DOWNLOADS"] = True                 # nút "Tải MP4 / SRT / JSON"
        webview.settings["OPEN_EXTERNAL_LINKS_IN_BROWSER"] = True
    except Exception:
        pass
    js_api = DesktopApi()
    win = webview.create_window(WINDOW_TITLE, html=LOADING_HTML, width=1440, height=900, min_size=(1100, 680),
                                background_color="#101318", text_select=True, js_api=js_api)
    js_api._window = win

    def after_start():
        if wait_ready(port) and server_thread.is_alive():
            win.load_url(url)
        else:
            win.load_html("<body style='background:#101318;color:#ff7b72;font:15px system-ui;padding:40px'>"
                          f"<h2>Không khởi động được máy chủ nội bộ</h2><p>Xem nhật ký: {log_file}</p></body>")

    try:
        storage = data / "webview"
        storage.mkdir(parents=True, exist_ok=True)
        kw = {"gui": "edgechromium"} if os.name == "nt" else {}
        webview.start(after_start, private_mode=False, storage_path=str(storage), **kw)
        return True
    except Exception:
        log.exception("Không mở được cửa sổ WebView")
        return False


def run_edge_app(url: str, data: Path) -> bool:
    """Dự phòng: Edge chế độ ứng dụng với hồ sơ riêng -> tiến trình riêng, chờ được tới khi người dùng đóng cửa sổ."""
    edge = find_edge()
    if not edge:
        return False
    try:
        prof = data / "edge_app_profile"
        p = subprocess.Popen([edge, f"--app={url}", f"--user-data-dir={prof}", "--no-first-run",
                              "--no-default-browser-check", "--window-size=1440,900"])
        p.wait()
        return True
    except Exception:
        log.exception("Không mở được Edge chế độ ứng dụng")
        return False


# ---------------------------------------------------------------- tự kiểm tra
def selftest(out_path: str | None = None) -> int:
    """Khởi động máy chủ thật, đăng nhập, gọi thử vài API rồi thoát (mã 0 = đạt). Chỉ in ASCII."""
    import http.cookiejar
    import tempfile
    os.environ["MRE_DATA_DIR"] = tempfile.mkdtemp(prefix="mre_selftest_")
    lines: list[str] = []
    ok_all = True

    def out(s: str):
        lines.append(s)
        try:
            print(s, flush=True)
        except Exception:
            pass

    def check(name: str, ok: bool, detail: str = "", required: bool = True):
        nonlocal ok_all
        out(f"  [{'OK ' if ok else ('LOI' if required else '-- ')}] {name} {detail}")
        ok_all = ok_all and (ok or not required)

    try:
        import uvicorn
        from app.main import app
        from app.utils.paths import find_binary
    except Exception as e:
        out("  [LOI] khong nap duoc ung dung: " + repr(e))
        ok_all = False
    else:
        port = _free(0)
        server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", log_config=None))
        threading.Thread(target=server.run, daemon=True).start()
        up = wait_ready(port, 30)
        check("May chu khoi dong, /api/health tra loi", up)
        if up:
            base = f"http://127.0.0.1:{port}"
            op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

            def call(path, body=None):
                req = urllib.request.Request(base + path, data=None if body is None else json.dumps(body).encode(),
                                             headers={"Content-Type": "application/json"})
                return json.loads(op.open(req, timeout=15).read())

            try:
                check("Giao dien (index.html)", "<title>" in op.open(base + "/", timeout=10).read().decode("utf-8", "replace"))
            except Exception as e:
                check("Giao dien (index.html)", False, repr(e))
            try:
                call("/api/projects")
                check("API bi khoa khi chua dang nhap", False)
            except urllib.error.HTTPError as e:
                check("API bi khoa khi chua dang nhap", e.code == 401)
            except Exception as e:
                check("API bi khoa khi chua dang nhap", False, repr(e))
            try:
                # Giấy phép: tạo khoá thử NGAY TRONG tiến trình tự kiểm tra, ký giấy phép cho tài khoản thử + mã máy này,
                # kích hoạt rồi đăng nhập -> kiểm tra trọn đường chữ ký số + đăng nhập tài khoản mà không cần máy chủ Admin.
                import base64 as _b64
                import time as _t
                from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
                from app.auth import auth as _auth
                from app.services import license_service as L
                sk = Ed25519PrivateKey.generate()
                L.PUBKEY_OVERRIDE = _b64.urlsafe_b64encode(sk.public_key().public_bytes_raw()).decode().rstrip("=")
                L._valid_cache["t"] = 0
                st0 = call("/api/auth/status")              # công khai (chưa đăng nhập vẫn gọi được)
                mk = st0["machine_key"]
                blocked = st0["license"]["valid"] is False and st0["mode"] == "account"
                body = _b64.urlsafe_b64encode(json.dumps({"v": 2, "mk": mk, "u": "selftest", "st": "active",
                                                          "exp": int(_t.time()) + 3600, "iat": int(_t.time())}).encode()).decode().rstrip("=")
                tok = body + "." + _b64.urlsafe_b64encode(sk.sign(body.encode())).decode().rstrip("=")
                ok = call("/api/license/activate", {"code": tok})["valid"]
                check("Giay phep tai khoan + ma may (chu ky so)", blocked and ok, mk)
                _auth._cache_account("selftest", "selftest-pass")
                call("/api/auth/login", {"username": "selftest", "password": "selftest-pass"})
                check("Dang nhap tai khoan (khong can mang)", call("/api/auth/status")["authenticated"])
            except Exception as e:
                check("Giay phep / dang nhap tai khoan", False, repr(e))
            try:
                check("Co so du lieu (tao project)", bool(call("/api/projects", {"title": "selftest"}).get("id")))
            except Exception as e:
                check("Co so du lieu (tao project)", False, repr(e))
        # Bản build (Setup và Portable) phải có sẵn FFmpeg
        check("FFmpeg co san", bool(find_binary("ffmpeg")), find_binary("ffmpeg") or "", required=getattr(sys, "frozen", False))
        check("FFprobe", bool(find_binary("ffprobe")), required=False)
        try:
            import webview  # noqa: F401
            check("Cua so ung dung (pywebview)", True)
        except Exception as e:
            check("Cua so ung dung (pywebview)", False, repr(e), required=(os.name == "nt"))
        if os.name == "nt":
            check("WebView2 Runtime tren may nay", webview2_installed(), "(thieu thi app dung Edge --app)", required=False)
        for mod in ("faster_whisper", "scenedetect"):
            try:
                __import__(mod)
                check(mod + " (tuy chon)", True, required=False)
            except Exception:
                check(mod + " (tuy chon, kieu NHE khong co)", False, required=False)
        server.should_exit = True
    out("TU KIEM TRA: " + ("DAT" if ok_all else "KHONG DAT"))
    if out_path:
        try:
            Path(out_path).write_text("\n".join(lines) + "\n", "utf-8")
        except Exception:
            pass
    return 0 if ok_all else 1


# ---------------------------------------------------------------- main
def main():
    if "--selftest" in sys.argv:
        attach_parent_console()
        fix_streams()
        i = sys.argv.index("--selftest")
        out = sys.argv[i + 1] if len(sys.argv) > i + 1 and not sys.argv[i + 1].startswith("-") else None
        code = selftest(out)
        sys.stdout.flush()
        os._exit(code)                     # thoát ngay, không chờ thread nền
    fix_streams()
    os.environ.setdefault("MRE_DATA_DIR", str(Path.home() / "MovieReviewEditor"))
    data = Path(os.environ["MRE_DATA_DIR"])
    log_file = setup_logging(data)
    browser_only = "--browser" in sys.argv

    kind, port = pick_port()
    url = f"http://127.0.0.1:{port}/"
    if kind == "existing":
        if not focus_existing_window():
            message_box("Movie Review Editor đang chạy rồi. Hãy dùng cửa sổ đang mở (xem thanh tác vụ).")
        return

    from app.version import __version__
    log.info("Movie Review Editor v%s — %s — dữ liệu: %s", __version__, url, data)
    try:
        server, thread = start_server(port)
    except Exception:
        log.exception("Không nạp được ứng dụng")
        message_box(f"Không khởi động được ứng dụng.\nChi tiết: {log_file}", error=True)
        sys.exit(1)

    shown = False
    if not browser_only:
        shown = run_webview(url, data, port, thread, log_file)
        if not shown and wait_ready(port):
            shown = run_edge_app(url, data)
    if not shown:                          # phương án cuối (hoặc chạy với --browser khi phát triển)
        if wait_ready(port):
            webbrowser.open(url)
            print(f"\n Movie Review Editor v{__version__} đang chạy tại {url}  (Ctrl+C để thoát)\n")
        try:
            while thread.is_alive():
                thread.join(1)
        except KeyboardInterrupt:
            pass

    # Cửa sổ đã đóng -> dừng máy chủ rồi thoát hẳn (job nền dang dở sẽ được đánh dấu khi mở lại).
    server.should_exit = True
    thread.join(5)
    logging.shutdown()
    os._exit(0)


if __name__ == "__main__":
    main()
