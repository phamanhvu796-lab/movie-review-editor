"""Chế độ KỊCH BẢN -> VIDEO (tự tìm hình minh hoạ) + key kho ảnh/video miễn phí."""
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

from ..config import settings
from ..database import get_db
from ..models import SrtCue
from ..services import factcheck_service, stock_service, styles
from ..services.api_errors import friendly
from ..workers import broll_worker, pool
from .deps import ensure_idle, get_project

router = APIRouter(prefix="/api")


@router.get("/broll/styles")
def broll_styles():
    k = stock_service.keys()
    return {"styles": styles.listing(), "groups": styles.GROUPS, "media": styles.MEDIA_LABELS,
            "keys": {"pexels": bool(k["pexels"]), "pixabay": bool(k["pixabay"])}}


class KeysIn(BaseModel):
    pexels: str | None = None
    pixabay: str | None = None


@router.put("/broll/keys")
def broll_keys(body: KeysIn):
    kw = {}
    if body.pexels is not None:
        kw["stock_pexels_key"] = body.pexels.strip()
    if body.pixabay is not None:
        kw["stock_pixabay_key"] = body.pixabay.strip()
    settings.update(**kw)
    result = {}
    for src, val in (("pexels", body.pexels), ("pixabay", body.pixabay)):     # thử ngay key vừa nhập
        if val:
            try:
                fn = stock_service.search_pexels if src == "pexels" else stock_service.search_pixabay
                result[src] = {"ok": bool(fn("city", "photo", False, 3)), "detail": ""}
            except Exception as e:
                result[src] = {"ok": False, "detail": "Key không dùng được hoặc không có mạng: " + str(e)[:120]}
    return {"keys": {k: bool(v) for k, v in stock_service.keys().items()}, "test": result}


class BrollIn(BaseModel):
    style: str
    media: str | None = Field(None, pattern="^(photo|video|mix)$")
    topic: str = ""
    script: str | None = None
    target_seconds: int | None = Field(None, ge=15, le=3600)
    render: bool = True
    factcheck: bool = True               # có API DeepSeek -> kiểm tra & kiểm chứng kịch bản trước khi làm giọng


@router.post("/projects/{pid}/broll", status_code=202)
def start_broll(pid: str, body: BrollIn, db=Depends(get_db)):
    p = get_project(db, pid)
    ensure_idle(p)
    try:
        styles.get(body.style)
    except ValueError as e:
        raise HTTPException(400, str(e))
    if not settings.anthropic_api_key:
        raise HTTPException(400, "Cần kết nối Claude (⚙ Cài đặt) để AI chọn hình cho từng câu")
    p.status, p.error_message = "analyzing", None
    p.settings = {**(p.settings or {}), "auto_step": "Đang chuẩn bị…"}
    db.commit()
    pool.submit(broll_worker.run_broll, pid, body.model_dump())
    return {"status": "queued"}


@router.get("/projects/{pid}/broll/credits", response_class=PlainTextResponse)
def broll_credits(pid: str, db=Depends(get_db)):
    return (get_project(db, pid).settings or {}).get("stock_credits") or "Chưa có hình tải từ kho."


# ------------------------------------------------------------------ DeepSeek: kiểm tra & kiểm chứng kịch bản
class FactIn(BaseModel):
    script: str | None = Field(None, max_length=60000)   # trống = dùng kịch bản đã lưu / phụ đề của project
    style: str | None = None
    topic: str | None = None


@router.get("/factcheck/status")
def factcheck_status():
    return {"available": factcheck_service.available()}


@router.post("/projects/{pid}/factcheck")
def factcheck(pid: str, body: FactIn, db=Depends(get_db)):
    p = get_project(db, pid)
    if not factcheck_service.available():
        raise HTTPException(400, "Chưa gắn API DeepSeek: vào ⚙ Cài đặt → mục 🔎 DeepSeek, dán API key.")
    ps = p.settings or {}
    script, source = (body.script or "").strip(), "input"
    if not script:
        script, source = (ps.get("script") or "").strip(), "script"
    if not script:
        cues = db.query(SrtCue).filter_by(project_id=pid).order_by(SrtCue.cue_index).all()
        script, source = "\n".join(c.text for c in cues).strip(), "subtitles"
    if not script:
        raise HTTPException(400, "Chưa có kịch bản hoặc phụ đề để kiểm tra")
    group = None
    sid = body.style or (ps.get("broll") or {}).get("style")
    if sid:
        try:
            group = styles.get(sid).get("group")
        except ValueError:
            pass
    try:
        fc = factcheck_service.check_script(script, p.language or "vi", group, body.topic or (ps.get("broll") or {}).get("topic") or p.title)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(502, f"Gọi DeepSeek lỗi: {friendly(e)}")
    fc = {**fc, "source": source, "applied": False}
    p.settings = {**ps, "factcheck": fc}
    db.commit()
    return fc


@router.get("/projects/{pid}/factcheck")
def factcheck_last(pid: str, db=Depends(get_db)):
    return (get_project(db, pid).settings or {}).get("factcheck") or {}
