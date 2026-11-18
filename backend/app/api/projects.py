import mimetypes
import shutil
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from pydantic import BaseModel

from ..database import get_db
from ..models import Asset, EditPlanRow, Project, SrtCue, to_dict
from ..services.ffmpeg_service import probe
from ..services import storage_service
from ..services.srt_service import parse_srt
from ..utils.paths import data_dir
from ..utils.rangefile import ranged_file
from ..workers import analyze_worker, pool
from .deps import ensure_idle, get_project

router = APIRouter(prefix="/api")

VIDEO = {".mp4", ".mov", ".mkv", ".webm"}
AUDIO = {".mp3", ".wav", ".m4a"}
IMAGE = {".png", ".jpg", ".jpeg", ".webp"}
ALLOWED = {"footage": VIDEO, "broll": VIDEO, "voice": AUDIO, "music": AUDIO, "logo": IMAGE, "image": IMAGE,
           "subtitle": {".srt", ".vtt"}, "lut": {".cube"}, "font": {".ttf", ".otf"}}


class ProjectIn(BaseModel):
    title: str
    movie_title: str | None = None
    genre: str | None = None
    language: str = "vi"
    target_duration_seconds: int = 480
    aspect_ratio: str = "16:9"
    settings: dict = {}


class ProjectPatch(BaseModel):
    title: str | None = None
    movie_title: str | None = None
    genre: str | None = None
    language: str | None = None
    target_duration_seconds: int | None = None
    aspect_ratio: str | None = None
    settings: dict | None = None


# ---------- Projects ----------
@router.post("/projects")
def create_project(body: ProjectIn, db=Depends(get_db)):
    p = Project(**body.model_dump())
    db.add(p)
    db.commit()
    return to_dict(p)


@router.get("/projects")
def list_projects(db=Depends(get_db)):
    return [to_dict(p) for p in db.query(Project).order_by(Project.updated_at.desc())]


@router.get("/projects/{pid}")
def read_project(pid: str, db=Depends(get_db)):
    return to_dict(get_project(db, pid))


@router.patch("/projects/{pid}")
def patch_project(pid: str, body: ProjectPatch, db=Depends(get_db)):
    p = get_project(db, pid)
    for k, v in body.model_dump(exclude_unset=True).items():
        setattr(p, k, v)
    db.commit()
    return to_dict(p)


@router.post("/projects/{pid}/duplicate")
def duplicate_project(pid: str, db=Depends(get_db)):
    src = get_project(db, pid)
    p = Project(title=src.title + " (bản sao)", movie_title=src.movie_title, genre=src.genre, language=src.language,
                target_duration_seconds=src.target_duration_seconds, aspect_ratio=src.aspect_ratio,
                settings=dict(src.settings or {}))
    db.add(p)
    db.flush()
    for a in src.assets:   # dùng chung file, không copy footage nặng
        db.add(Asset(project_id=p.id, asset_type=a.asset_type, file_name=a.file_name, file_path=a.file_path,
                     size_bytes=a.size_bytes, duration_seconds=a.duration_seconds, width=a.width, height=a.height,
                     processing_status=a.processing_status, meta=dict(a.meta or {})))
    for c in src.cues:
        db.add(SrtCue(project_id=p.id, cue_index=c.cue_index, start_seconds=c.start_seconds,
                      end_seconds=c.end_seconds, text=c.text, language=c.language))
    last = max(src.plans, key=lambda x: x.version, default=None)
    if last:
        db.add(EditPlanRow(project_id=p.id, version=1, plan=last.plan, validation_status=last.validation_status,
                           validation_errors=last.validation_errors))
    db.commit()
    return to_dict(p)


def _unlink_asset_file(db, a: Asset):
    # File "liên kết" nằm ở máy người dùng (không sao chép) -> KHÔNG BAO GIỜ xoá file gốc.
    if (a.meta or {}).get("linked") or not storage_service.is_inside_data(a.file_path):
        return
    others = db.query(Asset).filter(Asset.file_path == a.file_path, Asset.id != a.id).count()
    if not others:
        Path(a.file_path).unlink(missing_ok=True)


@router.delete("/projects/{pid}", status_code=204)
def delete_project(pid: str, db=Depends(get_db)):
    p = get_project(db, pid)
    ensure_idle(p)
    for a in p.assets:
        _unlink_asset_file(db, a)
    for r in p.renders:
        if r.output_path:
            Path(r.output_path).unlink(missing_ok=True)
            Path(r.output_path).with_suffix(".srt").unlink(missing_ok=True)
    db.delete(p)
    db.commit()


# ---------- Assets ----------
def _unique_name(db, pid, name):
    taken = {r[0] for r in db.query(Asset.file_name).filter_by(project_id=pid)}
    if name not in taken:
        return name
    stem, ext, n = Path(name).stem, Path(name).suffix, 2
    while f"{stem} ({n}){ext}" in taken:
        n += 1
    return f"{stem} ({n}){ext}"


@router.post("/projects/{pid}/assets")
def upload_asset(pid: str, asset_type: str = Form(...), description: str = Form(""),
                 file: UploadFile = File(...), db=Depends(get_db)):
    get_project(db, pid)
    if asset_type not in ALLOWED:
        raise HTTPException(400, f"asset_type không hợp lệ: {asset_type}")
    ext = Path(file.filename or "").suffix.lower()
    if ext not in ALLOWED[asset_type]:
        raise HTTPException(400, f"{asset_type} chỉ nhận: {', '.join(sorted(ALLOWED[asset_type]))}")

    a = Asset(project_id=pid, asset_type=asset_type, file_name=_unique_name(db, pid, Path(file.filename).name),
              file_path="", processing_status="processing", meta={"description": description} if description else {})
    db.add(a)
    db.flush()
    folder = data_dir() / "projects" / pid / "assets"
    folder.mkdir(parents=True, exist_ok=True)
    dest = folder / f"{a.id}{ext}"
    with open(dest, "wb") as f:
        shutil.copyfileobj(file.file, f, 1024 * 1024)
    a.file_path, a.size_bytes = str(dest), dest.stat().st_size

    if asset_type in ("footage", "broll", "voice", "music", "logo", "image"):
        try:
            info = probe(dest)
            a.duration_seconds, a.width, a.height = info["duration"], info["width"], info["height"]
            if asset_type in ("footage", "broll") and not info["has_video"]:
                raise RuntimeError("File không có luồng video")
            a.processing_status = "ready"
        except RuntimeError as e:
            a.processing_status, a.meta = "failed", {**(a.meta or {}), "error": str(e)}
    else:
        a.processing_status = "ready"
    db.commit()
    return _asset_dict(a)


class LinkIn(BaseModel):
    paths: list[str]
    asset_type: str = "auto"


def _guess_type(path: Path) -> str | None:
    ext, name = path.suffix.lower(), path.name.lower()
    if ext in VIDEO:
        return "footage"
    if ext in AUDIO:
        return "music" if any(w in name for w in ("nhac", "nhạc", "music", "bgm", "beat", "song")) else "voice"
    if ext in IMAGE:
        return "logo"
    if ext == ".cube":
        return "lut"
    return None


@router.post("/projects/{pid}/assets/link")
def link_assets(pid: str, body: LinkIn, db=Depends(get_db)):
    """Thêm file bằng ĐƯỜNG DẪN trên máy (bản desktop): KHÔNG sao chép -> không tốn thêm ổ đĩa.
    Nếu sau này di chuyển/xoá file gốc thì dự án sẽ báo thiếu file."""
    get_project(db, pid)
    out, errors = [], []
    for raw in body.paths[:200]:
        src = Path(raw)
        t = _guess_type(src) if body.asset_type == "auto" else body.asset_type
        if not src.is_file():
            errors.append(f"{src.name}: không tìm thấy file")
            continue
        if t not in ALLOWED or src.suffix.lower() not in ALLOWED[t]:
            errors.append(f"{src.name}: không nhận ra loại file")
            continue
        a = Asset(project_id=pid, asset_type=t, file_name=_unique_name(db, pid, src.name), file_path=str(src.resolve()),
                  size_bytes=src.stat().st_size, processing_status="processing", meta={"linked": True})
        db.add(a)
        db.flush()
        try:
            info = probe(src)
            a.duration_seconds, a.width, a.height = info["duration"], info["width"], info["height"]
            if t in ("footage", "broll") and not info["has_video"]:
                raise RuntimeError("File không có luồng video")
            a.processing_status = "ready"
        except RuntimeError as e:
            a.processing_status, a.meta = "failed", {**(a.meta or {}), "error": str(e)}
        db.commit()
        out.append(_asset_dict(a))
    return {"assets": out, "errors": errors}


def _asset_dict(a: Asset):
    d = to_dict(a)
    meta = dict(a.meta or {})
    d["scene_count"] = len(meta.pop("scenes", []))
    d["meta"] = meta
    return d


@router.get("/projects/{pid}/assets")
def list_assets(pid: str, db=Depends(get_db)):
    get_project(db, pid)
    return [_asset_dict(a) for a in db.query(Asset).filter_by(project_id=pid).order_by(Asset.created_at)]


@router.delete("/assets/{asset_id}", status_code=204)
def delete_asset(asset_id: str, db=Depends(get_db)):
    a = db.get(Asset, asset_id)
    if not a:
        raise HTTPException(404, "Không tìm thấy asset")
    _unlink_asset_file(db, a)
    db.delete(a)
    db.commit()


class AnalyzeIn(BaseModel):
    tag_scenes: bool = False       # gắn nhãn cảnh bằng Claude vision (tốn API)


@router.post("/projects/{pid}/analyze", status_code=202)
def analyze(pid: str, body: AnalyzeIn = AnalyzeIn(), db=Depends(get_db)):
    p = get_project(db, pid)
    ensure_idle(p)
    p.status = "analyzing"
    db.commit()
    pool.submit(analyze_worker.analyze_project, pid, body.tag_scenes)
    return {"status": "queued"}


# ---------- SRT / kịch bản ----------
class SrtText(BaseModel):
    text: str


class CuePatch(BaseModel):
    text: str | None = None
    start_seconds: float | None = None
    end_seconds: float | None = None


def _store(db, p: Project, parsed):
    db.query(SrtCue).filter_by(project_id=p.id).delete()
    for i, c in enumerate(parsed.cues, 1):      # đánh số lại liên tục 1..N
        db.add(SrtCue(project_id=p.id, cue_index=i, start_seconds=c.start, end_seconds=c.end,
                      text=c.text, language=p.language))
    db.commit()
    return {"cue_count": len(parsed.cues), "issues": [{"line": i.line, "message": i.message} for i in parsed.issues]}


@router.post("/projects/{pid}/srt/parse")
def parse_text(pid: str, body: SrtText, db=Depends(get_db)):
    return _store(db, get_project(db, pid), parse_srt(body.text))


@router.post("/projects/{pid}/srt/upload")
def upload_srt(pid: str, file: UploadFile = File(...), db=Depends(get_db)):
    raw = file.file.read()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("cp1252", errors="replace")
    return _store(db, get_project(db, pid), parse_srt(text))


@router.get("/projects/{pid}/srt/cues")
def list_cues(pid: str, db=Depends(get_db)):
    get_project(db, pid)
    return [to_dict(c) for c in db.query(SrtCue).filter_by(project_id=pid).order_by(SrtCue.cue_index)]


@router.patch("/projects/{pid}/srt/cues/{cue_id}")
def patch_cue(pid: str, cue_id: str, body: CuePatch, db=Depends(get_db)):
    c = db.get(SrtCue, cue_id)
    if not c or c.project_id != pid:
        raise HTTPException(404, "Không tìm thấy cue")
    for k, v in body.model_dump(exclude_none=True).items():
        setattr(c, k, v)
    if c.end_seconds <= c.start_seconds:
        raise HTTPException(422, "end phải lớn hơn start")
    db.commit()
    return to_dict(c)


class TranscribeIn(BaseModel):
    language: str | None = None


@router.post("/projects/{pid}/srt/transcribe", status_code=202)
def transcribe(pid: str, body: TranscribeIn = TranscribeIn(), db=Depends(get_db)):
    p = get_project(db, pid)
    ensure_idle(p)
    if not db.query(Asset).filter_by(project_id=pid, asset_type="voice").count():
        raise HTTPException(400, "Chưa upload voice-over")
    p.status = "analyzing"
    db.commit()
    pool.submit(analyze_worker.transcribe_project, pid, body.language)
    return {"status": "queued"}


# ---------- Phục vụ trình xem trước ----------
@router.get("/assets/{asset_id}/file")
def asset_file(asset_id: str, request: Request, db=Depends(get_db)):
    a = db.get(Asset, asset_id)
    if not a or not Path(a.file_path).exists():
        raise HTTPException(404, "Không tìm thấy file")
    return ranged_file(request, a.file_path, mimetypes.guess_type(a.file_path)[0] or "application/octet-stream")


@router.get("/assets/{asset_id}/scenes")
def asset_scenes(asset_id: str, db=Depends(get_db)):
    a = db.get(Asset, asset_id)
    if not a:
        raise HTTPException(404, "Không tìm thấy asset")
    return (a.meta or {}).get("scenes", [])
