from ..database import SessionLocal
from ..models import EditPlanRow, Project, Render, SrtCue
from ..services.render_service import RES, RenderOptions
from . import analyze_worker, plan_worker
from .pool import submit_render
from .render_worker import run_render


def _set(project_id: str, step: str | None = None, status: str | None = None):
    with SessionLocal() as db:
        p = db.get(Project, project_id)
        if step is not None:
            p.settings = {**(p.settings or {}), "auto_step": step}
        if status:
            p.status = status
        db.commit()


def run_auto(project_id: str, o: dict):
    """Một chạm: phân tích cảnh -> (Whisper nếu chưa có phụ đề) -> Claude lên bản dựng -> dựng video xem thử.
    Mỗi bước tự đặt/trả trạng thái project; lỗi ở bước nào thì dừng ở đó với thông báo rõ."""
    try:
        _set(project_id, "Đang phân tích cảnh phim…")
        analyze_worker.analyze_project(project_id, bool(o.get("tag_scenes")))
        with SessionLocal() as db:
            has_cues = db.query(SrtCue).filter_by(project_id=project_id).count() > 0
        if not has_cues:
            _set(project_id, "Đang tạo phụ đề từ giọng đọc…", "analyzing")
            analyze_worker.transcribe_project(project_id, None)
        _set(project_id, "Claude đang lên bản dựng…", "analyzing")
        plan_worker.generate_plan(project_id, o.get("allow_spoilers"), o.get("style"))
        with SessionLocal() as db:
            p = db.get(Project, project_id)
            plan = db.query(EditPlanRow).filter_by(project_id=project_id).order_by(EditPlanRow.version.desc()).first()
            if not plan or plan.validation_status != "valid":
                _set(project_id, "Bản dựng cần chỉnh tay (xem thông báo lỗi ở tab Bản dựng)")
                return
            if o.get("render", True):
                saved = (p.settings or {}).get("render_opts", {})
                opts = RenderOptions(**{k: v for k, v in saved.items() if k in RenderOptions.model_fields})
                opts = opts.model_copy(update={"quality": "preview"})
                W, H = RES[(opts.aspect_ratio, opts.quality)]
                r = Render(project_id=project_id, edit_plan_id=plan.id, format=opts.aspect_ratio,
                           resolution=f"{W}x{H}", options=opts.model_dump())
                db.add(r)
                p.status = "rendering"
                p.settings = {**(p.settings or {}), "auto_step": "Đang dựng video xem thử…"}
                db.commit()
                submit_render(run_render, r.id)
                return
        _set(project_id, "")
    except Exception:
        _set(project_id, "")
        raise
