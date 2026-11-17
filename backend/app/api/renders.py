from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse

from ..database import get_db
from ..models import Asset, EditPlanRow, Render, to_dict
from ..services.render_service import RES, RenderOptions
from ..utils.rangefile import ranged_file
from ..workers import pool, render_worker
from ..workers.pool import CANCELLED
from .deps import ensure_idle, get_project

router = APIRouter(prefix="/api")


class RenderIn(RenderOptions):
    edit_plan_id: str | None = None


@router.post("/projects/{pid}/renders", status_code=202)
def create_render(pid: str, body: RenderIn, db=Depends(get_db)):
    p = get_project(db, pid)
    ensure_idle(p)
    q = db.query(EditPlanRow).filter_by(project_id=pid)
    plan = db.get(EditPlanRow, body.edit_plan_id) if body.edit_plan_id else q.order_by(EditPlanRow.version.desc()).first()
    if not plan or plan.project_id != pid:
        raise HTTPException(400, "Chưa có edit plan")
    if plan.validation_status != "valid":
        raise HTTPException(400, "Edit plan chưa hợp lệ, hãy sửa và validate trước khi render")
    if not db.query(Asset).filter_by(project_id=pid, asset_type="voice").count():
        raise HTTPException(400, "Chưa upload voice-over")

    opts = RenderOptions(**body.model_dump(exclude={"edit_plan_id"}))
    W, H = RES[(opts.aspect_ratio, opts.quality)]
    r = Render(project_id=pid, edit_plan_id=plan.id, format=opts.aspect_ratio, resolution=f"{W}x{H}",
               options=opts.model_dump())
    db.add(r)
    p.status = "rendering"
    db.commit()
    pool.submit_render(render_worker.run_render, r.id)
    return to_dict(r)


@router.get("/projects/{pid}/renders")
def list_renders(pid: str, db=Depends(get_db)):
    get_project(db, pid)
    return [to_dict(r) for r in db.query(Render).filter_by(project_id=pid).order_by(Render.created_at.desc())]


def _render(db, rid):
    r = db.get(Render, rid)
    if not r:
        raise HTTPException(404, "Không tìm thấy render")
    return r


@router.get("/renders/{rid}")
def read_render(rid: str, db=Depends(get_db)):
    return to_dict(_render(db, rid))


@router.post("/renders/{rid}/cancel")
def cancel(rid: str, db=Depends(get_db)):
    r = _render(db, rid)
    if r.status == "queued":
        r.status = "cancelled"
        p = get_project(db, r.project_id)
        p.status = "plan_ready"
        db.commit()
    elif r.status == "running":
        CANCELLED.add(rid)          # worker sẽ dừng ffmpeg trong ~1 giây
    else:
        raise HTTPException(409, "Render không ở trạng thái huỷ được")
    return {"status": "cancelling"}


@router.get("/renders/{rid}/status")
def render_status(rid: str, db=Depends(get_db)):
    r = _render(db, rid)
    return {"status": r.status, "progress": r.progress, "error_message": r.error_message}


@router.get("/renders/{rid}/download")
def download(rid: str, request: Request, kind: str = "video", inline: bool = False, db=Depends(get_db)):
    r = _render(db, rid)
    if r.status != "completed" or not r.output_path:
        raise HTTPException(409, "Render chưa hoàn tất")
    from pathlib import Path
    out = Path(r.output_path)
    if kind == "video" and inline:
        return ranged_file(request, str(out), "video/mp4")
    if kind == "video":
        return FileResponse(out, media_type="video/mp4", filename=f"review_{r.format.replace(':', 'x')}_{rid[:6]}.mp4")
    if kind == "srt":
        return FileResponse(out.with_suffix(".srt"), media_type="text/plain", filename=f"review_{rid[:6]}.srt")
    if kind == "plan":
        return JSONResponse(db.get(EditPlanRow, r.edit_plan_id).plan,
                            headers={"Content-Disposition": f'attachment; filename="edit_plan_{rid[:6]}.json"'})
    raise HTTPException(400, "kind phải là video | srt | plan")
