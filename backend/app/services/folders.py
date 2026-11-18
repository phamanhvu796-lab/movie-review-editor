"""Thư mục ĐẦU VÀO (nơi để phim, voice, nhạc… -> mở sẵn khi chọn file, quét nhanh để thêm vào dự án)
và thư mục ĐẦU RA (nơi lưu video xuất bản chính thức). Đổi trong ⚙ Cài đặt."""
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from ..config import settings

MEDIA_EXT = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v", ".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg",
             ".png", ".jpg", ".jpeg", ".webp", ".cube", ".srt", ".vtt"}


def _videos_home() -> Path:
    v = Path.home() / "Videos"
    return v if v.exists() else Path.home()


def input_dir() -> Path:
    p = settings._load().get("input_dir")                     # noqa: SLF001
    return Path(p) if p else _videos_home()


def output_dir() -> Path:
    p = settings._load().get("output_dir")                    # noqa: SLF001
    d = Path(p) if p else _videos_home() / "MovieReviewEditor"
    d.mkdir(parents=True, exist_ok=True)
    return d


def set_dirs(input_path: str | None = None, output_path: str | None = None) -> dict:
    kw = {}
    for key, val in (("input_dir", input_path), ("output_dir", output_path)):
        if val is None:
            continue
        val = val.strip()
        if val:
            p = Path(val).expanduser()
            try:
                p.mkdir(parents=True, exist_ok=True)
            except OSError as e:
                raise ValueError(f"Không tạo/mở được thư mục {val}: {e}") from e
            if not os.access(p, os.W_OK if key == "output_dir" else os.R_OK):
                raise ValueError(f"Không có quyền {'ghi' if key == 'output_dir' else 'đọc'} thư mục {val}")
            val = str(p.resolve())
        kw[key] = val
    settings.update(**kw)
    return info()


def info() -> dict:
    return {"input_dir": str(input_dir()), "output_dir": str(output_dir())}


def safe_name(s: str) -> str:
    s = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', " ", s).strip(" .")
    return re.sub(r"\s+", " ", s)[:80] or "video"


AI_DIR = "Tài nguyên AI"
MUSIC_DIR = "Nhạc nền"


def ai_dir(project_title: str) -> Path:
    """Thư mục đầu vào / Tài nguyên AI / <tên dự án>: ảnh & video AI tự tìm cho dự án này."""
    d = input_dir() / AI_DIR / safe_name(project_title or "Dự án")
    d.mkdir(parents=True, exist_ok=True)
    return d


def music_dir() -> Path:
    """Thư mục đầu vào / Nhạc nền: kho nhạc dùng chung mọi dự án (bạn thả nhạc của mình vào, AI tải thêm vào /AI)."""
    d = input_dir() / MUSIC_DIR
    d.mkdir(parents=True, exist_ok=True)
    return d


def output_file(title: str, fmt: str, resolution: str) -> Path:
    stamp = datetime.now().strftime("%Y-%m-%d %H-%M")
    base = f"{safe_name(title)} - {fmt.replace(':', 'x')} {resolution} - {stamp}"
    d = output_dir()
    out, i = d / f"{base}.mp4", 2
    while out.exists():
        out, i = d / f"{base} ({i}).mp4", i + 1
    return out


def list_inputs(limit: int = 400) -> list[dict]:
    """File media trong thư mục đầu vào (và 1 cấp thư mục con), mới nhất trước."""
    root = input_dir()
    out = []
    if not root.exists():
        return out
    cands = list(root.iterdir())
    for d in [c for c in cands if c.is_dir() and not c.name.startswith(".")][:50]:
        try:
            cands += list(d.iterdir())
        except OSError:
            pass
    for f in cands:
        try:
            if f.is_file() and f.suffix.lower() in MEDIA_EXT:
                st = f.stat()
                out.append({"path": str(f), "name": f.name, "folder": str(f.parent.relative_to(root)) if f.parent != root else "",
                            "size": st.st_size, "mtime": st.st_mtime, "ext": f.suffix.lower()})
        except OSError:
            continue
    out.sort(key=lambda x: -x["mtime"])
    return out[:limit]


def open_in_explorer(path: str | Path) -> None:
    p = Path(path)
    if not p.exists():
        raise ValueError("Thư mục/file không còn tồn tại")
    if os.name == "nt":
        if p.is_file():
            subprocess.Popen(["explorer", "/select,", str(p)])
        else:
            os.startfile(str(p))                                  # noqa: S606
    elif sys.platform == "darwin":
        subprocess.Popen(["open", "-R" if p.is_file() else "", str(p)])
    else:
        subprocess.Popen(["xdg-open", str(p.parent if p.is_file() else p)])
