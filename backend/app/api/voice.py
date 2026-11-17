from typing import Literal

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel, Field

from ..config import settings
from ..database import get_db
from ..models import Asset, to_dict
from ..services import metadata_service, tts_service
from ..services.api_errors import friendly
from ..utils.paths import find_binary
from ..workers import pool, tts_worker
from .deps import ensure_idle, get_project
from .projects import upload_asset

router = APIRouter(prefix="/api")


class VoiceGenIn(BaseModel):
    text: str = Field(min_length=1)
    provider: Literal["elevenlabs", "openai"] = "elevenlabs"
    voice_id: str = ""          # OpenAI: để trống = 'alloy'; ElevenLabs: bắt buộc chọn giọng
    style: Literal["narrative", "dramatic", "calm"] = "narrative"
    speed: float = Field(1.0, ge=0.25, le=4.0)
    quality: Literal["high", "fast"] = "high"     # fast = ElevenLabs flash: nhanh, ~50% credit
    set_cues: bool = True        # tạo luôn phụ đề từ kịch bản (thời lượng thật của audio)


@router.get("/tts/voices")
def voices(provider: Literal["elevenlabs", "openai"] = "elevenlabs"):
    try:
        return tts_service.list_voices(provider)
    except RuntimeError as e:
        raise HTTPException(400, str(e))


class VoiceTestIn(BaseModel):
    provider: Literal["elevenlabs", "openai"] = "elevenlabs"
    voice_id: str = ""
    style: Literal["narrative", "dramatic", "calm"] = "narrative"
    quality: Literal["high", "fast"] = "high"


@router.post("/tts/test")
def test_voice(body: VoiceTestIn):
    """Kiểm tra giọng đọc đầu-cuối bằng một câu ngắn và trả về từng bước (nguồn, FFmpeg, âm thanh, giải mã)."""
    return tts_service.diagnose(body.provider, body.voice_id.strip(), body.style, body.quality)


@router.get("/tts/quota")
def quota():
    try:
        return tts_service.get_quota()
    except RuntimeError as e:
        raise HTTPException(400, str(e))


@router.post("/projects/{pid}/voice/generate", status_code=202)
def generate(pid: str, body: VoiceGenIn, db=Depends(get_db)):
    p = get_project(db, pid)
    ensure_idle(p)
    key = settings.elevenlabs_api_key if body.provider == "elevenlabs" else settings.openai_api_key
    if not key:
        raise HTTPException(400, f"Chưa nhập API key cho {body.provider} (nút Cài đặt)")
    if body.provider == "elevenlabs" and not body.voice_id.strip():
        raise HTTPException(400, "Chưa chọn giọng: bấm 'Tải danh sách giọng' rồi chọn một giọng.")
    if not (find_binary("ffmpeg") and find_binary("ffprobe")):
        raise HTTPException(400, "Thiếu FFmpeg (cần để xử lý giọng đọc). Bấm 'Tải FFmpeg tự động' ở mục Giọng AI hoặc Cài đặt.")
    p.status = "analyzing"
    db.commit()
    pool.submit(tts_worker.generate_voice, pid, body.text, body.provider, body.voice_id.strip() or "alloy",
                body.style, body.speed, body.set_cues, body.quality)
    return {"status": "queued"}


@router.get("/projects/{pid}/voice/status")
def voice_status(pid: str, db=Depends(get_db)):
    p = get_project(db, pid)
    a = db.query(Asset).filter_by(project_id=pid, asset_type="voice").order_by(Asset.created_at.desc()).first()
    return {"project_status": p.status, "error": p.error_message,
            "voice": {k: v for k, v in to_dict(a).items() if k != "meta"} if a else None}


@router.post("/projects/{pid}/voice/upload")
def voice_upload(pid: str, file: UploadFile = File(...), db=Depends(get_db)):
    return upload_asset(pid, "voice", "", file, db)


@router.post("/projects/{pid}/metadata/generate")
def metadata(pid: str, db=Depends(get_db)):
    """Tiêu đề / mô tả / hashtag / chữ thumbnail từ kịch bản (đồng bộ, vài giây)."""
    from ..models import EditPlanRow, SrtCue
    p = get_project(db, pid)
    if not settings.anthropic_api_key:
        raise HTTPException(400, "Chưa nhập Anthropic API key")
    cues = db.query(SrtCue).filter_by(project_id=pid).order_by(SrtCue.cue_index).all()
    if not cues:
        raise HTTPException(400, "Chưa có kịch bản/SRT")
    plan = db.query(EditPlanRow).filter_by(project_id=pid).order_by(EditPlanRow.version.desc()).first()
    try:
        result = metadata_service.generate_metadata(
            p.movie_title or p.title, p.genre or "", p.language or "vi",
            (plan.plan.get("creative_direction", "") if plan else ""), " ".join(c.text for c in cues),
            bool((p.settings or {}).get("allow_spoilers")))
    except Exception as e:
        raise HTTPException(502, f"Gọi Claude lỗi: {friendly(e)}")
    p.settings = {**(p.settings or {}), "metadata": result}
    db.commit()
    return result
