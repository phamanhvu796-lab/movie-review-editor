from fastapi import HTTPException

from ..models import Project


def get_project(db, pid: str) -> Project:
    p = db.get(Project, pid)
    if not p:
        raise HTTPException(404, "Không tìm thấy project")
    return p


def ensure_idle(p: Project):
    if p.status in ("analyzing", "rendering"):
        raise HTTPException(409, "Project đang xử lý, vui lòng đợi hoàn tất")
