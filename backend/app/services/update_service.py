"""Cập nhật phiên bản online qua GitHub Releases.

Mỗi lần đẩy tag vX.Y.Z, GitHub Actions build và đăng Release gồm:
  MovieReviewEditor-Setup-X.Y.Z.exe     (cài một chạm)   -> app cài bằng Setup tự cập nhật bằng file này
  MovieReviewEditor-Portable-X.Y.Z.exe  (1 file chạy ngay) -> bản portable tự thay file exe của chính nó
Nguồn mặc định = repo đã build ra app (build_info.UPDATE_REPO). Có thể đổi trong Cài đặt: "chu/repo" hoặc URL tới
file JSON {"version","notes","setup_url","portable_url","setup_sha256","portable_sha256"}.
Repo phải ở chế độ Public (GitHub không cho máy khác đọc Releases của repo Private khi không có token).
"""
import hashlib
import json
import logging
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from .. import build_info
from ..config import settings
from ..utils.paths import data_dir
from ..version import __version__

log = logging.getLogger("mre.update")
UA = f"MovieReviewEditor/{__version__}"
CHECK_TTL = 6 * 3600
_lock = threading.Lock()
_state: dict = {"phase": "idle", "progress": 0.0, "error": "", "file": "", "kind": ""}
_last: dict = {}


def parse_version(v: str) -> tuple:
    nums = re.findall(r"\d+", (v or "").split("-")[0].split("+")[0])
    return tuple(int(x) for x in nums[:4]) or (0,)


def is_newer(remote: str, local: str = __version__) -> bool:
    return parse_version(remote) > parse_version(local)


def dist_kind() -> str:
    """Cách app đang được cài -> quyết định cách tự cập nhật."""
    if not getattr(sys, "frozen", False):
        return "source"
    if build_info.DIST == "portable":
        return "portable"
    if any(Path(sys.executable).parent.glob("unins*.exe")):
        return "setup"
    return "folder"


def cleanup():
    """Mở app lên: xoá bộ cài đã dùng ở lần cập nhật trước (đỡ tốn ổ đĩa)."""
    for f in (data_dir() / "updates").glob("*"):
        try:
            f.unlink()
        except OSError:
            pass


def source() -> str:
    return (settings._load().get("update_source") or build_info.UPDATE_REPO or "").strip()   # noqa: SLF001


def _get_json(url: str, timeout: float = 15) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def _from_github(repo: str) -> dict:
    try:
        rel = _get_json(f"https://api.github.com/repos/{repo}/releases/latest")
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise RuntimeError(f"Không thấy bản phát hành nào ở {repo}. Repo phải ở chế độ Public và đã có Release "
                               "(đẩy tag vX.Y.Z để GitHub Actions tự tạo).") from e
        if e.code == 403:
            raise RuntimeError("GitHub tạm giới hạn lượt kiểm tra, thử lại sau ít phút.") from e
        raise
    out = {"version": (rel.get("tag_name") or "").lstrip("vV"), "notes": (rel.get("body") or "")[:3000],
           "published": rel.get("published_at", ""), "page": rel.get("html_url", "")}
    for a in rel.get("assets") or []:
        name, url = a.get("name", ""), a.get("browser_download_url", "")
        digest = (a.get("digest") or "").removeprefix("sha256:")
        if re.search(r"setup.*\.exe$", name, re.I):
            out.update(setup_url=url, setup_size=a.get("size"), setup_sha256=digest, setup_name=name)
        elif re.search(r"portable.*\.exe$", name, re.I):
            out.update(portable_url=url, portable_size=a.get("size"), portable_sha256=digest, portable_name=name)
    return out


def check(force: bool = False) -> dict:
    src = source()
    base = {"current": __version__, "dist": dist_kind(), "source": src, "auto_check": settings._load().get("update_auto", True)}  # noqa: SLF001
    if not src:
        return {**base, "available": False, "error": "Chưa đặt nguồn cập nhật (repo GitHub). Bản build bằng GitHub Actions có sẵn."}
    if not force and not base["auto_check"] and _last.get("src") != src:
        return {**base, "available": False, "error": ""}          # tắt tự kiểm tra: chỉ kiểm tra khi bấm nút
    with _lock:
        if not force and _last.get("src") == src and time.time() - _last.get("t", 0) < CHECK_TTL:
            return {**base, **_last["data"]}
    try:
        if src.startswith("http"):
            info = _get_json(src)
            info["version"] = str(info.get("version", "")).lstrip("vV")
        elif re.fullmatch(r"[\w.-]+/[\w.-]+", src):
            info = _from_github(src)
        else:
            raise RuntimeError("Nguồn cập nhật phải dạng chu-repo/ten-repo hoặc URL https tới file JSON")
        data = {"latest": info.get("version"), "available": bool(info.get("version")) and is_newer(info["version"]),
                "notes": info.get("notes", ""), "page": info.get("page", ""), "published": info.get("published", ""),
                "assets": {k: info.get(k) for k in ("setup_url", "setup_size", "setup_sha256", "setup_name",
                                                     "portable_url", "portable_size", "portable_sha256", "portable_name")},
                "error": ""}
    except Exception as e:
        log.warning("Kiểm tra cập nhật lỗi: %s", e)
        data = {"available": False, "error": str(e)[:300] if isinstance(e, RuntimeError) else f"Không kiểm tra được: {e}"}
        return {**base, **data}
    with _lock:
        _last.update(src=src, t=time.time(), data=data)
    return {**base, **data}


def state() -> dict:
    return dict(_state)


def _which_asset(info: dict) -> str:
    d = dist_kind()
    a = info.get("assets") or {}
    if d == "portable" and a.get("portable_url"):
        return "portable"
    if a.get("setup_url"):
        return "setup"
    if a.get("portable_url"):
        return "portable"
    raise RuntimeError("Bản phát hành không có file Setup.exe / Portable.exe")


def start_download() -> dict:
    info = check()
    if not info.get("available"):
        raise RuntimeError(info.get("error") or "Đang dùng bản mới nhất")
    if _state["phase"] == "downloading":
        return state()
    kind = _which_asset(info)
    a = info["assets"]
    url, size, sha = a[f"{kind}_url"], a.get(f"{kind}_size"), (a.get(f"{kind}_sha256") or "").lower()
    if not url.startswith("https://"):
        raise RuntimeError("Link tải phải là https")
    folder = data_dir() / "updates"
    folder.mkdir(parents=True, exist_ok=True)
    for old in folder.glob("*"):                       # chỉ giữ 1 bản tải về -> không tốn ổ đĩa
        try:
            old.unlink()
        except OSError:
            pass
    dest = folder / (a.get(f"{kind}_name") or f"MovieReviewEditor-{kind}-{info['latest']}.exe")
    _state.update(phase="downloading", progress=0.0, error="", file="", kind=kind, version=info["latest"])

    def run():
        part = dest.with_suffix(".part")
        try:
            h = hashlib.sha256()
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=60) as r, open(part, "wb") as f:
                total = int(r.headers.get("Content-Length") or size or 0)
                got = 0
                while True:
                    chunk = r.read(1 << 20)
                    if not chunk:
                        break
                    f.write(chunk)
                    h.update(chunk)
                    got += len(chunk)
                    _state["progress"] = round(got / total, 3) if total else 0.0
            if size and got != int(size):
                raise RuntimeError("File tải về không đủ dung lượng (mạng bị ngắt?)")
            if sha and h.hexdigest() != sha:
                raise RuntimeError("File tải về không khớp mã kiểm tra — đã huỷ để an toàn")
            part.replace(dest)
            _state.update(phase="ready", progress=1.0, file=str(dest))
        except Exception as e:
            log.exception("Tải bản cập nhật lỗi")
            part.unlink(missing_ok=True)
            _state.update(phase="error", error=str(e)[:300])

    threading.Thread(target=run, name="update-download", daemon=True).start()
    return state()


def _exit_soon(delay: float = 1.5):
    threading.Timer(delay, lambda: os._exit(0)).start()


def apply() -> dict:
    """Cài bản đã tải rồi thoát app; bản mới tự mở lại."""
    if _state.get("phase") != "ready" or not Path(_state.get("file", "")).exists():
        raise RuntimeError("Chưa tải xong bản cập nhật")
    new = Path(_state["file"])
    flags = 0x00000008 | 0x00000200 if os.name == "nt" else 0     # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    d = dist_kind()
    if _state.get("kind") == "setup":
        # Cài im lặng đè lên bản cũ (giữ nguyên dữ liệu); /UPDATE=1 -> bộ cài tự mở lại app khi xong.
        args = [str(new), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/CLOSEAPPLICATIONS", "/FORCECLOSEAPPLICATIONS",
                "/UPDATE=1", f"/LOG={data_dir() / 'logs' / 'update_install.log'}"]
        if d != "setup":           # bản thư mục giải nén / source: chạy bộ cài có giao diện để cài bản chuẩn
            args = [str(new)]
        subprocess.Popen(args, creationflags=flags, close_fds=True)
        _state["phase"] = "installing"
        _exit_soon()
        return {**state(), "message": "Đang cài bản mới, ứng dụng sẽ tự mở lại."}
    if d != "portable" or os.name != "nt":
        raise RuntimeError("Bản này không tự thay được. Hãy mở file đã tải: " + str(new))
    # Portable: script nhỏ chờ app thoát -> thay file exe -> mở lại -> tự xoá.
    exe = Path(sys.executable)
    bat = Path(tempfile.gettempdir()) / "mre_update.cmd"
    bat.write_text(
        "@echo off\r\n"
        f"set PID={os.getpid()}\r\n"
        ":wait\r\n"
        'tasklist /FI "PID eq %PID%" 2>nul | find "%PID%" >nul && (timeout /t 1 /nobreak >nul & goto wait)\r\n'
        "set /a N=0\r\n"
        ":copy\r\n"
        f'copy /y "{new}" "{exe}" >nul 2>nul || (set /a N+=1 & timeout /t 1 /nobreak >nul & if %N% lss 20 goto copy)\r\n'
        f'start "" "{exe}"\r\n'
        f'del "{new}" >nul 2>nul\r\n'
        '(goto) 2>nul & del "%~f0"\r\n', "utf-8")
    subprocess.Popen(["cmd", "/c", str(bat)], creationflags=flags | 0x08000000, close_fds=True)   # CREATE_NO_WINDOW
    _state["phase"] = "installing"
    _exit_soon()
    return {**state(), "message": "Đang thay bằng bản mới, ứng dụng sẽ tự mở lại."}
