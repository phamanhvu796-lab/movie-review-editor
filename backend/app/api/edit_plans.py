import threading
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from ..config import settings
from ..database import get_db
from ..models import Asset, EditPlanRow, to_dict
from ..services.context_service import build_context
from ..services.ffmpeg_service import extract_frame
from ..services.render_service import PRESETS
from ..services.repair_service import repair_plan
from ..services.validator_service import validate_plan
from ..utils.paths import data_dir
from ..workers import plan_worker, pool
from .deps import ensure_idle, get_project

router = APIRouter(prefix="/api")
_FRAME_SEM = threading.Semaphore(3)   # trình duyệt có thể xin hàng trăm ảnh cùng lúc


class PlanGenIn(BaseModel):
    allow_spoilers: bool | None = None
    style: str | None = None


class PlanPatch(BaseModel):
    plan: dict


def _plan_or_404(db, pid, plan_id):
    row = db.get(EditPlanRow, plan_id)
    if not row or row.project_id != pid:
        raise HTTPException(404, "Không tìm thấy edit plan")
    return row


def _validate_row(db, project, row):
    try:
        ctx = build_context(db, project)
    except ValueError as e:
        raise HTTPException(400, str(e))
    _, issues = validate_plan(row.plan, ctx)
    row.validation_status = "invalid" if issues else "valid"
    row.validation_errors = [str(i) for i in issues]
    db.commit()
    return issues


@router.post("/projects/{pid}/edit-plan/generate", status_code=202)
def generate(pid: str, body: PlanGenIn = PlanGenIn(), db=Depends(get_db)):
    p = get_project(db, pid)
    ensure_idle(p)
    if not settings.anthropic_api_key:
        raise HTTPException(400, "Chưa nhập Anthropic API key (nút Cài đặt)")
    try:
        build_context(db, p, body.allow_spoilers, body.style)
    except ValueError as e:
        raise HTTPException(400, str(e))
    p.settings = {**(p.settings or {}), **{k: v for k, v in body.model_dump().items() if v is not None}}
    p.status = "analyzing"
    db.commit()
    pool.submit(plan_worker.generate_plan, pid, body.allow_spoilers, body.style)
    return {"status": "queued"}


@router.get("/projects/{pid}/edit-plans")
def list_plans(pid: str, db=Depends(get_db)):
    get_project(db, pid)
    rows = db.query(EditPlanRow).filter_by(project_id=pid).order_by(EditPlanRow.version.desc())
    return [to_dict(r, exclude=("plan",)) for r in rows]


@router.get("/projects/{pid}/edit-plan/latest")
def latest_plan(pid: str, db=Depends(get_db)):
    row = db.query(EditPlanRow).filter_by(project_id=pid).order_by(EditPlanRow.version.desc()).first()
    if not row:
        raise HTTPException(404, "Chưa có edit plan")
    return to_dict(row)


@router.get("/projects/{pid}/edit-plan/{plan_id}")
def read_plan(pid: str, plan_id: str, db=Depends(get_db)):
    return to_dict(_plan_or_404(db, pid, plan_id))


@router.patch("/projects/{pid}/edit-plan/{plan_id}")
def patch_plan(pid: str, plan_id: str, body: PlanPatch, db=Depends(get_db)):
    """Sửa tay = tạo phiên bản mới (giữ lịch sử), rồi validate lại."""
    p = get_project(db, pid)
    _plan_or_404(db, pid, plan_id)
    version = db.query(EditPlanRow).filter_by(project_id=pid).count() + 1
    try:
        ctx = build_context(db, p)
    except ValueError as e:
        raise HTTPException(400, str(e))
    fixed, notes = repair_plan(body.plan, ctx)   # kéo-thả/đổi cảnh xong tự co giãn cho khớp lời
    row = EditPlanRow(project_id=pid, version=version, plan=fixed)
    db.add(row)
    db.flush()
    _validate_row(db, p, row)
    if row.validation_status == "valid":
        p.status = "plan_ready"
        db.commit()
    return {**to_dict(row), "repairs": notes}


@router.post("/projects/{pid}/edit-plan/{plan_id}/validate")
def validate(pid: str, plan_id: str, db=Depends(get_db)):
    p = get_project(db, pid)
    row = _plan_or_404(db, pid, plan_id)
    issues = _validate_row(db, p, row)
    return {"valid": not issues, "issues": [str(i) for i in issues]}


@router.post("/projects/{pid}/edit-plan/{plan_id}/restore")
def restore(pid: str, plan_id: str, db=Depends(get_db)):
    """Khôi phục phiên bản cũ: sao chép thành phiên bản mới nhất."""
    old = _plan_or_404(db, pid, plan_id)
    row = EditPlanRow(project_id=pid, version=db.query(EditPlanRow).filter_by(project_id=pid).count() + 1,
                      plan=old.plan, validation_status=old.validation_status, validation_errors=old.validation_errors)
    db.add(row)
    db.commit()
    return to_dict(row)


@router.get("/projects/{pid}/filters")
def filters(pid: str, db=Depends(get_db)):
    luts = [Path(a.file_name).stem for a in db.query(Asset).filter_by(project_id=pid, asset_type="lut")]
    return list(PRESETS) + luts


@router.get("/projects/{pid}/frame")
def frame(pid: str, footage: str, t: float, db=Depends(get_db)):
    """Ảnh storyboard: 1 frame tại thời điểm t của footage."""
    a = db.query(Asset).filter_by(project_id=pid, file_name=footage).first()
    if not a:
        raise HTTPException(404, "Không tìm thấy footage")
    cache = data_dir() / "cache" / "frames"
    cache.mkdir(parents=True, exist_ok=True)
    out = cache / f"{a.id}_{int(t * 10)}.jpg"
    if not out.exists():
        with _FRAME_SEM:
            if not out.exists():
                try:
                    extract_frame(a.file_path, t, out)
                except RuntimeError as e:
                    raise HTTPException(500, str(e))
    return FileResponse(out, media_type="image/jpeg", headers={"Cache-Control": "public, max-age=86400"})
