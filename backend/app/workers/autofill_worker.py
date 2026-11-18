"""Tự phân tích tài nguyên của dự án và VÁ chỗ thiếu bằng AI để có thể dựng/edit ngay.

Thứ tự: phân tích cảnh -> (AI gắn nhãn cảnh nếu phải tự viết kịch bản) -> AI viết kịch bản -> AI tạo giọng đọc
(ElevenLabs/OpenAI) -> phụ đề (từ giọng AI, Whisper, hoặc ước lượng theo kịch bản) -> Claude lên bản dựng -> tiêu đề/mô tả.
Không tự xuất video (tiết kiệm CPU và ổ đĩa) — người dùng bấm xem nhanh hoặc xuất khi muốn.
"""
import importlib.util
import logging

from ..config import settings
from ..database import SessionLocal
from ..models import Asset, EditPlanRow, Project, SrtCue
from ..services import metadata_service, script_service, tts_service
from ..services.api_errors import friendly
from . import analyze_worker, plan_worker, tts_worker

log = logging.getLogger("mre.autofill")


def _has_tts() -> str | None:
    if settings.usable_connections("elevenlabs"):
        return "elevenlabs"
    if settings.usable_connections("openai_tts"):
        return "openai"
    return None


def readiness(db, p: Project) -> dict:
    """Bảng kiểm tra: mỗi mục ok | ai (AI sẽ tự bổ sung) | user (cần người dùng) | optional."""
    assets = db.query(Asset).filter_by(project_id=p.id).all()
    foot = [a for a in assets if a.asset_type in ("footage", "broll") and a.processing_status != "failed"
            and not (a.meta or {}).get("stock")]      # hình tải từ kho (Kịch bản → Video) không tính là footage phim
    voice = any(a.asset_type == "voice" and a.processing_status != "failed" for a in assets)
    cues = db.query(SrtCue).filter_by(project_id=p.id).count()
    plan = db.query(EditPlanRow).filter_by(project_id=p.id).count()
    ps = p.settings or {}
    script = bool((ps.get("script") or "").strip())
    llm, tts = bool(settings.anthropic_api_key), _has_tts()
    whisper = importlib.util.find_spec("faster_whisper") is not None
    scenes_ok = bool(foot) and all((a.meta or {}).get("scenes") for a in foot)
    items = [
        ("footage", "Footage phim", "ok" if foot else "user", f"{len(foot)} file" if foot else "Thêm video phim ở Tài nguyên (AI không tạo được footage)"),
        ("scenes", "Phân tích cảnh", "ok" if scenes_ok else ("ai" if foot else "user"), "" if scenes_ok else "Tự phát hiện cảnh"),
        ("script", "Kịch bản", "ok" if (script or cues or voice) else ("ai" if llm else "user"),
         "" if (script or cues or voice) else ("Claude tự viết theo tên phim + cảnh" if llm else "Cần kết nối Claude hoặc dán kịch bản")),
        ("voice", "Giọng đọc", "ok" if voice else ("ai" if tts else "user"),
         "" if voice else (f"Tạo giọng AI ({'ElevenLabs' if tts == 'elevenlabs' else 'OpenAI'})" if tts
                           else "Tải voice-over lên, hoặc thêm key ElevenLabs/OpenAI TTS ở Cài đặt")),
        ("subtitles", "Phụ đề", "ok" if cues else ("ai" if (tts or voice or script) else "user"),
         "" if cues else ("Tạo cùng giọng AI" if (not voice and tts) else ("Whisper nghe giọng đọc" if whisper else "Ước lượng theo kịch bản"))),
        ("plan", "Bản dựng", "ok" if plan else ("ai" if llm else "user"), "" if plan else ("Claude lên bản dựng" if llm else "Cần kết nối Claude")),
        ("metadata", "Tiêu đề / mô tả", "ok" if ps.get("metadata") else ("ai" if llm else "optional"), ""),
        ("music", "Nhạc nền", "ok" if any(a.asset_type == "music" for a in assets) else "optional", "Tuỳ chọn — tải lên nếu muốn"),
        ("logo", "Logo", "ok" if any(a.asset_type == "logo" for a in assets) else "optional", "Tuỳ chọn"),
    ]
    out = [{"key": k, "label": lb, "status": st, "detail": d} for k, lb, st, d in items]
    return {"items": out, "complete": all(i["status"] in ("ok", "optional") for i in out),
            "can_fill": any(i["status"] == "ai" for i in out), "blocked": [i["label"] for i in out if i["status"] == "user"]}


def _step(pid: str, text: str, status: str = "analyzing"):
    with SessionLocal() as db:
        p = db.get(Project, pid)
        p.settings = {**(p.settings or {}), "auto_step": text}
        p.status, p.error_message = status, None
        db.commit()


def _finish(pid: str, error: str | None = None, notes: list[str] | None = None, render: str | None = None):
    """Kết thúc một quy trình tự động. render="preview"|"final": xếp hàng xuất video NGAY trong cùng lần lưu
    (trạng thái chuyển thẳng sang 'rendering', giao diện không thấy khoảng 'xong' giả ở giữa)."""
    with SessionLocal() as db:
        p = db.get(Project, pid)
        has_plan = db.query(EditPlanRow).filter_by(project_id=pid, validation_status="valid").count() > 0
        p.settings = {**(p.settings or {}), "auto_step": "", "autofill_notes": notes or []}
        if error:
            p.status, p.error_message = "failed", error[:1000]
        else:
            p.status = "plan_ready" if has_plan else "draft"
        if not error and render and _renderable(db, pid):
            from ..utils.paths import find_binary
            from .render_worker import latest_valid_plan, queue_render
            if find_binary("ffmpeg"):
                queue_render(db, p, latest_valid_plan(db, pid).id, render if render in ("preview", "final") else "preview")
                return
        db.commit()


def _renderable(db, pid: str) -> bool:
    """Chỉ xuất khi bản dựng MỚI NHẤT hợp lệ (bản mới còn lỗi thì không xuất nhầm bản cũ)."""
    newest = db.query(EditPlanRow).filter_by(project_id=pid).order_by(EditPlanRow.version.desc()).first()
    return newest is not None and newest.validation_status == "valid"


def _scene_notes(pid: str) -> list[str]:
    with SessionLocal() as db:
        out = []
        for a in db.query(Asset).filter(Asset.project_id == pid, Asset.asset_type.in_(["footage", "broll"])):
            for s in (a.meta or {}).get("scenes", [])[:60]:
                if s.get("description"):
                    out.append(f"- {a.file_name} {s['start']:.0f}s: {s.get('shot_type') or ''} {s['description']}")
        return out


def _broll_opts(ps: dict, title: str, opts: dict) -> dict:
    from ..services import styles
    b = ps.get("broll") or {}
    sid = b.get("style") or opts.get("style")
    try:
        styles.get(sid)
    except (ValueError, KeyError, TypeError):
        sid = "fin_faceless"
    return {"style": sid, "media": b.get("media"), "topic": b.get("topic") or title or "", "script": None,
            "target_seconds": b.get("target_seconds") or None, "render": opts.get("render"), "factcheck": True}


def run_autofill(pid: str, opts: dict | None = None):
    opts = opts or {}
    notes: list[str] = []
    try:
        with SessionLocal() as db:
            p = db.get(Project, pid)
            r = readiness(db, p)
            st = {i["key"]: i["status"] for i in r["items"]}
            ps = dict(p.settings or {})
            title, genre, lang = p.movie_title or p.title, p.genre or "", p.language or "vi"
            target = p.target_duration_seconds or 480
        if st["footage"] != "ok":
            if opts.get("full"):         # bấm ⚡ Tự động: không có footage -> Kịch bản → Video (tự tìm hình minh hoạ)
                from . import broll_worker
                broll_worker.run_broll(pid, _broll_opts(ps, title, opts))
            else:
                _finish(pid, None, ["Chưa có footage — thêm video phim, hoặc bấm “⚡ Tự động tạo video” để AI tự tìm "
                                    "ảnh/video minh hoạ (chế độ Kịch bản → Video)."])
            return
        need_script = st["script"] == "ai"

        tag = bool((need_script or opts.get("tag_scenes")) and settings.anthropic_api_key)
        if st["scenes"] != "ok" or tag:
            _step(pid, "AI đang phân tích cảnh phim…" if tag else "Đang phân tích cảnh phim…")
            # Phải tự viết kịch bản -> cần biết cảnh có gì: gắn nhãn cảnh bằng AI (model nhanh, rẻ).
            analyze_worker.analyze_project(pid, tag)
            notes.append("Đã phân tích cảnh phim")

        script = (ps.get("script") or "").strip()
        if need_script:
            _step(pid, "Claude đang viết kịch bản…")
            script = script_service.write_script(title, genre, lang, target, _scene_notes(pid), ps.get("style", ""),
                                                 bool(ps.get("allow_spoilers")))
            with SessionLocal() as db:
                p = db.get(Project, pid)
                p.settings = {**(p.settings or {}), "script": script, "script_by_ai": True}
                db.commit()
            notes.append(f"AI đã viết kịch bản ({len(script):,} ký tự) — xem/sửa ở mục Giọng AI")

        if st["voice"] == "ai" and script:
            prov = _has_tts()
            voice_id = ps.get("voice_id") or ""
            if prov == "elevenlabs" and not voice_id:
                voices = tts_service.list_voices("elevenlabs")
                voice_id = voices[0]["id"] if voices else ""
            _step(pid, f"Đang tạo giọng đọc AI ({'ElevenLabs' if prov == 'elevenlabs' else 'OpenAI'})…")
            tts_worker.generate_voice(pid, script, prov, voice_id or "alloy", ps.get("voice_style", "narrative"),
                                      float(ps.get("voice_speed") or 1.0), True, ps.get("voice_quality", "high"))
            notes.append("Đã tạo giọng đọc AI và phụ đề khớp từng câu")
        elif st["voice"] == "user":
            _finish(pid, None, notes + ["Cần giọng đọc: tải voice-over lên, hoặc thêm key ElevenLabs/OpenAI TTS để AI đọc kịch bản."])
            return

        with SessionLocal() as db:
            has_cues = db.query(SrtCue).filter_by(project_id=pid).count() > 0
            voice = (db.query(Asset).filter_by(project_id=pid, asset_type="voice").order_by(Asset.created_at.desc()).first())
            vdur, vpath = (voice.duration_seconds or 0, voice.file_path) if voice else (0, None)
        if not vpath:
            _finish(pid, None, notes + ["Cần giọng đọc: tải voice-over lên, hoặc dán kịch bản ở mục Giọng AI và thêm key ElevenLabs/OpenAI TTS."])
            return
        if not has_cues:
            if importlib.util.find_spec("faster_whisper") is not None:
                _step(pid, "Đang tạo phụ đề từ giọng đọc (Whisper)…")
                analyze_worker.transcribe_project(pid, None)
                notes.append("Đã tạo phụ đề bằng Whisper")
            elif script and vdur:
                with SessionLocal() as db:
                    p = db.get(Project, pid)
                    for c in script_service.estimate_cues(script, vdur):
                        db.add(SrtCue(project_id=pid, cue_index=c.index, start_seconds=c.start, end_seconds=c.end,
                                      text=c.text, language=p.language))
                    db.commit()
                notes.append("Phụ đề được ước lượng theo kịch bản (nên kiểm tra lại thời gian ở mục Kịch bản)")
            else:
                _finish(pid, None, notes + ["Thiếu phụ đề: nạp file SRT ở mục Kịch bản (hoặc dán kịch bản để AI ước lượng)."])
                return

        if not settings.anthropic_api_key:
            _finish(pid, None, notes + ["Kết nối Claude ở Cài đặt để AI tự lên bản dựng."])
            return
        with SessionLocal() as db:
            has_plan = db.query(EditPlanRow).filter_by(project_id=pid).count() > 0
        if not has_plan or opts.get("replan"):
            _step(pid, "Claude đang lên bản dựng…")
            plan_worker.generate_plan(pid, opts.get("allow_spoilers"), opts.get("style"))
            notes.append("Claude đã lên bản dựng")
        with SessionLocal() as db:
            p = db.get(Project, pid)
            need_meta = not (p.settings or {}).get("metadata")
            cues = db.query(SrtCue).filter_by(project_id=pid).order_by(SrtCue.cue_index).all()
            plan = db.query(EditPlanRow).filter_by(project_id=pid).order_by(EditPlanRow.version.desc()).first()
            args = (p.movie_title or p.title, p.genre or "", p.language or "vi",
                    (plan.plan.get("creative_direction", "") if plan else ""), " ".join(c.text for c in cues),
                    bool((p.settings or {}).get("allow_spoilers")))
        if need_meta and cues:
            _step(pid, "Claude đang viết tiêu đề, mô tả, hashtag…")
            try:
                meta = metadata_service.generate_metadata(*args)
                with SessionLocal() as db:
                    p = db.get(Project, pid)
                    p.settings = {**(p.settings or {}), "metadata": meta}
                    db.commit()
                notes.append("Đã tạo tiêu đề / mô tả / hashtag")
            except Exception as e:          # phần phụ: lỗi không chặn kết quả chính
                notes.append(f"Chưa tạo được tiêu đề: {friendly(e)}")
        _finish(pid, None, notes, opts.get("render"))
    except Exception as e:
        log.exception("Tự bổ sung lỗi")
        _finish(pid, friendly(e), notes)
