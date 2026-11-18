"""Chế độ KỊCH BẢN -> VIDEO (tự tìm hình minh hoạ) + key kho ảnh/video miễn phí."""
import shutil
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

from ..config import settings
from ..database import get_db
from ..models import SrtCue
from ..services import factcheck_service, stock_service, styles
from ..services.api_errors import friendly
from ..workers import broll_worker, pool
from ..utils.paths import data_dir
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
    ps = get_project(db, pid).settings or {}
    text = ps.get("stock_credits") or "Chưa có hình tải từ kho."
    return text + ("\n\n" + ps["music_credit"] if ps.get("music_credit") else "")


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


# ------------------------------------------------------------------ hình còn thiếu: cần tìm / cần vẽ (gắn file vào từng câu)
class FillPath(BaseModel):
    path: str = Field(min_length=3, max_length=1000)


class ReqPatch(BaseModel):
    prompt: str | None = Field(None, max_length=4000)
    what: str | None = Field(None, max_length=1000)
    status: str | None = Field(None, pattern="^(open|filled|skipped)$")


@router.get("/projects/{pid}/media-requests")
def media_requests(pid: str, db=Depends(get_db)):
    return (get_project(db, pid).settings or {}).get("media_requests") or {"items": []}


def _after_fill(db, pid: str, res: dict) -> dict:
    from ..models import to_dict
    from ..workers.render_worker import rerender_soon
    p = get_project(db, pid)
    res["render"] = rerender_soon(db, p) if res["valid"] else "none"
    db.refresh(p)
    return {**res, "project": to_dict(p)}


@router.post("/projects/{pid}/media-requests/{cue}/fill")
def media_fill(pid: str, cue: int, body: FillPath, db=Depends(get_db)):
    from ..workers import media_worker
    get_project(db, pid)
    if not Path(body.path).is_file():
        raise HTTPException(400, "Không thấy file")
    try:
        res = media_worker.fill(pid, cue, body.path)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return _after_fill(db, pid, res)


@router.post("/projects/{pid}/media-requests/{cue}/upload")
def media_upload(pid: str, cue: int, file: UploadFile = File(...), db=Depends(get_db)):
    from ..workers import media_worker
    get_project(db, pid)
    ext = Path(file.filename or "").suffix.lower()
    if ext not in media_worker.IMAGE_EXT | media_worker.VIDEO_EXT:
        raise HTTPException(400, "Chỉ nhận ảnh (jpg, png, webp) hoặc video (mp4, mov, mkv, webm)")
    tmp = data_dir() / "projects" / pid / "uploads"
    tmp.mkdir(parents=True, exist_ok=True)
    dest = tmp / Path(file.filename).name
    with open(dest, "wb") as f:
        shutil.copyfileobj(file.file, f, 1024 * 1024)
    try:
        res = media_worker.fill(pid, cue, str(dest))
    except ValueError as e:
        raise HTTPException(400, str(e))
    finally:
        dest.unlink(missing_ok=True)
    return _after_fill(db, pid, res)


@router.patch("/projects/{pid}/media-requests/{cue}")
def media_patch(pid: str, cue: int, body: ReqPatch, db=Depends(get_db)):
    from ..workers import media_worker
    get_project(db, pid)
    hit = media_worker.update_request(pid, cue, **body.model_dump())
    if hit is None:
        raise HTTPException(404, "Không có yêu cầu hình cho câu này")
    return hit
