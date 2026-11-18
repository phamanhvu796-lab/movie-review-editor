from ..database import SessionLocal
from ..models import EditPlanRow, Project
from ..services.api_errors import friendly
from ..services.claude_service import generate_edit_plan
from ..services.context_service import build_context


def generate_plan(project_id: str, allow_spoilers: bool | None, style: str | None):
    with SessionLocal() as db:
        p = db.get(Project, project_id)
        try:
            res = generate_edit_plan(build_context(db, p, allow_spoilers, style))
            version = (db.query(EditPlanRow).filter_by(project_id=project_id).count()) + 1
            ok = res.status == "ok"
            raw = res.plan.model_dump() if ok else (res.raw_plan or {})
            db.add(EditPlanRow(project_id=project_id, version=version, plan=raw,
                               validation_status="valid" if ok else "needs_manual_review",
                               validation_errors=[str(i) for i in res.issues]))
            p.status = "plan_ready" if ok else "draft"
            p.error_message = None if ok else "Plan còn lỗi, cần chỉnh tay hoặc tạo lại (xem danh sách lỗi)"
            db.commit()
            if ok:                       # chat AI nhờ "làm lại" -> tự dựng lại luôn ở nền
                from .render_worker import rerender_if_pending
                rerender_if_pending(db, p)
        except Exception as e:
            p.status, p.error_message = "failed", friendly(e)[:1000]
            db.commit()
            raise
