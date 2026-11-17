"""Tải FFmpeg tự động (Windows) vào <thư mục dữ liệu>/bin khi máy chưa có."""
import os
import shutil
import tempfile
import threading
import urllib.request
import zipfile
from pathlib import Path

from ..config import DEFAULT_UA
from ..utils.paths import data_dir, find_binary

URLS = ["https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip",
        "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip"]
_state = {"installing": False, "progress": 0, "error": "", "message": ""}
_lock = threading.Lock()


def state() -> dict:
    return {**_state, "installed": bool(find_binary("ffmpeg") and find_binary("ffprobe")), "supported": os.name == "nt"}


def extract_ffmpeg(zip_path, dest: Path) -> list[str]:
    """Lấy ffmpeg.exe và ffprobe.exe ở bất kỳ thư mục con nào trong file zip."""
    dest.mkdir(parents=True, exist_ok=True)
    got = []
    with zipfile.ZipFile(zip_path) as z:
        for name in z.namelist():
            base = name.rsplit("/", 1)[-1].lower()
            if base in ("ffmpeg.exe", "ffprobe.exe"):
                with z.open(name) as src, open(dest / base, "wb") as out:
                    shutil.copyfileobj(src, out)
                got.append(base)
    return got


def _download(url: str, out_path: Path):
    req = urllib.request.Request(url, headers={"User-Agent": DEFAULT_UA})
    with urllib.request.urlopen(req, timeout=60) as r, open(out_path, "wb") as f:
        total, done = int(r.headers.get("Content-Length") or 0), 0
        while True:
            chunk = r.read(1 << 20)
            if not chunk:
                break
            f.write(chunk)
            done += len(chunk)
            _state["progress"] = int(100 * done / total) if total else 0


def _run():
    tmp = Path(tempfile.mkdtemp(prefix="mre_ff_"))
    last = ""
    try:
        for url in URLS:
            try:
                _state.update(message="Đang tải FFmpeg…", progress=0)
                z = tmp / "ffmpeg.zip"
                _download(url, z)
                if {"ffmpeg.exe", "ffprobe.exe"} <= set(extract_ffmpeg(z, data_dir() / "bin")):
                    _state.update(message="Đã cài FFmpeg", progress=100, error="")
                    return
                last = "gói tải về thiếu ffmpeg/ffprobe"
            except Exception as e:
                last = str(e)
        _state["error"] = (f"Không tải được FFmpeg ({last}). Hãy tải thủ công từ gyan.dev rồi chép ffmpeg.exe và "
                           f"ffprobe.exe vào {data_dir() / 'bin'}")
    finally:
        _state["installing"] = False
        shutil.rmtree(tmp, ignore_errors=True)


def start() -> bool:
    with _lock:
        if _state["installing"]:
            return False
        _state.update(installing=True, progress=0, error="", message="")
    threading.Thread(target=_run, daemon=True).start()
    return True
