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
from ..services.validator_service import validate_plan
from ..utils.paths import find_binary
from ..workers import autofill_worker, plan_worker, pool
from ..workers.render_worker import latest_valid_plan, queue_render, take_pending
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
    if body.only_if_auto and r["items"][0]["status"] != "ok":
        return {"status": "nothing", "readiness": r}      # chưa có footage: đợi người dùng bấm ⚡ Tự động (Kịch bản → Video)
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
    at_time: float | None = Field(None, ge=0)     # giây đang xem trên trình phát -> AI biết "đoạn này" là đoạn nào
    render_id: str | None = None                  # bản đang xem -> dựng lại cùng chất lượng
    auto_render: bool = True                      # sửa xong tự dựng lại ngầm


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


def _queue_preview(db, p, plan_id: str, quality: str = "preview"):
    return queue_render(db, p, plan_id, quality)


def _focus_text(db, p, plan: dict | None, t: float | None) -> str | None:
    """Mô tả đoạn video ở giây t (câu phụ đề + cảnh) để AI sửa đúng chỗ người xem chỉ."""
    if t is None:
        return None
    cues = db.query(SrtCue).filter_by(project_id=p.id).order_by(SrtCue.cue_index).all()
    cue = next((c for c in cues if c.start_seconds <= t < c.end_seconds), None)
    if cue is None and cues:
        cue = min(cues, key=lambda c: min(abs(c.start_seconds - t), abs(c.end_seconds - t)))
    mm = f"{int(t // 60)}:{t % 60:04.1f}"
    lines = [f"Người xem đang dừng ở {mm} ({t:.1f}s) và muốn sửa ĐOẠN NÀY (chỉ sửa quanh đây trừ khi yêu cầu nói khác)."]
    if cue is not None:
        lines.append(f"Câu phụ đề #{cue.cue_index} [{cue.start_seconds:.1f}–{cue.end_seconds:.1f}s]: {cue.text}")
    for i, sec in enumerate((plan or {}).get("sections") or []):
        if sec.get("voice_start", 0) <= t < sec.get("voice_end", 0) or (cue is not None and sec.get("srt_index") == cue.cue_index):
            vis = ", ".join(f"{v.get('footage')} {v.get('start', 0):.1f}-{v.get('end', 0):.1f}s" for v in sec.get("visuals") or [])
            lines.append(f"Đó là section thứ {i} (srt_index {sec.get('srt_index')}), cảnh đang dùng: {vis}")
            break
    return "\n".join(lines)


def _render_quality(db, p, render_id: str | None) -> str:
    q = db.query(Render).filter_by(project_id=p.id)
    r = q.filter_by(id=render_id).first() if render_id else None
    r = r or q.filter_by(status="completed").order_by(Render.completed_at.desc()).first()
    return ((r.options or {}).get("quality") if r else None) or "preview"


@router.post("/projects/{pid}/chat")
def chat(pid: str, body: ChatIn, db=Depends(get_db)):
    p = get_project(db, pid)
    if not settings.anthropic_api_key:
        raise HTTPException(400, "Chưa kết nối Claude: vào ⚙ Cài đặt và dán API key.")
    if p.status == "analyzing":           # đang dựng vẫn chat được: sửa xong sẽ tự dựng lại sau lần dựng hiện tại
        raise HTTPException(409, "Project đang được phân tích, đợi xong rồi chat nhé.")
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
            res = chat_service.chat_edit(ctx, row.plan if row else None, opts, hist, body.message.strip(),
                                         focus=_focus_text(db, p, row.plan if row else None, body.at_time))
        except Exception as e:
            raise HTTPException(502, f"Gọi Claude lỗi: {friendly(e)}")

        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        out = {"reply": res["reply"], "notes": res["notes"], "issues": res["issues"], "plan_version": None,
               "valid": None, "regenerating": False, "rendering": False, "queued": False}
        busy = p.status == "rendering"
        changed = bool(res["options"] != opts)
        for idx, text in res["actions"]["cue_texts"].items():
            c = db.query(SrtCue).filter_by(project_id=pid, cue_index=idx).first()
            if c and c.text != text:
                c.text, changed = text, True
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
            changed = changed or not issues
            if not issues and not busy:
                p.status = "plan_ready"
        hist += [{"role": "user", "text": body.message.strip(), "at": now,
                  **({"at_time": round(body.at_time, 1)} if body.at_time is not None else {})},
                 {"role": "assistant", "text": res["reply"], "notes": res["notes"], "at": now,
                  "version": out["plan_version"], "valid": out["valid"]}]
        p.settings = {**(p.settings or {}), "chat": hist[-MAX_HISTORY:]}
        db.commit()

        quality = _render_quality(db, p, body.render_id)
        want = res["actions"]["render"] or (body.auto_render and changed)
        if res["actions"]["regenerate"] is not None:
            style = res["actions"]["regenerate"].get("style")
            if busy:                       # đang dựng: không chen ngang lần dựng hiện tại
                out["notes"] = out["notes"] + ["Video đang được dựng — đợi xong rồi gửi lại yêu cầu “làm lại toàn bộ” nhé."]
            else:
                st = {**(p.settings or {}), "auto_step": "Claude đang làm lại bản dựng…", **({"style": style} if style else {})}
                if body.auto_render:       # làm lại xong -> plan_worker tự dựng lại cùng chất lượng
                    st["rerender_pending"] = {"quality": quality, "at": now}
                p.status, p.error_message, p.settings = "analyzing", None, st
                db.commit()
                pool.submit(plan_worker.generate_plan, pid, None, style)
                out["regenerating"] = True
        elif want and find_binary("ffmpeg"):
            if busy:                       # video đang dựng -> ghi nhớ, dựng lại ngay khi lần hiện tại xong
                p.settings = {**(p.settings or {}), "rerender_pending": {"quality": quality, "at": now}}
                db.commit()
                db.refresh(p)
                if p.status == "rendering":
                    out["queued"] = True
                else:                      # vừa dựng xong đúng lúc này -> tự dựng luôn
                    target = latest_valid_plan(db, pid)
                    take_pending(db, p)
                    if target is not None:
                        _queue_preview(db, p, target.id, quality)
                        out["rendering"] = True
            else:
                target = new_row if (new_row and out["valid"]) else latest_valid_plan(db, pid)
                if target is not None and target.validation_status == "valid":
                    _queue_preview(db, p, target.id, quality)
                    out["rendering"] = True
        out["quality"] = quality
        out["project"] = to_dict(p)
        return out
    finally:
        lock.release()
