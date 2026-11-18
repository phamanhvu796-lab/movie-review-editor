"""⚡ Tự động tạo video — MỘT nút cho mọi trường hợp:
- Có footage phim  -> phân tích cảnh -> (vá kịch bản/giọng/phụ đề nếu thiếu) -> Claude lên bản dựng -> xuất video.
- Không có footage -> chế độ Kịch bản → Video: từ kịch bản / SRT / voice MP3, tự tìm ảnh-video miễn phí bản quyền,
  dựng khớp giọng đọc -> xuất video.
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from ..config import settings
from ..database import get_db
from ..models import Asset, SrtCue
from ..utils.paths import find_binary
from ..workers import autofill_worker, pool
from .deps import ensure_idle, get_project

router = APIRouter(prefix="/api")


class AutoIn(BaseModel):
    tag_scenes: bool = False
    render: bool = True
    quality: str = Field("final", pattern="^(preview|final)$")   # final = thành phẩm 1080p vào thư mục đầu ra
    allow_spoilers: bool | None = None
    style: str | None = None


@router.post("/projects/{pid}/auto", status_code=202)
def auto(pid: str, body: AutoIn = AutoIn(), db=Depends(get_db)):
    p = get_project(db, pid)
    ensure_idle(p)
    if not settings.anthropic_api_key:
        raise HTTPException(400, "Chưa kết nối Claude: vào ⚙ Cài đặt và dán API key")
    if body.render and not find_binary("ffmpeg"):
        raise HTTPException(400, "Thiếu FFmpeg (đặt ffmpeg.exe vào thư mục bin/)")
    assets = db.query(Asset).filter(Asset.project_id == pid, Asset.processing_status != "failed").all()
    has_voice = any(a.asset_type == "voice" for a in assets)
    has_cues = db.query(SrtCue).filter_by(project_id=pid).count() > 0
    has_script = bool(((p.settings or {}).get("script") or "").strip())
    topic = ((p.settings or {}).get("broll") or {}).get("topic")
    has_footage = any(a.asset_type == "footage" for a in assets)
    if not (has_voice or has_cues or has_script or topic or has_footage):
        raise HTTPException(400, "Chưa có gì để dựng: thêm voice-over (MP3), nạp SRT ở mục Kịch bản, "
                                 "hoặc dán kịch bản / chủ đề ở mục Kịch bản → Video")
    if not has_voice and not autofill_worker._has_tts() and not has_footage:
        raise HTTPException(400, "Cần giọng đọc: tải file voice-over (MP3/WAV) lên, hoặc thêm key ElevenLabs/OpenAI TTS ở Cài đặt")
    p.status, p.error_message = "analyzing", None
    p.settings = {**(p.settings or {}), "auto_step": "Đang kiểm tra tài nguyên…"}
    db.commit()
    pool.submit(autofill_worker.run_autofill, pid, {"replan": True, "full": True, "render": body.quality if body.render else None,
                                                   "tag_scenes": body.tag_scenes, "allow_spoilers": body.allow_spoilers,
                                                   "style": body.style})
    return {"status": "queued"}
