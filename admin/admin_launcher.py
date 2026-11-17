"""Tool Admin — Movie Review Editor.

Bấm đôi: mở cửa sổ quản lý + chạy máy chủ nhận đăng ký ở cổng 8800 (máy khách trong mạng LAN / Internet gọi vào).
Chạy trên VPS / máy chủ không màn hình:  MovieReviewEditor-Admin.exe --server [--port 8800] [--allow-remote-admin]
"""
import ctypes
import logging
import os
import socket
import sys
import threading
import time
import urllib.request
from logging.handlers import RotatingFileHandler

TITLE = "Movie Review Editor — Admin"


def _arg(name, default=None):
    if name in sys.argv:
        i = sys.argv.index(name)
        return sys.argv[i + 1] if i + 1 < len(sys.argv) else default
    return default


def _fix_streams():
    for n in ("stdout", "stderr"):
        if getattr(sys, n) is None:
            setattr(sys, n, open(os.devnull, "w", encoding="utf-8"))


def _alive(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=1.5) as r:
            return b"MovieReviewEditorAdmin" in r.read()
    except Exception:
        return False


def _msg(text: str, error: bool = False):
    if os.name == "nt":
        try:
            ctypes.windll.user32.MessageBoxW(None, text, TITLE, 0x10 if error else 0x40)
            return
        except Exception:
            pass
    print(text)


def main():
    _fix_streams()
    port = int(_arg("--port", os.getenv("MRE_ADMIN_PORT", "8800")))
    os.environ["MRE_ADMIN_PORT"] = str(port)
    if "--allow-remote-admin" in sys.argv:
        os.environ["MRE_ADMIN_REMOTE"] = "1"
    from admin_app import core
    logf = core.data_dir() / "admin.log"
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        handlers=[RotatingFileHandler(logf, maxBytes=2_000_000, backupCount=2, encoding="utf-8")])
    headless = "--server" in sys.argv
    if _alive(port):
        if headless:
            print(f"Máy chủ Admin đã chạy ở cổng {port}")
            return
    else:
        with socket.socket() as s:
            try:
                s.bind(("0.0.0.0", port))
            except OSError:
                _msg(f"Cổng {port} đang bị chương trình khác dùng. Chạy với --port <số khác>.", True)
                sys.exit(1)
        core.load_or_create_key()                     # lần đầu: tạo khoá ký
        import uvicorn
        from admin_app.server import app
        server = uvicorn.Server(uvicorn.Config(app, host="0.0.0.0", port=port, log_config=None, log_level="info",
                                               proxy_headers=True, forwarded_allow_ips="127.0.0.1"))
        if headless:
            print(f"Máy chủ Admin chạy ở cổng {port}. Dữ liệu: {core.data_dir()}")
            server.run()
            return
        threading.Thread(target=server.run, daemon=True).start()
        for _ in range(100):
            if _alive(port):
                break
            time.sleep(0.2)
    url = f"http://127.0.0.1:{port}/admin/"
    try:
        import webview
        webview.create_window(TITLE, url, width=1360, height=860, min_size=(1000, 640), background_color="#0f1216")
        webview.start(gui="edgechromium" if os.name == "nt" else None, private_mode=False,
                      storage_path=str(core.data_dir() / "webview"))
    except Exception:
        import webbrowser
        webbrowser.open(url)
        _msg("Đang mở trang quản lý trong trình duyệt. Đóng hộp thoại này sẽ TẮT máy chủ Admin.")
    os._exit(0)


if __name__ == "__main__":
    main()
