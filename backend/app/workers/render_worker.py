import logging
import shutil
from datetime import datetime, timezone
from pathlib import Path

from ..database import SessionLocal
from ..models import Asset, EditPlanRow, Project, Render, SrtCue
from ..schemas import Cue, EditPlan
from ..services.ffmpeg_service import RenderCancelled
from ..services import folders, storage_service
from ..services.render_service import RES, RenderOptions, render_video
from ..services.srt_service import to_srt
from ..utils.paths import data_dir
from .pool import CANCELLED, submit_render

log = logging.getLogger("mre.render")


def queue_render(db, p: Project, plan_id: str, quality: str = "preview") -> Render:
    """Xếp hàng một lần dựng với tuỳ chọn đã lưu của project (chất lượng tuỳ chọn)."""
    saved = (p.settings or {}).get("render_opts", {})
    opts = RenderOptions(**{k: v for k, v in saved.items() if k in RenderOptions.model_fields})
    if quality not in {q for _, q in RES}:
        quality = "preview"
    opts = opts.model_copy(update={"quality": quality})
    W, H = RES[(opts.aspect_ratio, opts.quality)]
    r = Render(project_id=p.id, edit_plan_id=plan_id, format=opts.aspect_ratio, resolution=f"{W}x{H}", options=opts.model_dump())
    db.add(r)
    p.status = "rendering"
    db.commit()
    submit_render(run_render, r.id)
    return r


def latest_valid_plan(db, pid: str):
    return (db.query(EditPlanRow).filter_by(project_id=pid, validation_status="valid")
            .order_by(EditPlanRow.version.desc()).first())


def take_pending(db, p: Project) -> dict | None:
    """Lấy (và xoá) yêu cầu dựng lại đang chờ — đặt bởi chat AI khi video đang được dựng."""
    db.refresh(p)
    pend = (p.settings or {}).get("rerender_pending")
    if pend:
        st = dict(p.settings or {})
        st.pop("rerender_pending", None)
        p.settings = st
        db.commit()
    return pend or None


def rerender_if_pending(db, p: Project) -> Render | None:
    """Gọi khi một lần dựng kết thúc: nếu trong lúc dựng người dùng đã nhờ AI sửa tiếp -> dựng lại ngay bản mới nhất."""
    try:
        pend = take_pending(db, p)
        if not pend:
            return None
        row = latest_valid_plan(db, p.id)
        if row is None:
            return None
        return queue_render(db, p, row.id, pend.get("quality", "preview"))
    except Exception:
        return None


def _fmt_dur(sec: float | None) -> str:
    sec = int(round(sec or 0))
    return f"{sec // 60}:{sec % 60:02d}"


def notify_chat(db, p: Project, r: Render, seconds: float | None = None, error: str | None = None) -> None:
    """Lần xuất do Claude khởi động (tự động edit / sửa qua chat) xong -> báo ngay trong khung chat."""
    try:
        db.refresh(p)
        st = p.settings or {}
        if not st.get("chat_notify"):
            return
        from ..services import chat_log
        chat_log.update_settings(p.id, {}, drop=("chat_notify",))
        q = (r.options or {}).get("quality", "preview")
        if error:
            msg = {"role": "assistant", "kind": "error",
                   "text": f"❌ Xuất video bị lỗi: {error[:300]}. Bạn nhắn “xuất lại” hoặc mô tả điều muốn đổi, mình thử lại nhé."}
        else:
            label = "thành phẩm" if q == "final" else "bản xem thử"
            more = " Mình đang dựng tiếp bản có thay đổi mới nhất của bạn." if st.get("rerender_pending") else ""
            msg = {"role": "assistant", "kind": "render", "render_id": r.id, "quality": q, "path": r.output_path,
                   "text": f"✅ Xong {label} {r.resolution}, dài {_fmt_dur(seconds)} — đã lưu: {Path(r.output_path).name}. "
                           "Bạn xem thử nhé; chỗ nào chưa ưng thì dừng video ở đó, bấm 📍 Sửa đoạn này rồi nói mình sửa." + more}
        chat_log.append(p.id, msg)
    except Exception:
        log.debug("notify chat", exc_info=True)


def rerender_soon(db, p: Project, quality: str | None = None) -> str:
    """Dựng lại bản dựng hợp lệ mới nhất (báo trong chat khi xong). Đang dựng -> ghi nhớ, dựng ngay sau lần hiện tại.
    Trả "queued" | "pending" | "none"."""
    from ..services import chat_log
    from ..utils.paths import find_binary
    if not find_binary("ffmpeg"):
        return "none"
    if quality is None:
        last = (db.query(Render).filter_by(project_id=p.id, status="completed").order_by(Render.completed_at.desc()).first())
        quality = ((last.options or {}).get("quality") if last else None) or "preview"
    db.refresh(p)
    if p.status in ("rendering", "analyzing"):
        chat_log.update_settings(p.id, {"rerender_pending": {"quality": quality}, "chat_notify": True})
        return "pending"
    row = latest_valid_plan(db, p.id)
    if row is None:
        return "none"
    chat_log.update_settings(p.id, {"chat_notify": True})
    db.refresh(p)
    queue_render(db, p, row.id, quality)
    return "queued"


def run_render(render_id: str):
    with SessionLocal() as db:
        r = db.get(Render, render_id)
        if r.status == "cancelled":      # bị huỷ khi còn đang xếp hàng
            return
        p = db.get(Project, r.project_id)
        r.status, p.status, p.error_message = "running", "rendering", None
        db.commit()
        workdir = data_dir() / "work" / render_id
        cache = data_dir() / "cache" / "segments"
        try:
            plan = EditPlan.model_validate(db.get(EditPlanRow, r.edit_plan_id).plan)
            assets = db.query(Asset).filter_by(project_id=p.id).order_by(Asset.created_at).all()
            latest = lambda t: next((a for a in reversed(assets) if a.asset_type == t and a.processing_status != "failed"), None)
            voice, music, logo = latest("voice"), latest("music"), latest("logo")
            if not voice:
                raise RuntimeError("Chưa có voice-over")
            cues = [Cue(index=c.cue_index, start=c.start_seconds, end=c.end_seconds, text=c.text)
                    for c in db.query(SrtCue).filter_by(project_id=p.id).order_by(SrtCue.cue_index)]
            if r.options.get("quality", "preview") == "preview":       # bản xem thử: bộ nhớ trong của app (tự dọn)
                out = data_dir() / "projects" / p.id / "renders" / f"{render_id}.mp4"
            else:                                                       # bản xuất: vào THƯ MỤC ĐẦU RA người dùng chọn
                out = folders.output_file(p.title or p.movie_title or "video", r.format, r.resolution)
            last = {"p": -1}

            def progress(v):
                if v != last["p"]:
                    last["p"], r.progress = v, v
                    db.commit()

            render_video(
                plan, cues,
                {a.file_name: a.file_path for a in assets if a.asset_type in ("footage", "broll")},
                voice.file_path, voice.duration_seconds or cues[-1].end, out, RenderOptions(**r.options), workdir,
                music_path=music.file_path if music else None, logo_path=logo.file_path if logo else None,
                luts={Path(a.file_name).stem: a.file_path for a in assets if a.asset_type == "lut"},
                progress_cb=progress, cache_dir=cache, cancel=lambda: render_id in CANCELLED)
            out.with_suffix(".srt").write_text(to_srt(cues), "utf-8")
            r.status, r.progress, r.output_path = "completed", 100, str(out)
            r.completed_at, p.status = datetime.now(timezone.utc), "completed"
            db.commit()
            if r.options.get("quality", "preview") == "preview":     # chỉ giữ bản xem thử mới nhất
                try:
                    storage_service.prune_old_previews(db, keep=1, project_id=p.id)
                except Exception:
                    pass
            notify_chat(db, p, r, voice.duration_seconds or cues[-1].end)
            rerender_if_pending(db, p)
        except RenderCancelled:
            r.status, p.status = "cancelled", "plan_ready"
            db.commit()
            take_pending(db, p)               # người dùng chủ động huỷ -> bỏ luôn lần dựng lại đang chờ
        except Exception as e:
            r.status, r.error_message, p.status, p.error_message = "failed", str(e)[:2000], "failed", str(e)[:1000]
            db.commit()
            notify_chat(db, p, r, error=str(e))
            if rerender_if_pending(db, p) is None:
                raise
        finally:
            CANCELLED.discard(render_id)
            shutil.rmtree(workdir, ignore_errors=True)
            try:
                storage_service.prune_all()
            except OSError:
                pass
