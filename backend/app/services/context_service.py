from pathlib import Path

from .. import config as C
from ..models import Asset, Project, SrtCue
from ..schemas import Cue, Footage, PlanContext, Scene
from .render_service import PRESETS


def build_context(db, project: Project, allow_spoilers: bool | None = None, style: str | None = None) -> PlanContext:
    cues = db.query(SrtCue).filter_by(project_id=project.id).order_by(SrtCue.cue_index).all()
    if not cues:
        raise ValueError("Chưa có kịch bản/SRT")
    assets = db.query(Asset).filter_by(project_id=project.id).order_by(Asset.created_at).all()

    footage = []
    for a in assets:
        if a.asset_type not in ("footage", "broll"):
            continue
        meta = a.meta or {}
        scenes = [s for s in meta.get("scenes", []) if s["end"] - s["start"] >= C.VISUAL_DURATION_RANGE[0]]
        if scenes:
            footage.append(Footage(file=a.file_name, duration=a.duration_seconds or 0,
                                   description=meta.get("description", ""),
                                   scenes=[Scene(**{k: v for k, v in s.items() if k in Scene.model_fields}) for s in scenes]))
    if not footage:
        raise ValueError("Chưa phân tích footage: bấm 'Phân tích footage' trước")

    voices = [a for a in assets if a.asset_type == "voice" and a.processing_status != "failed"]
    voice_duration = (voices[-1].duration_seconds if voices else None) or cues[-1].end_seconds
    luts = [Path(a.file_name).stem for a in assets if a.asset_type == "lut"]
    ps = project.settings or {}
    return PlanContext(
        movie_title=project.movie_title or project.title, genre=project.genre or "", language=project.language or "vi",
        target_duration_seconds=project.target_duration_seconds or 480,
        allow_spoilers=ps.get("allow_spoilers", False) if allow_spoilers is None else allow_spoilers,
        style=ps.get("style", "") if style is None else style,
        voice_duration=voice_duration,
        srt_cues=[Cue(index=c.cue_index, start=c.start_seconds, end=c.end_seconds, text=c.text) for c in cues],
        available_footage=footage, available_filters=list(PRESETS) + luts,
    )
