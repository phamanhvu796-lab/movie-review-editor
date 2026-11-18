"""Lịch sử khung chat AI của dự án (lưu trong project.settings["chat"]).

Cả API chat lẫn các tiến trình chạy nền (tự động edit, xuất video) đều ghi vào đây, nên mọi thao tác ghi đi qua
một khoá chung và luôn đọc lại bản mới nhất trước khi thêm — không lượt nào ghi đè mất tin nhắn của lượt khác.
Tin nhắn: {role: user|assistant, text, at, kind?: progress|summary|render|error, ...}
"""
import threading
from datetime import datetime, timezone

MAX_HISTORY = 40
_lock = threading.Lock()


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _session():
    from ..database import SessionLocal
    return SessionLocal()


def _write(pid: str, fn) -> list[dict]:
    from ..models import Project
    with _lock, _session() as db:
        p = db.get(Project, pid)
        if p is None:
            return []
        hist = list((p.settings or {}).get("chat") or [])
        hist = fn(hist)[-MAX_HISTORY:]
        p.settings = {**(p.settings or {}), "chat": hist}
        db.commit()
        return hist


def append(pid: str, *msgs: dict) -> list[dict]:
    stamp = now()
    return _write(pid, lambda h: [m for m in h if m.get("kind") != "progress"] + [{"at": stamp, **m} for m in msgs])


def progress(pid: str, text: str) -> None:
    """Dòng "Claude đang làm…" — cập nhật tại chỗ, không chèn thêm tin nhắn mới mỗi bước."""
    def fn(h):
        steps = []
        if h and h[-1].get("kind") == "progress":
            steps = list(h[-1].get("steps") or [])
            h = h[:-1]
        if not steps or steps[-1] != text:
            steps.append(text)
        return h + [{"role": "assistant", "kind": "progress", "text": text, "steps": steps[-12:], "at": now()}]
    _write(pid, fn)


def clear_progress(pid: str) -> None:
    _write(pid, lambda h: [m for m in h if m.get("kind") != "progress"])


def last_steps(pid: str) -> list[str]:
    from ..models import Project
    with _session() as db:
        p = db.get(Project, pid)
        h = list(((p.settings or {}) if p else {}).get("chat") or [])
    return list(h[-1].get("steps") or []) if h and h[-1].get("kind") == "progress" else []


def update_settings(pid: str, patch: dict, drop: tuple = ()) -> dict:
    """Sửa vài khoá trong project.settings trên bản MỚI NHẤT (không ghi đè tin nhắn chat do tiến trình khác vừa thêm)."""
    from ..models import Project
    with _lock, _session() as db:
        p = db.get(Project, pid)
        st = {**(p.settings or {}), **patch}
        for k in drop:
            st.pop(k, None)
        p.settings = st
        db.commit()
        return st
