import importlib.util

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ..config import settings
from ..database import get_db
from ..models import Asset, SrtCue
from ..utils.paths import find_binary
from ..workers import auto_worker, pool
from .deps import ensure_idle, get_project

router = APIRouter(prefix="/api")


class AutoIn(BaseModel):
    tag_scenes: bool = False
    render: bool = True
    allow_spoilers: bool | None = None
    style: str | None = None


@router.post("/projects/{pid}/auto", status_code=202)
def auto(pid: str, body: AutoIn = AutoIn(), db=Depends(get_db)):
    p = get_project(db, pid)
    ensure_idle(p)
    if not settings.anthropic_api_key:
        raise HTTPException(400, "Chưa kết nối Claude: vào Cài đặt và nhập API key")
    if not find_binary("ffmpeg"):
        raise HTTPException(400, "Thiếu FFmpeg (đặt ffmpeg.exe vào thư mục bin/)")
    if not db.query(Asset).filter(Asset.project_id == pid, Asset.asset_type.in_(["footage", "broll"])).count():
        raise HTTPException(400, "Hãy thêm footage phim ở mục Tài nguyên")
    if not db.query(Asset).filter_by(project_id=pid, asset_type="voice").count():
        raise HTTPException(400, "Hãy thêm voice-over (tải lên hoặc tạo ở mục Giọng AI)")
    if not db.query(SrtCue).filter_by(project_id=pid).count() and importlib.util.find_spec("faster_whisper") is None:
        raise HTTPException(400, "Chưa có kịch bản/SRT. Hãy nạp SRT ở mục Kịch bản (hoặc cài Whisper để tự tạo)")
    p.status, p.error_message = "analyzing", None
    db.commit()
    pool.submit(auto_worker.run_auto, pid, body.model_dump())
    return {"status": "queued"}
