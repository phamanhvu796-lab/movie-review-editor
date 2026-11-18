"""Trợ lý AI (chat sửa bản dựng), tự bổ sung tài nguyên, tín dụng API, dung lượng ổ đĩa."""
import threading
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from ..config import settings
from ..database import get_db
from ..models import EditPlanRow, Render, SrtCue, to_dict
from ..services import chat_log, chat_service, credit_service, storage_service, styles, usage_service
from ..services.api_errors import friendly
from ..services.context_service import build_context
from ..services.validator_service import validate_plan
from ..utils.paths import find_binary
from ..workers import autofill_worker, plan_worker, pool
from ..workers.render_worker import latest_valid_plan, queue_render, take_pending
from .deps import ensure_idle, get_project

router = APIRouter(prefix="/api")
_chat_locks: dict[str, threading.Lock] = {}


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
    if body.only_if_auto and not r["film"]:
        return {"status": "nothing", "readiness": r}      # chưa có footage: đợi người dùng bấm ⚡ Tự động (Kịch bản → Video)
    if not r["can_fill"] and not body.replan and all(i["status"] != "ai" for i in r["items"]):
        return {"status": "nothing", "readiness": r}
    p.status, p.error_message = "analyzing", None
    p.settings = {**(p.settings or {}), "auto_step": "Đang kiểm tra tài nguyên…"}
    db.commit()
    # bấm nút (không phải tự chạy sau khi thêm file): không có phim -> làm luôn Kịch bản → Video
    pool.submit(autofill_worker.run_autofill, pid, {**body.model_dump(), "full": not body.only_if_auto})
    return {"status": "queued", "readiness": r}


# ------------------------------------------------------------------ chat AI
class ChatIn(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    at_time: float | None = Field(None, ge=0)     # giây đang xem trên trình phát -> AI biết "đoạn này" là đoạn nào
    render_id: str | None = None                  # bản đang xem -> dựng lại cùng chất lượng
    auto_render: bool = True                      # sửa xong tự dựng lại ngầm
    auto: bool = False                            # bấm nút ⚡ Tự động edit: lệnh cho Claude làm trọn gói
    quality: str | None = Field(None, pattern="^(preview|final)$")


def _history(p) -> list[dict]:
    return list((p.settings or {}).get("chat") or [])


@router.get("/projects/{pid}/chat")
def chat_history(pid: str, db=Depends(get_db)):
    return _history(get_project(db, pid))


@router.delete("/projects/{pid}/chat")
def chat_clear(pid: str, db=Depends(get_db)):
    get_project(db, pid)
    chat_log.update_settings(pid, {"chat": []})
    return []


def _queue_preview(db, p, plan_id: str, quality: str = "preview"):
    chat_log.update_settings(p.id, {"chat_notify": True})       # dựng xong -> Claude báo trong khung chat
    db.refresh(p)
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


def _director_info(db, p) -> dict:
    r = autofill_worker.readiness(db, p)
    ps = p.settings or {}
    info = {"title": p.movie_title or p.title, "language": p.language or "vi", "mode": "review phim" if r["film"] else
            "kịch bản → video (chưa có phim của người dùng)", "items": [{k: i[k] for k in ("label", "status", "detail")}
                                                                       for i in r["items"]],
            "current_style": (ps.get("broll") or {}).get("style") or ps.get("style") or None,
            "has_render": db.query(Render).filter_by(project_id=p.id, status="completed").count() > 0}
    if not r["film"]:
        info["styles"] = {x["id"]: x["label"] for x in styles.listing() if not x.get("disabled")}
    return info


def _start_auto(db, p, style: str | None, topic: str | None, quality: str) -> str | None:
    """Bắt đầu tự động edit theo lệnh trong chat. Trả câu báo lỗi nếu chưa thể bắt đầu."""
    err = autofill_worker.precheck(db, p, True)
    if err and topic and "Chưa có gì để dựng" in err:
        err = None                              # người dùng vừa nêu chủ đề trong chat -> AI viết kịch bản từ đó
    if err:
        return err
    ps = p.settings or {}
    broll = dict(ps.get("broll") or {})
    if style and style in styles.STYLES:
        broll["style"] = style
    if topic:
        broll["topic"] = topic
    if broll != (ps.get("broll") or {}):
        chat_log.update_settings(p.id, {"broll": broll})
        db.refresh(p)
    autofill_worker.start(db, p, {"replan": True, "full": True, "render": quality, "style": style if style and
                                  style not in styles.STYLES else None})
    return None


def _director_turn(db, p, body: "ChatIn", user_msg: dict) -> dict:
    msg = body.message.strip()
    try:
        d = chat_service.director(_director_info(db, p), _history(p),
                                  msg + ("\n[Người dùng vừa bấm nút ⚡ Tự động edit]" if body.auto else ""))
    except Exception as e:
        if not body.auto:
            raise HTTPException(502, f"Gọi Claude lỗi: {friendly(e)}")
        d = {"reply": "Mình bắt đầu tự động edit nhé — từng bước mình sẽ kể ngay tại đây.", "action": "auto_create",
             "style": None, "topic": None, "quality": "final"}
    if body.auto:                                 # nút ⚡ là lệnh rõ ràng: luôn bắt đầu
        d["action"] = "auto_create"
    reply, started = d["reply"], False
    if d["action"] == "auto_create":
        if p.status == "rendering":
            reply += "\n\n⏳ Video đang được xuất — đợi xong rồi bấm lại ⚡ nhé."
        else:
            chat_log.append(p.id, user_msg, {"role": "assistant", "kind": "director", "text": reply})
            db.refresh(p)
            err = _start_auto(db, p, d.get("style"), d.get("topic"), body.quality or d.get("quality") or "final")
            if err is None:
                started = True
            else:
                chat_log.append(p.id, {"role": "assistant", "kind": "error", "text": "⚠️ Chưa bắt đầu được: " + err})
            db.refresh(p)
            return {"reply": reply, "started": started, "error": err, "notes": [], "issues": [],
                    "regenerating": started, "rendering": False, "queued": False, "project": to_dict(p)}
    chat_log.append(p.id, user_msg, {"role": "assistant", "kind": "director", "text": reply})
    db.refresh(p)
    return {"reply": reply, "started": False, "notes": [], "issues": [], "regenerating": False, "rendering": False,
            "queued": False, "project": to_dict(p)}


@router.post("/projects/{pid}/chat")
def chat(pid: str, body: ChatIn, db=Depends(get_db)):
    p = get_project(db, pid)
    if not settings.anthropic_api_key:
        raise HTTPException(400, "Chưa kết nối Claude: vào ⚙ Cài đặt và dán API key.")
    if p.status == "analyzing":           # đang dựng vẫn chat được: sửa xong sẽ tự dựng lại sau lần dựng hiện tại
        raise HTTPException(409, "Claude đang làm việc tự động — đợi xong lượt này rồi nhắn tiếp nhé.")
    lock = _chat_locks.setdefault(pid, threading.Lock())
    if not lock.acquire(blocking=False):
        raise HTTPException(409, "AI đang xử lý yêu cầu trước, đợi chút nhé.")
    try:
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        user_msg = {"role": "user", "text": body.message.strip(),
                    **({"at_time": round(body.at_time, 1)} if body.at_time is not None else {}),
                    **({"auto": True} if body.auto else {})}
        try:
            ctx = build_context(db, p)
        except ValueError:
            ctx = None
        if body.auto or ctx is None:      # chưa có gì để sửa / bấm ⚡: Claude điều phối cả quy trình
            return _director_turn(db, p, body, user_msg)

        row = db.query(EditPlanRow).filter_by(project_id=pid).order_by(EditPlanRow.version.desc()).first()
        opts = dict((p.settings or {}).get("render_opts") or {})
        try:
            res = chat_service.chat_edit(ctx, row.plan if row else None, opts, _history(p), body.message.strip(),
                                         focus=_focus_text(db, p, row.plan if row else None, body.at_time))
        except Exception as e:
            raise HTTPException(502, f"Gọi Claude lỗi: {friendly(e)}")

        out = {"reply": res["reply"], "notes": res["notes"], "issues": res["issues"], "plan_version": None,
               "valid": None, "regenerating": False, "rendering": False, "queued": False, "started": False}
        busy = p.status == "rendering"
        changed = bool(res["options"] != opts)
        for idx, text in res["actions"]["cue_texts"].items():
            c = db.query(SrtCue).filter_by(project_id=pid, cue_index=idx).first()
            if c and c.text != text:
                c.text, changed = text, True
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
        db.commit()
        if res["options"] != opts:
            chat_log.update_settings(pid, {"render_opts": res["options"]})
        chat_log.append(pid, user_msg, {"role": "assistant", "text": res["reply"], "notes": res["notes"],
                                        "version": out["plan_version"], "valid": out["valid"]})
        db.refresh(p)

        quality = _render_quality(db, p, body.render_id)
        auto = res["actions"].get("auto")
        want = res["actions"]["render"] or (body.auto_render and changed)
        if auto is not None:                # Claude quyết định làm lại trọn gói
            if busy:
                out["notes"] = out["notes"] + ["Video đang được xuất — đợi xong rồi nhờ mình làm lại toàn bộ nhé."]
            else:
                err = _start_auto(db, p, auto.get("style"), None, auto.get("quality") or "final")
                if err:
                    chat_log.append(pid, {"role": "assistant", "kind": "error", "text": "⚠️ Chưa bắt đầu được: " + err})
                out["started"] = out["regenerating"] = err is None
        elif res["actions"]["regenerate"] is not None:
            style = res["actions"]["regenerate"].get("style")
            if busy:                       # đang dựng: không chen ngang lần dựng hiện tại
                out["notes"] = out["notes"] + ["Video đang được dựng — đợi xong rồi gửi lại yêu cầu “làm lại toàn bộ” nhé."]
            else:
                patch = {"auto_step": "Claude đang làm lại bản dựng…", **({"style": style} if style else {})}
                if body.auto_render:       # làm lại xong -> plan_worker tự dựng lại cùng chất lượng
                    patch["rerender_pending"] = {"quality": quality, "at": now}
                chat_log.update_settings(pid, patch)
                db.refresh(p)
                p.status, p.error_message = "analyzing", None
                db.commit()
                pool.submit(plan_worker.generate_plan, pid, None, style)
                out["regenerating"] = True
        elif want and find_binary("ffmpeg"):
            if busy:                       # video đang dựng -> ghi nhớ, dựng lại ngay khi lần hiện tại xong
                chat_log.update_settings(pid, {"rerender_pending": {"quality": quality, "at": now}})
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
        db.refresh(p)
        out["project"] = to_dict(p)
        return out
    finally:
        lock.release()
