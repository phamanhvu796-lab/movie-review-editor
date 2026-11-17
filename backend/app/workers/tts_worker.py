import shutil
import uuid
from pathlib import Path

from ..database import SessionLocal
from ..models import Asset, Project, SrtCue
from ..services.ffmpeg_service import probe
from ..services.tts_service import build_voice, cues_from_chunks
from ..utils.paths import data_dir


def generate_voice(project_id: str, text: str, provider: str, voice_id: str, style: str,
                   speed: float, set_cues: bool, quality: str = "high"):
    with SessionLocal() as db:
        p = db.get(Project, project_id)
        p.status, p.error_message = "analyzing", None
        db.commit()
        aid = uuid.uuid4().hex
        work = data_dir() / "work" / f"tts_{aid}"
        out = data_dir() / "projects" / project_id / "assets" / f"{aid}.wav"
        try:
            chunks, durs = build_voice(text, out, work, provider, voice_id, style, speed, quality)
            info = probe(out)
            db.add(Asset(id=aid, project_id=project_id, asset_type="voice", file_name=f"voice_ai_{aid[:6]}.wav",
                         file_path=str(out), size_bytes=out.stat().st_size, duration_seconds=info["duration"],
                         processing_status="ready", meta={"tts": {"provider": provider, "voice_id": voice_id}}))
            if set_cues:   # phụ đề từ kịch bản + thời lượng thật của audio cuối cùng
                db.query(SrtCue).filter_by(project_id=project_id).delete()
                for c in cues_from_chunks(chunks, durs):
                    db.add(SrtCue(project_id=project_id, cue_index=c.index, start_seconds=c.start,
                                  end_seconds=c.end, text=c.text, language=p.language))
            # Giọng AI cũ không còn dùng (render luôn lấy voice mới nhất) -> xoá để đỡ tốn ổ đĩa
            for old in db.query(Asset).filter(Asset.project_id == project_id, Asset.asset_type == "voice", Asset.id != aid):
                if (old.meta or {}).get("tts"):
                    if not db.query(Asset).filter(Asset.file_path == old.file_path, Asset.id != old.id).count():
                        Path(old.file_path).unlink(missing_ok=True)
                    db.delete(old)
            p.settings = {**(p.settings or {}), "script": text, "voice_id": voice_id, "voice_provider": provider,
                          "voice_style": style, "voice_speed": speed, "voice_quality": quality}
            p.status = "draft"
            db.commit()
        except Exception as e:
            out.unlink(missing_ok=True)
            p.status, p.error_message = "failed", str(e)[:1000]
            db.commit()
            raise
        finally:
            shutil.rmtree(work, ignore_errors=True)
