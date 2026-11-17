"""Trợ lý AI (chat sửa bản dựng), tự bổ sung tài nguyên, tín dụng API, dung lượng ổ đĩa."""
import threading
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from ..config import settings
from ..database import get_db
from ..models import EditPlanRow, Render, SrtCue, to_dict
from ..services import chat_service, credit_service, storage_service, usage_service
from ..services.api_errors import friendly
from ..services.context_service import build_context
from ..services.render_service import RES, RenderOptions
from ..services.validator_service import validate_plan
from ..utils.paths import find_binary
from ..workers import autofill_worker, plan_worker, pool
from ..workers.pool import submit_render
from ..workers.render_worker import run_render
from .deps import ensure_idle, get_project

router = APIRouter(prefix="/api")
_chat_locks: dict[str, threading.Lock] = {}
MAX_HISTORY = 40


# ------------------------------------------------------------------ tín dụng API
@router.get("/credits")
def credits(refresh: bool = False):
    return credit_service.summary(refresh)


class ConnId(BaseModel):
    id: str


@router.post("/credits/reset-usage")
def reset_usage(body: ConnId):
    usage_service.reset(body.id)
    credit_service.invalidate()
    return {"ok": True}


# ------------------------------------------------------------------ dung lượng
@router.get("/storage")
def storage(db=Depends(get_db)):
    return storage_service.usage(db)


class CleanIn(BaseModel):
    targets: list[str] = Field(default_factory=lambda: ["segments", "tts", "frames", "work", "previews", "orphans"])


@router.post("/storage/clean")
def storage_clean(body: CleanIn, db=Depends(get_db)):
    freed = storage_service.clean(db, body.targets)
    return {"freed": freed, **storage_service.usage(db)}


class LimitIn(BaseModel):
    cache_limit_gb: float = Field(ge=0.5, le=50)


@router.put("/storage/limit")
def storage_limit(body: LimitIn, db=Depends(get_db)):
    settings.update(cache_limit_gb=body.cache_limit_gb)
    storage_service.prune_all()
    return storage_service.usage(db)


# ------------------------------------------------------------------ tự bổ sung
@router.get("/projects/{pid}/readiness")
def readiness(pid: str, db=Depends(get_db)):
    return autofill_worker.readiness(db, get_project(db, pid))


class AutofillIn(BaseModel):
    replan: bool = False
    only_if_auto: bool = False       # gọi tự động sau khi thêm file: tôn trọng công tắc "tự bổ sung" của dự án


@router.post("/projects/{pid}/autofill", status_code=202)
def autofill(pid: str, body: AutofillIn = AutofillIn(), db=Depends(get_db)):
    p = get_project(db, pid)
    if body.only_if_auto and (p.settings or {}).get("autofill") is False:
        return {"status": "skipped"}
    ensure_idle(p)
    r = autofill_worker.readiness(db, p)
    if not r["can_fill"] and not body.replan and all(i["status"] != "ai" for i in r["items"]):
        return {"status": "nothing", "readiness": r}
    p.status, p.error_message = "analyzing", None
    p.settings = {**(p.settings or {}), "auto_step": "Đang kiểm tra tài nguyên…"}
    db.commit()
    pool.submit(autofill_worker.run_autofill, pid, body.model_dump())
    return {"status": "queued", "readiness": r}


# ------------------------------------------------------------------ chat AI
class ChatIn(BaseModel):
    message: str = Field(min_length=1, max_length=4000)


def _history(p) -> list[dict]:
    return list((p.settings or {}).get("chat") or [])


@router.get("/projects/{pid}/chat")
def chat_history(pid: str, db=Depends(get_db)):
    return _history(get_project(db, pid))


@router.delete("/projects/{pid}/chat")
def chat_clear(pid: str, db=Depends(get_db)):
    p = get_project(db, pid)
    p.settings = {**(p.settings or {}), "chat": []}
    db.commit()
    return []


def _queue_preview(db, p, plan_id: str):
    saved = (p.settings or {}).get("render_opts", {})
    opts = RenderOptions(**{k: v for k, v in saved.items() if k in RenderOptions.model_fields}).model_copy(update={"quality": "preview"})
    W, H = RES[(opts.aspect_ratio, opts.quality)]
    r = Render(project_id=p.id, edit_plan_id=plan_id, format=opts.aspect_ratio, resolution=f"{W}x{H}", options=opts.model_dump())
    db.add(r)
    p.status = "rendering"
    db.commit()
    submit_render(run_render, r.id)


@router.post("/projects/{pid}/chat")
def chat(pid: str, body: ChatIn, db=Depends(get_db)):
    p = get_project(db, pid)
    if not settings.anthropic_api_key:
        raise HTTPException(400, "Chưa kết nối Claude: vào ⚙ Cài đặt và dán API key.")
    ensure_idle(p)
    lock = _chat_locks.setdefault(pid, threading.Lock())
    if not lock.acquire(blocking=False):
        raise HTTPException(409, "AI đang xử lý yêu cầu trước, đợi chút nhé.")
    try:
        try:
            ctx = build_context(db, p)
        except ValueError as e:
            r = autofill_worker.readiness(db, p)
            hint = ("Bấm “✨ Tự động bổ sung bằng AI” để AI làm phần còn thiếu." if r["can_fill"]
                    else "Còn thiếu: " + ", ".join(r["blocked"]))
            raise HTTPException(400, f"{e}. {hint}")
        row = db.query(EditPlanRow).filter_by(project_id=pid).order_by(EditPlanRow.version.desc()).first()
        opts = dict((p.settings or {}).get("render_opts") or {})
        hist = _history(p)
        try:
            res = chat_service.chat_edit(ctx, row.plan if row else None, opts, hist, body.message.strip())
        except Exception as e:
            raise HTTPException(502, f"Gọi Claude lỗi: {friendly(e)}")

        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        out = {"reply": res["reply"], "notes": res["notes"], "issues": res["issues"], "plan_version": None,
               "valid": None, "regenerating": False, "rendering": False}
        for idx, text in res["actions"]["cue_texts"].items():
            c = db.query(SrtCue).filter_by(project_id=pid, cue_index=idx).first()
            if c:
                c.text = text
        if res["options"] != opts:
            p.settings = {**(p.settings or {}), "render_opts": res["options"]}
        new_row = None
        if res["plan"] is not None:
            _, issues = validate_plan(res["plan"], ctx)
            new_row = EditPlanRow(project_id=pid, version=db.query(EditPlanRow).filter_by(project_id=pid).count() + 1,
                                  plan=res["plan"], validation_status="invalid" if issues else "valid",
                                  validation_errors=[str(i) for i in issues])
            db.add(new_row)
            db.flush()
            out["plan_version"], out["valid"], out["issues"] = new_row.version, not issues, [str(i) for i in issues]
            if not issues:
                p.status = "plan_ready"
        hist += [{"role": "user", "text": body.message.strip(), "at": now},
                 {"role": "assistant", "text": res["reply"], "notes": res["notes"], "at": now,
                  "version": out["plan_version"], "valid": out["valid"]}]
        p.settings = {**(p.settings or {}), "chat": hist[-MAX_HISTORY:]}
        db.commit()

        if res["actions"]["regenerate"] is not None:
            style = res["actions"]["regenerate"].get("style")
            p.status, p.error_message = "analyzing", None
            p.settings = {**(p.settings or {}), "auto_step": "Claude đang làm lại bản dựng…", **({"style": style} if style else {})}
            db.commit()
            pool.submit(plan_worker.generate_plan, pid, None, style)
            out["regenerating"] = True
        elif res["actions"]["render"] and find_binary("ffmpeg"):
            target = new_row if (new_row and out["valid"]) else row
            if target is not None and target.validation_status == "valid":
                _queue_preview(db, p, target.id)
                out["rendering"] = True
        out["project"] = to_dict(p)
        return out
    finally:
        lock.release()
