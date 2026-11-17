import shutil
from datetime import datetime, timezone
from pathlib import Path

from ..database import SessionLocal
from ..models import Asset, EditPlanRow, Project, Render, SrtCue
from ..schemas import Cue, EditPlan
from ..services.ffmpeg_service import RenderCancelled
from ..services import folders, storage_service
from ..services.render_service import RenderOptions, render_video
from ..services.srt_service import to_srt
from ..utils.paths import data_dir
from .pool import CANCELLED


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
            latest = lambda t: next((a for a in reversed(assets) if a.asset_type == t), None)
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
        except RenderCancelled:
            r.status, p.status = "cancelled", "plan_ready"
            db.commit()
        except Exception as e:
            r.status, r.error_message, p.status, p.error_message = "failed", str(e)[:2000], "failed", str(e)[:1000]
            db.commit()
            raise
        finally:
            CANCELLED.discard(render_id)
            shutil.rmtree(workdir, ignore_errors=True)
            try:
                storage_service.prune_all()
            except OSError:
                pass
