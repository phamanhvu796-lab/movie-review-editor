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
from ..services import chat_log, metadata_service, script_service, tts_service
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
    has_music = any(a.asset_type == "music" and a.processing_status != "failed" for a in assets)
    stock = [a for a in assets if (a.meta or {}).get("stock")]
    if foot:
        foot_item = ("footage", "Footage phim", "ok", f"{len(foot)} file")
        scene_item = ("scenes", "Phân tích cảnh", "ok" if scenes_ok else "ai", "" if scenes_ok else "Tự phát hiện cảnh")
    elif stock:
        foot_item = ("footage", "Hình minh hoạ", "ok", f"{len(stock)} ảnh/video AI đã tìm (thư mục Tài nguyên AI)")
        scene_item = ("scenes", "Phân tích cảnh", "ok", "")
    else:
        foot_item = ("footage", "Footage / hình minh hoạ", "ai" if llm else "user",
                     "Review phim: thêm file phim của bạn. Không có phim → AI tự tìm ảnh/video miễn phí bản quyền, "
                     "lưu vào thư mục đầu vào/Tài nguyên AI" if llm else "Thêm video phim ở Tài nguyên, hoặc kết nối Claude")
        scene_item = ("scenes", "Phân tích cảnh", "ai" if llm else "user", "Tự động sau khi có hình")
    music_auto = llm and ps.get("auto_music") is not False
    items = [
        foot_item,
        scene_item,
        ("script", "Kịch bản", "ok" if (script or cues or voice) else ("ai" if llm else "user"),
         "" if (script or cues or voice) else ("Claude tự viết theo tên phim + cảnh" if llm else "Cần kết nối Claude hoặc dán kịch bản")),
        ("voice", "Giọng đọc", "ok" if voice else ("ai" if tts else "user"),
         "" if voice else (f"Tạo giọng AI ({'ElevenLabs' if tts == 'elevenlabs' else 'OpenAI'})" if tts
                           else "Tải voice-over lên, hoặc thêm key ElevenLabs/OpenAI TTS ở Cài đặt")),
        ("subtitles", "Phụ đề", "ok" if cues else ("ai" if (tts or voice or script) else "user"),
         "" if cues else ("Tạo cùng giọng AI" if (not voice and tts) else ("Whisper nghe giọng đọc" if whisper else "Ước lượng theo kịch bản"))),
        ("plan", "Bản dựng", "ok" if plan else ("ai" if llm else "user"), "" if plan else ("Claude lên bản dựng" if llm else "Cần kết nối Claude")),
        ("metadata", "Tiêu đề / mô tả", "ok" if ps.get("metadata") else ("ai" if llm else "optional"), ""),
        ("music", "Nhạc nền", "ok" if has_music else ("ai" if music_auto else "optional"),
         "" if has_music else ("Claude chọn bài hợp nội dung trong thư mục Nhạc nền, thiếu thì lấy nhạc miễn phí bản quyền"
                               if music_auto else "Tuỳ chọn — tải lên nếu muốn")),
        ("logo", "Logo", "ok" if any(a.asset_type == "logo" for a in assets) else "optional", "Tuỳ chọn — logo kênh của bạn"),
    ]
    out = [{"key": k, "label": lb, "status": st, "detail": d} for k, lb, st, d in items]
    return {"items": out, "film": bool(foot), "complete": all(i["status"] in ("ok", "optional") for i in out),
            "can_fill": any(i["status"] == "ai" for i in out), "blocked": [i["label"] for i in out if i["status"] == "user"]}


def _step(pid: str, text: str, status: str = "analyzing"):
    with SessionLocal() as db:
        p = db.get(Project, pid)
        p.settings = {**(p.settings or {}), "auto_step": text}
        p.status, p.error_message = status, None
        db.commit()
    try:                                  # kể lại trong khung chat: "Claude đang …"
        chat_log.progress(pid, text)
    except Exception:
        log.debug("chat progress", exc_info=True)


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
        queued = None
        if not error and render and _renderable(db, pid):
            from ..utils.paths import find_binary
            from .render_worker import latest_valid_plan, queue_render
            if find_binary("ffmpeg"):
                q = render if render in ("preview", "final") else "preview"
                p.settings = {**(p.settings or {}), "chat_notify": True}     # xuất xong -> báo trong khung chat
                queued = queue_render(db, p, latest_valid_plan(db, pid).id, q)
                queued = {"quality": q, "resolution": queued.resolution}
        if queued is None:
            db.commit()
        info = {"title": p.movie_title or p.title, "has_plan": has_plan}
    _post_summary(pid, error, notes or [], queued, info)


def _post_summary(pid: str, error: str | None, notes: list[str], render: dict | None, info: dict) -> None:
    """Claude tổng kết lượt tự động trong khung chat (lỗi gọi Claude -> dùng mẫu có sẵn)."""
    try:
        steps = chat_log.last_steps(pid)
    except Exception:
        steps = []
    data = {"video": info.get("title"), "steps_done": steps, "notes": notes, "error": error,
            "has_valid_plan": info.get("has_plan"), "exporting": render}
    text = ""
    if settings.anthropic_api_key:
        try:
            from ..services import chat_service
            text = chat_service.summarize(data)
        except Exception as e:
            log.warning("Tóm tắt chat lỗi: %s", e)
    if not text:
        if error:
            text = f"Mình gặp lỗi khi làm: {error}"
        else:
            text = ("Xong phần tự động." if info.get("has_plan") else "Mình đã làm được một phần.")
            if render:
                text += f" Đang xuất video {render['resolution']}, xong mình sẽ báo ngay ở đây."
            text += " Bạn muốn chỉnh gì thì cứ nhắn nhé."
    try:
        chat_log.append(pid, {"role": "assistant", "kind": "error" if error else "summary", "text": text,
                              "notes": notes, **({"rendering": render} if render else {})})
    except Exception:
        log.debug("chat summary", exc_info=True)


def start(db, p, opts: dict) -> None:
    """Bắt đầu lượt tự động edit (từ nút ⚡ hoặc khi Claude quyết định trong chat)."""
    from . import pool
    db.commit()
    chat_log.update_settings(p.id, {"auto_step": "Đang kiểm tra tài nguyên…"}, drop=("chat_notify",))
    db.refresh(p)
    p.status, p.error_message = "analyzing", None
    db.commit()
    pool.submit(run_autofill, p.id, opts)


def precheck(db, p, render: bool = True) -> str | None:
    """Điều kiện tối thiểu để bắt đầu tự động edit. Trả câu báo lỗi (tiếng Việt) hoặc None."""
    from ..utils.paths import find_binary
    if not settings.anthropic_api_key:
        return "Chưa kết nối Claude: vào ⚙ Cài đặt và dán API key"
    if render and not find_binary("ffmpeg"):
        return "Thiếu FFmpeg (đặt ffmpeg.exe vào thư mục bin/)"
    assets = db.query(Asset).filter(Asset.project_id == p.id, Asset.processing_status != "failed").all()
    has_voice = any(a.asset_type == "voice" for a in assets)
    has_footage = any(a.asset_type == "footage" for a in assets)
    has_cues = db.query(SrtCue).filter_by(project_id=p.id).count() > 0
    ps = p.settings or {}
    has_script = bool((ps.get("script") or "").strip())
    topic = (ps.get("broll") or {}).get("topic")
    if not (has_voice or has_cues or has_script or topic or has_footage):
        return ("Chưa có gì để dựng: thêm file phim (review phim), hoặc voice-over MP3 / file SRT, "
                "hoặc nói cho mình chủ đề / dán kịch bản ở mục Kịch bản → Video")
    if not has_voice and not _has_tts() and not has_footage:
        return "Cần giọng đọc: tải file voice-over (MP3/WAV) lên, hoặc thêm key ElevenLabs/OpenAI TTS ở Cài đặt"
    return None


def auto_metadata(pid: str, notes: list[str]) -> None:
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


def auto_music(pid: str, notes: list[str]) -> None:
    """Chưa có nhạc nền -> Claude chọn bài hợp nội dung (kho Nhạc nền của bạn, hoặc nhạc miễn phí bản quyền trên mạng)."""
    from pathlib import Path
    from ..services import music_service, styles
    with SessionLocal() as db:
        p = db.get(Project, pid)
        ps = p.settings or {}
        if ps.get("auto_music") is False or not settings.anthropic_api_key:
            return
        assets = db.query(Asset).filter_by(project_id=pid).all()
        if any(a.asset_type == "music" and a.processing_status != "failed" for a in assets):
            return
        cues = db.query(SrtCue).filter_by(project_id=pid).order_by(SrtCue.cue_index).all()
        text = (ps.get("script") or " ".join(c.text for c in cues))[:1800]
        voice = [a for a in assets if a.asset_type == "voice" and a.processing_status != "failed"]
        seconds = (voice[-1].duration_seconds if voice else None) or (cues[-1].end_seconds if cues else 120)
        sid = (ps.get("broll") or {}).get("style")
        label = (styles.STYLES.get(sid) or {}).get("label") if sid else None
        summary = {"title": p.movie_title or p.title, "kind": label or ("review phim — " + (ps.get("style") or p.genre or "")),
                   "language": p.language or "vi", "seconds": round(seconds), "script_excerpt": text}
    _step(pid, "Claude đang chọn nhạc nền hợp nội dung…")
    try:
        pick = music_service.auto_pick(summary, seconds, online=ps.get("music_online", True) is not False)
    except Exception as e:
        notes.append(f"Chưa chọn được nhạc nền tự động: {friendly(e)[:140]}")
        return
    if not pick:
        notes.append("Không tìm được nhạc nền hợp — thả nhạc vào thư mục Nhạc nền (thư mục đầu vào) rồi chạy lại")
        return
    path = Path(pick["path"])
    with SessionLocal() as db:
        p = db.get(Project, pid)
        names = {a.file_name for a in db.query(Asset).filter_by(project_id=pid)}
        name, i = path.name, 2
        while name in names:
            name, i = f"{path.stem} ({i}){path.suffix}", i + 1
        db.add(Asset(project_id=pid, asset_type="music", file_name=name, file_path=str(path), size_bytes=path.stat().st_size,
                     duration_seconds=pick["duration"], processing_status="ready",
                     meta={"linked": True, "auto": True, "mood": pick.get("mood"), "reason": pick.get("reason"),
                           "license": pick.get("license"), "attribution": pick.get("attribution"), "source": pick.get("source")}))
        st = dict(p.settings or {})
        st["render_opts"] = {**(st.get("render_opts") or {}), "music_volume": pick["volume"], "music_ducking": True}
        credit = music_service.credit_line(pick)
        if credit:
            st["music_credit"] = credit
        p.settings = st
        db.commit()
    where = "kho của bạn" if pick["source"] == "library" else "nhạc miễn phí bản quyền (đã lưu vào Nhạc nền/AI)"
    notes.append(f"Claude chọn nhạc nền “{pick['title']}” từ {where}" + (f": {pick['reason']}" if pick.get("reason") else ""))


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
        if not r["film"]:                # không có phim của người dùng -> chế độ Kịch bản → Video
            if opts.get("full") and (opts.get("replan") or st["plan"] != "ok" or st["footage"] != "ok"):
                from . import broll_worker   # tự tìm hình minh hoạ + dựng (+ nhạc nền, + xuất video nếu được yêu cầu)
                broll_worker.run_broll(pid, _broll_opts(ps, title, opts))
            elif opts.get("full"):           # đã có bản dựng từ hình minh hoạ: chỉ bổ sung phần còn thiếu
                auto_metadata(pid, notes)
                auto_music(pid, notes)
                _finish(pid, None, notes, opts.get("render"))
            else:
                _finish(pid, None, ["Chưa có phim — bấm “⚡ Tự động tạo video” để AI tự tìm ảnh/video minh hoạ "
                                    "(chế độ Kịch bản → Video)."])
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
            whisper_err, whisper_ok = None, False
            if importlib.util.find_spec("faster_whisper") is not None:
                _step(pid, "Đang tạo phụ đề từ giọng đọc (Whisper)…")
                try:
                    analyze_worker.transcribe_project(pid, None, fail_project=False)
                    notes.append("Đã tạo phụ đề bằng Whisper")
                    whisper_ok = True
                except Exception as e:      # Whisper lỗi -> không dừng cả quy trình
                    log.warning("Whisper lỗi: %s", e)
                    whisper_err = friendly(e)[:160]
            if whisper_ok:
                pass
            elif script and vdur:
                with SessionLocal() as db:
                    p = db.get(Project, pid)
                    for c in script_service.estimate_cues(script, vdur):
                        db.add(SrtCue(project_id=pid, cue_index=c.index, start_seconds=c.start, end_seconds=c.end,
                                      text=c.text, language=p.language))
                    db.commit()
                notes.append(("Whisper lỗi (" + whisper_err + ") — " if whisper_err else "")
                             + "Phụ đề được ước lượng theo kịch bản (nên kiểm tra lại thời gian ở mục Kịch bản)")
            else:
                _finish(pid, None, notes + [("Whisper không tạo được phụ đề: " + whisper_err + ". " if whisper_err else "")
                                            + "Thiếu phụ đề: nạp file SRT ở mục Kịch bản (hoặc dán kịch bản để AI ước lượng)."])
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
        auto_metadata(pid, notes)
        auto_music(pid, notes)
        _finish(pid, None, notes, opts.get("render"))
    except Exception as e:
        log.exception("Tự bổ sung lỗi")
        _finish(pid, friendly(e), notes)
