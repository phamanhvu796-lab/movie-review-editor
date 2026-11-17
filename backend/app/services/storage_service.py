"""Quản lý dung lượng ổ đĩa của ứng dụng: đo, giới hạn cache, dọn dẹp."""
import os
import shutil
from pathlib import Path

from ..config import settings
from ..utils.paths import data_dir

GB = 1024 ** 3
MB = 1024 ** 2
DEFAULT_LIMITS = {"segments": 2 * GB, "tts": 512 * MB, "frames": 256 * MB}


def limits() -> dict:
    d = settings._load()                                   # noqa: SLF001
    try:
        seg = float(d.get("cache_limit_gb", DEFAULT_LIMITS["segments"] / GB)) * GB
    except (TypeError, ValueError):
        seg = DEFAULT_LIMITS["segments"]
    return {"segments": int(seg), "tts": DEFAULT_LIMITS["tts"], "frames": DEFAULT_LIMITS["frames"]}


def dir_size(p: Path) -> int:
    total = 0
    if not p.exists():
        return 0
    for root, _, files in os.walk(p):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


def prune(cache_dir: Path, max_bytes: int, pattern: str = "*") -> int:
    """Xoá file cũ nhất (theo lần dùng gần nhất) tới khi dưới giới hạn. Trả số byte đã xoá."""
    files = []
    for f in Path(cache_dir).glob(pattern):
        try:
            st = f.stat()
            files.append((max(st.st_atime, st.st_mtime), st.st_size, f))
        except OSError:
            pass
    files.sort()
    total, freed = sum(s for _, s, _ in files), 0
    for _, size, f in files:
        if total <= max_bytes:
            break
        try:
            f.unlink()
            total -= size
            freed += size
        except OSError:
            pass
    return freed


def prune_all() -> int:
    c, lim = data_dir() / "cache", limits()
    return (prune(c / "segments", lim["segments"], "*.mp4") + prune(c / "tts", lim["tts"], "*.mp3")
            + prune(c / "frames", lim["frames"], "*.jpg"))


def is_inside_data(path: str | Path) -> bool:
    try:
        Path(path).resolve().relative_to(data_dir().resolve())
        return True
    except (ValueError, OSError):
        return False


def usage(db) -> dict:
    from ..models import Asset
    d = data_dir()
    linked = sum((a.size_bytes or 0) for a in db.query(Asset).all() if (a.meta or {}).get("linked"))
    parts = [
        ("assets", "Tài nguyên đã sao chép vào app", sum(dir_size(p / "assets") for p in (d / "projects").glob("*"))),
        ("renders", "Video đã xuất", sum(dir_size(p / "renders") for p in (d / "projects").glob("*"))),
        ("segments", "Bộ nhớ đệm dựng video", dir_size(d / "cache" / "segments")),
        ("tts", "Bộ nhớ đệm giọng đọc", dir_size(d / "cache" / "tts")),
        ("frames", "Ảnh xem trước", dir_size(d / "cache" / "frames")),
        ("work", "File tạm", dir_size(d / "work")),
        ("other", "Nhật ký, cửa sổ ứng dụng, khác", dir_size(d / "logs") + dir_size(d / "webview") + dir_size(d / "edge_app_profile")),
    ]
    total = sum(p[2] for p in parts)
    try:
        free = shutil.disk_usage(d).free
    except OSError:
        free = None
    return {"total": total, "free": free, "linked_not_copied": linked, "data_dir": str(d),
            "parts": [{"key": k, "label": lb, "bytes": b} for k, lb, b in parts],
            "cache_limit_gb": limits()["segments"] / GB}


def clean(db, targets: list[str]) -> int:
    """Dọn theo mục. 'previews': xoá các bản xem thử cũ (giữ bản mới nhất mỗi dự án). 'orphans': file không còn dùng."""
    from ..models import Asset, Render
    d, freed = data_dir(), 0
    if "segments" in targets:
        freed += prune(d / "cache" / "segments", 0, "*.mp4")
    if "tts" in targets:
        freed += prune(d / "cache" / "tts", 0, "*.mp3")
    if "frames" in targets:
        freed += prune(d / "cache" / "frames", 0, "*.jpg")
    if "work" in targets:
        freed += dir_size(d / "work")
        shutil.rmtree(d / "work", ignore_errors=True)
    if "previews" in targets:
        freed += prune_old_previews(db, keep=1)
    if "orphans" in targets:
        used = {str(Path(a.file_path).resolve()) for a in db.query(Asset).all() if a.file_path}
        used |= {str(Path(r.output_path).resolve()) for r in db.query(Render).all() if r.output_path}
        used |= {str(Path(r.output_path).with_suffix(".srt").resolve()) for r in db.query(Render).all() if r.output_path}
        for sub in ("assets", "renders"):
            for f in (d / "projects").glob(f"*/{sub}/*"):
                if f.is_file() and str(f.resolve()) not in used:
                    try:
                        freed += f.stat().st_size
                        f.unlink()
                    except OSError:
                        pass
    return freed


def prune_old_previews(db, keep: int = 1, project_id: str | None = None) -> int:
    """Bản xem thử (preview 720p) chỉ để xem nhanh: giữ `keep` bản mới nhất mỗi dự án, xoá file + bản ghi cũ."""
    from ..models import Render
    q = db.query(Render).filter(Render.status.in_(["completed", "failed", "cancelled"]))
    if project_id:
        q = q.filter(Render.project_id == project_id)
    by_proj: dict[str, list] = {}
    for r in q.order_by(Render.created_at.desc()):
        if (r.options or {}).get("quality", "preview") == "preview" or r.status != "completed":
            by_proj.setdefault(r.project_id, []).append(r)
    freed = 0
    for rows in by_proj.values():
        done = [r for r in rows if r.status == "completed"]
        drop = done[keep:] + [r for r in rows if r.status != "completed"][3:]      # giữ vài bản lỗi gần nhất để xem lý do
        for r in drop:
            for f in ([Path(r.output_path), Path(r.output_path).with_suffix(".srt")] if r.output_path else []):
                try:
                    freed += f.stat().st_size
                    f.unlink()
                except OSError:
                    pass
            db.delete(r)
    db.commit()
    return freed
