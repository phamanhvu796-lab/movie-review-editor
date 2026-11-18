from concurrent.futures import ThreadPoolExecutor, as_completed

from ..database import SessionLocal
from ..models import Asset, Project, SrtCue
from ..services.scene_service import detect_scenes
from ..services.srt_service import to_srt  # noqa: F401  (giữ để dễ mở rộng xuất SRT)
from ..services.tagging_service import tag_scenes


def _fail(db, p, e):
    p.status, p.error_message = "failed", str(e)[:1000]
    db.commit()


def _scan(path: str, duration: float, meta: dict, tag: bool) -> dict:
    """Chạy trong luồng phụ, KHÔNG đụng DB (session không thread-safe)."""
    meta = dict(meta)
    if not meta.get("scenes"):
        meta["scenes"] = detect_scenes(path, duration)
    if tag and meta["scenes"] and not meta.get("tagged"):
        meta["scenes"] = tag_scenes(path, meta["scenes"])
        meta["tagged"] = True
    return meta


def analyze_project(project_id: str, tag: bool = False):
    with SessionLocal() as db:
        p = db.get(Project, project_id)
        p.status, p.error_message = "analyzing", None
        db.commit()
        try:
            assets = db.query(Asset).filter(Asset.project_id == project_id,
                                            Asset.asset_type.in_(["footage", "broll"])).all()
            for a in assets:
                a.processing_status = "processing"
            db.commit()
            with ThreadPoolExecutor(max_workers=max(1, min(3, len(assets)))) as ex:
                futs = {ex.submit(_scan, a.file_path, a.duration_seconds or 0, a.meta or {}, tag): a.id for a in assets}
                for fut in as_completed(futs):
                    a = db.get(Asset, futs[fut])
                    try:
                        a.meta, a.processing_status = fut.result(), "ready"
                    except Exception as e:      # một file lỗi không làm hỏng các file còn lại
                        a.processing_status, a.meta = "failed", {**(a.meta or {}), "error": str(e)[:300]}
                    db.commit()
            p.status = "draft"
            db.commit()
        except Exception as e:
            if fail_project:            # gọi riêng (nút Tạo phụ đề): báo lỗi lên dự án
                _fail(db, p, e)
            else:                       # trong quy trình tự động: để bên gọi tự xử lý (vd dùng phụ đề ước lượng)
                db.rollback()
            raise


def transcribe_project(project_id: str, language: str | None, fail_project: bool = True,
                       script: str | None = None, progress=None):
    from ..services.whisper_service import transcribe_to_cues
    with SessionLocal() as db:
        p = db.get(Project, project_id)
        p.status, p.error_message = "analyzing", None
        db.commit()
        try:
            voice = (db.query(Asset).filter(Asset.project_id == project_id, Asset.asset_type == "voice",
                                            Asset.processing_status != "failed").order_by(Asset.created_at.desc()).first())
            if voice is None:
                raise ValueError("Chưa có file giọng đọc để tạo phụ đề")
            cues = transcribe_to_cues(voice.file_path, language or p.language, script=script, progress=progress)
            if not cues:
                raise ValueError("Whisper không nghe ra lời nào trong file giọng đọc")
            db.query(SrtCue).filter_by(project_id=project_id).delete()
            for c in cues:
                db.add(SrtCue(project_id=project_id, cue_index=c.index, start_seconds=c.start,
                              end_seconds=c.end, text=c.text, language=language or p.language))
            p.status = "draft"
            db.commit()
        except Exception as e:
            _fail(db, p, e)
            raise
