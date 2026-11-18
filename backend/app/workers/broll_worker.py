"""Chế độ KỊCH BẢN -> VIDEO: tự động từ kịch bản (hoặc chủ đề) ra video có hình minh hoạ, giọng đọc, phụ đề.

Kịch bản (AI viết nếu chỉ có chủ đề) -> giọng đọc (AI, hoặc tách từ video người dẫn, hoặc file bạn tải) -> phụ đề SRT
-> Claude chọn hình cho từng câu -> tải ảnh/video từ kho miễn phí bản quyền -> lắp bản dựng theo phong cách -> (xem thử).
"""
import logging
import shutil
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from ..database import SessionLocal
from ..models import Asset, EditPlanRow, Project, SrtCue
from ..schemas import Cue
from ..services import broll_service, chat_log, factcheck_service, folders, script_service, stock_service, styles
from ..services.api_errors import friendly
from ..services.context_service import build_context
from ..services.ffmpeg_service import probe, run_ffmpeg
from ..services.repair_service import repair_plan
from ..services.validator_service import validate_plan
from ..utils.paths import data_dir
from . import analyze_worker, tts_worker
from .autofill_worker import _finish, _has_tts, _step, auto_metadata, auto_music

log = logging.getLogger("mre.broll")


def _presenter(db, pid: str):
    """Video người dẫn bạn tự quay (Footage, không phải hình tải từ kho)."""
    for a in db.query(Asset).filter_by(project_id=pid, asset_type="footage").order_by(Asset.created_at):
        if not (a.meta or {}).get("stock") and a.processing_status == "ready":
            return a
    return None


def _orig_ext(item: dict) -> str:
    ext = Path(item["url"].split("?")[0]).suffix.lower()
    return ext if ext in (".jpg", ".jpeg", ".png", ".webp") else ".jpg"


def _fetch_one(pid: str, item: dict, W: int, H: int, need_seconds: float, keep_dir: Path | None = None) -> dict | None:
    """Tải 1 ảnh/video và chuẩn hoá. Trả {name, path, duration, kind, item, original}.
    keep_dir (Thư mục đầu vào/Tài nguyên AI/<dự án>): video đã cắt lưu thẳng ở đó; ảnh lưu bản gốc ở đó,
    còn clip dựng từ ảnh (chỉ là bộ đệm) để trong dữ liệu dự án."""
    folder = data_dir() / "projects" / pid / "assets"
    base = "stock_" + stock_service.cache_name(item)
    photo = item["kind"] == "photo"
    final = (folder if (photo or keep_dir is None) else keep_dir) / f"{base}.mp4"
    original = keep_dir / f"{base}{_orig_ext(item)}" if (photo and keep_dir is not None) else None
    try:
        if not final.exists():
            raw = folder / f"{base}.raw"
            if original is not None and original.exists():
                raw = original
            else:
                stock_service.download(item["url"], raw)
            try:
                if photo:
                    stock_service.photo_to_clip(raw, final, W, H)
                else:
                    stock_service.trim_video(raw, final, min(max(need_seconds + 2, 10.0), 24.0))
            finally:
                if original is not None and raw != original:
                    try:
                        shutil.move(str(raw), str(original))
                    except OSError:
                        raw.unlink(missing_ok=True)
                elif raw != original:
                    raw.unlink(missing_ok=True)
        info = probe(final)
        if not info.get("has_video") or (info.get("duration") or 0) < 1.0:
            final.unlink(missing_ok=True)
            if original is not None:
                original.unlink(missing_ok=True)
            return None
        return {"name": final.name, "path": str(final), "duration": float(info["duration"]), "kind": item["kind"],
                "item": item, "w": info.get("width"), "h": info.get("height"), "size": final.stat().st_size,
                "original": str(original) if original is not None and original.exists() else None}
    except Exception as e:
        log.warning("Bỏ qua %s: %s", item.get("url"), e)
        final.unlink(missing_ok=True)
        if original is not None:
            original.unlink(missing_ok=True)
        return None


def _search_for(shot: dict, style: dict, vertical: bool, used: set[str], need: int) -> list[dict]:
    kinds = [shot["kind"], "photo" if shot["kind"] == "video" else "video"]
    found: list[dict] = []
    for kind in kinds:
        for q in shot["q"]:
            hint = style.get("hint", "").split(",")[0].strip() if kind == "video" else ""
            for query in ([q] + ([f"{q} {hint}"] if hint and hint not in q else []))[:1]:
                for it in stock_service.search(query, kind, vertical, style["sources"], style.get("photo_type", "photo"), n=6):
                    if it["id"] not in used and all(it["id"] != f["id"] for f in found):
                        found.append(it)
            if len(found) >= need + 1:
                return found
        if found:
            return found
    return found


def run_broll(pid: str, opts: dict):
    notes: list[str] = []
    try:
        style = styles.get(opts["style"])
        media = opts.get("media") or style["media"]
        vertical = style["aspect"] == "9:16"
        W, H = (1080, 1920) if vertical else (1920, 1080)
        with SessionLocal() as db:
            p = db.get(Project, pid)
            ps = dict(p.settings or {})
            ps["broll"] = {"style": style["id"], "media": media, "topic": opts.get("topic", ""),
                           "target_seconds": int(opts.get("target_seconds") or 0)}
            if opts.get("script") is not None and opts["script"].strip():
                ps["script"] = opts["script"].strip()
            p.settings = ps
            p.aspect_ratio = style["aspect"]
            db.commit()
            lang, topic = p.language or "vi", opts.get("topic") or p.title
            target = int(opts.get("target_seconds") or style.get("max_seconds") or 180)
            script = (ps.get("script") or "").strip()
            has_voice = db.query(Asset).filter(Asset.project_id == pid, Asset.asset_type == "voice",
                                               Asset.processing_status != "failed").count() > 0
            has_cues = db.query(SrtCue).filter_by(project_id=pid).count() > 0
            if not script and has_cues:      # đã có SRT: lời trong SRT chính là kịch bản -> không nhờ AI viết
                script = "\n".join(c.text for c in db.query(SrtCue).filter_by(project_id=pid).order_by(SrtCue.cue_index))
            pres = _presenter(db, pid) if style.get("needs_human") else None
            pres_info = {"id": pres.id, "name": pres.file_name, "path": pres.file_path, "duration": pres.duration_seconds or 0} if pres else None

        if has_voice:
            notes.append("Dùng file giọng đọc có sẵn — bỏ qua bước tạo giọng AI")
        if has_cues and has_voice:
            notes.append("Dùng file SRT có sẵn — bỏ qua bước nhận giọng nói")
            _step(pid, "Đã có giọng đọc + SRT → vào thẳng bước edit…")
        # 1) Kịch bản
        if not script and not has_voice:
            if not opts.get("topic"):
                raise ValueError("Nhập kịch bản, hoặc nhập chủ đề để AI viết kịch bản")
            _step(pid, "Claude đang viết kịch bản…")
            script = script_service.write_topic_script(opts["topic"], style, lang, target)
            with SessionLocal() as db:
                p = db.get(Project, pid)
                p.settings = {**(p.settings or {}), "script": script, "script_by_ai": True}
                db.commit()
            notes.append(f"AI đã viết kịch bản ({len(script):,} ký tự)")
            by_ai = True
        else:
            by_ai = False

        # 1b) DeepSeek kiểm tra kịch bản & kiểm chứng thông tin (nếu đã gắn API DeepSeek)
        if script and not has_voice and opts.get("factcheck", True) and factcheck_service.available():
            _step(pid, "DeepSeek đang kiểm tra kịch bản & kiểm chứng thông tin…")
            try:
                fc = factcheck_service.check_script(script, lang, style.get("group"), topic)
                bad = fc["counts"]["sai"] + fc["counts"]["chua_chac"]
                applied = bool(by_ai and bad and fc["corrected_script"])
                if applied:                  # kịch bản do AI viết -> sửa luôn; kịch bản của người dùng -> chỉ báo
                    script = fc["corrected_script"]
                with SessionLocal() as db:
                    p = db.get(Project, pid)
                    p.settings = {**(p.settings or {}), "factcheck": {**fc, "applied": applied},
                                  **({"script": script} if applied else {})}
                    db.commit()
                notes.append(f"DeepSeek kiểm chứng: điểm tin cậy {fc['score']}/100, {fc['counts']['sai']} chỗ sai, "
                             f"{fc['counts']['chua_chac']} chỗ chưa chắc" + (" — đã tự sửa kịch bản" if applied
                                                                            else (" — xem mục 🔎 Kiểm chứng để sửa" if bad else "")))
            except Exception as e:           # không chặn cả quy trình vì bước kiểm tra phụ
                notes.append(f"Không kiểm chứng được bằng DeepSeek: {friendly(e)[:160]}")

        # 2) Giọng đọc
        if not has_voice:
            if pres_info and probe(pres_info["path"]).get("has_audio"):
                _step(pid, "Đang tách giọng từ video người dẫn…")
                out = data_dir() / "projects" / pid / "assets" / f"voice_from_{pres_info['id'][:8]}.wav"
                run_ffmpeg(["-i", pres_info["path"], "-vn", "-ac", "1", "-ar", "44100", str(out)])
                with SessionLocal() as db:
                    db.add(Asset(project_id=pid, asset_type="voice", file_name=out.name, file_path=str(out),
                                 size_bytes=out.stat().st_size, duration_seconds=probe(out)["duration"],
                                 processing_status="ready", meta={"from_presenter": pres_info["id"]}))
                    db.commit()
                notes.append("Đã tách giọng đọc từ video người dẫn")
            elif _has_tts():
                prov = _has_tts()
                with SessionLocal() as db:
                    ps = dict(db.get(Project, pid).settings or {})
                voice_id = ps.get("voice_id") or ""
                if prov == "elevenlabs" and not voice_id:
                    from ..services import tts_service
                    vs = tts_service.list_voices("elevenlabs")
                    voice_id = vs[0]["id"] if vs else ""
                _step(pid, "Đang tạo giọng đọc AI + phụ đề…")
                tts_worker.generate_voice(pid, script, prov, voice_id or "alloy", ps.get("voice_style", "narrative"),
                                          float(ps.get("voice_speed") or 1.0), True, ps.get("voice_quality", "high"))
                has_cues = True
                notes.append("Đã tạo giọng đọc AI và phụ đề khớp từng câu")
            else:
                raise ValueError("Cần giọng đọc: thêm key ElevenLabs/OpenAI TTS ở Cài đặt, hoặc tải file voice-over lên")

        # 3) Phụ đề (SRT)
        with SessionLocal() as db:
            has_cues = db.query(SrtCue).filter_by(project_id=pid).count() > 0
            voice = (db.query(Asset).filter(Asset.project_id == pid, Asset.asset_type == "voice",
                                            Asset.processing_status != "failed").order_by(Asset.created_at.desc()).first())
            if voice is None:
                raise ValueError("Không có file giọng đọc dùng được (file voice-over lỗi?) — tải lại MP3/WAV ở Tài nguyên")
            vdur = voice.duration_seconds or 0
        if not has_cues:
            whisper_err, whisper_ok = None, False
            from ..services.whisper_service import can_transcribe
            if can_transcribe():
                _step(pid, "Đang tạo phụ đề từ giọng đọc…")
                try:
                    analyze_worker.transcribe_project(pid, None, fail_project=False, script=script or None,
                                                      progress=lambda f, t: _step(pid, t))
                    notes.append("Đã căn phụ đề theo kịch bản (chữ đúng kịch bản, thời gian theo giọng đọc)"
                                 if script else "Đã tạo phụ đề từ giọng đọc")
                    whisper_ok = True
                except Exception as e:      # Whisper lỗi -> không dừng cả quy trình
                    log.warning("Whisper lỗi: %s", e)
                    whisper_err = friendly(e)[:160]
            if whisper_ok:
                pass
            elif script and vdur:
                with SessionLocal() as db:
                    for c in script_service.estimate_cues(script, vdur):
                        db.add(SrtCue(project_id=pid, cue_index=c.index, start_seconds=c.start, end_seconds=c.end,
                                      text=c.text, language=lang))
                    db.commit()
                notes.append(("Tạo phụ đề lỗi (" + whisper_err + ") — " if whisper_err else "")
                             + "Phụ đề ước lượng theo kịch bản (nên kiểm tra ở mục Kịch bản)")
            else:
                raise ValueError(("Không tạo được phụ đề từ giọng đọc: " + whisper_err + ". " if whisper_err else "")
                                 + "Hãy nạp file SRT ở mục Kịch bản, hoặc dán kịch bản để app ước lượng phụ đề")
        with SessionLocal() as db:
            cues = [Cue(index=c.cue_index, start=c.start_seconds, end=c.end_seconds, text=c.text)
                    for c in db.query(SrtCue).filter_by(project_id=pid).order_by(SrtCue.cue_index)]
        if style.get("max_seconds") and cues and cues[-1].end > style["max_seconds"] + 2:
            notes.append(f"Lưu ý: video dài {cues[-1].end:.0f}s, phong cách này nên dưới {style['max_seconds']}s")

        # 4) Claude chọn hình cho từng câu
        _step(pid, f"Claude đang chọn hình minh hoạ cho {len(cues)} câu…")
        shots = broll_service.plan_queries(cues, style, media, topic)

        # 5) Tìm & tải (lưu vào Thư mục đầu vào / Tài nguyên AI / <dự án> để bạn xem & dùng lại)
        try:
            with SessionLocal() as db:
                keep_dir = folders.ai_dir(db.get(Project, pid).title)
        except OSError:
            keep_dir = None
        used_ids: set[str] = set()
        missing: dict[int, dict] = {}                 # câu kho miễn phí không có hình hợp -> nhờ người dùng tìm/vẽ
        media_for_cue: dict[int, list[dict]] = {}
        fetched: dict[str, dict] = {}
        prev_pool: list[dict] | None = None
        pres_cues = {c.index for n, c in enumerate(cues) if pres_info and n % 3 == 0 and c.end <= pres_info["duration"] + 0.05}
        with SessionLocal() as db:                    # hình bạn đã tự gắn cho từng câu -> giữ nguyên, không tìm lại
            user_fill = {a.meta["user_fill"]: {"name": a.file_name, "duration": float(a.duration_seconds or 0),
                                               "kind": a.meta.get("stock_kind") or "video"}
                         for a in db.query(Asset).filter_by(project_id=pid, asset_type="broll").order_by(Asset.created_at)
                         if (a.meta or {}).get("user_fill") and Path(a.file_path).exists()}
        todo = [c for c in cues if c.index not in pres_cues]
        with ThreadPoolExecutor(max_workers=4) as ex:
            for k, c in enumerate(todo, 1):
                _step(pid, f"Đang tìm & tải hình minh hoạ {k}/{len(todo)}…")
                if c.index in user_fill:
                    media_for_cue[c.index] = prev_pool = [user_fill[c.index]]
                    continue
                shot = shots.get(c.index) or {"q": [topic], "kind": "video", "same_as_prev": True}
                need = broll_service.shots_needed(c, float(style.get("pace") or 4))
                if shot.get("same_as_prev") and prev_pool:
                    media_for_cue[c.index] = prev_pool
                    continue
                cands = _search_for(shot, style, vertical, used_ids, need)
                results = [r for r in ex.map(lambda it: _fetch_one(pid, it, W, H, (c.end - c.start) / need + 1, keep_dir),
                                             cands[:need + 1]) if r]
                for r in results:
                    used_ids.add(r["item"]["id"])
                    fetched[r["name"]] = r
                if not results:                                   # không tìm được: tạm dùng hình gần nhất, ghi lại để nhờ tìm
                    missing[c.index] = shot
                    if prev_pool:
                        results = prev_pool
                    else:
                        continue
                media_for_cue[c.index] = results
                prev_pool = results
        def _req(c, need, reason):
            return {"cue": c.index, "start": c.start, "end": c.end, "text": c.text, "need": need, "reason": reason,
                    "q": (shots.get(c.index) or {}).get("q") or []}
        if not media_for_cue:                         # kho không có gì dùng được -> nhờ người dùng tìm cả, báo trong chat
            from . import media_worker
            media_worker.post_requests(pid, [_req(c, "draw" if style.get("photo_type") == "illustration" else "find",
                                                  "kho miễn phí không có") for c in todo], {**style, "id": opts["style"]}, topic)
            raise ValueError("Kho ảnh/video miễn phí không có hình hợp với kịch bản này — mình đã liệt kê hình cần tìm/vẽ "
                             "trong khung chat. Hoặc thêm key Pexels/Pixabay ở ⚙ Cài đặt.")
        first = next(media_for_cue[c.index] for c in todo if c.index in media_for_cue)
        last_pool = first
        for c in todo:                                # câu đầu chưa có hình -> tạm dùng hình tìm được đầu tiên
            if c.index in media_for_cue:
                last_pool = media_for_cue[c.index]
            else:
                media_for_cue[c.index] = last_pool
        notes.append(f"Đã tải {len(fetched)} ảnh/video minh hoạ từ kho miễn phí bản quyền"
                     + (f"; {len(missing)} câu chưa có hình hợp (tạm dùng hình gần nhất)" if missing else ""))

        # 6) Lắp bản dựng + lưu tài nguyên
        _step(pid, "Đang lắp bản dựng theo phong cách…")
        presenter = {"name": pres_info["name"], "duration": pres_info["duration"]} if pres_info else None
        plan, scenes = broll_service.build_plan(cues, media_for_cue, style, lang, presenter)
        with SessionLocal() as db:
            p = db.get(Project, pid)
            existing = {a.file_name: a for a in db.query(Asset).filter_by(project_id=pid)}
            for name, sc in scenes.items():
                a = existing.get(name)
                if a is None:
                    r = fetched[name]
                    a = Asset(project_id=pid, asset_type="broll", file_name=name, file_path=r["path"], size_bytes=r["size"],
                              duration_seconds=r["duration"], width=r["w"], height=r["h"], processing_status="ready",
                              meta={"stock": r["item"], "stock_kind": r["kind"], "original": r.get("original")})
                    db.add(a)
                a.meta = {**(a.meta or {}), "scenes": sc}
            # hình tải từ lần chạy trước nhưng không dùng nữa -> xoá cho nhẹ ổ đĩa
            for name, a in existing.items():
                if (a.meta or {}).get("stock") and name not in scenes:
                    Path(a.file_path).unlink(missing_ok=True)
                    if (a.meta or {}).get("original"):
                        Path(a.meta["original"]).unlink(missing_ok=True)
                    db.delete(a)
            # tải về lần này nhưng bản dựng không dùng tới -> xoá luôn (trước đây bị sót lại trên ổ đĩa)
            for name, r in fetched.items():
                if name not in scenes and name not in existing:
                    Path(r["path"]).unlink(missing_ok=True)
                    if r.get("original"):
                        Path(r["original"]).unlink(missing_ok=True)
            db.flush()
            ctx = build_context(db, p)
            plan, _ = repair_plan(plan, ctx)
            _, issues = validate_plan(plan, ctx)
            version = db.query(EditPlanRow).filter_by(project_id=pid).count() + 1
            db.add(EditPlanRow(project_id=pid, version=version, plan=plan, validation_status="valid" if not issues else "needs_manual_review",
                               validation_errors=[str(i) for i in issues]))
            ro = dict((p.settings or {}).get("render_opts") or {})
            ro.update(aspect_ratio=style["aspect"], burn_subtitles=True, subtitle_size=style.get("subtitle_size", 54))
            items = [ (a.meta or {}).get("stock") for a in db.query(Asset).filter_by(project_id=pid) if (a.meta or {}).get("stock")]
            p.settings = {**(p.settings or {}), "render_opts": ro, "stock_credits": stock_service.credits_text([i for i in items if i])}
            db.commit()
        if issues:
            notes.append(f"Bản dựng còn {len(issues)} lỗi nhỏ — mở mục Bản dựng hoặc nhờ Chat AI sửa")
        if any((i or {}).get("source") == "wikimedia" for i in items):
            notes.append("Có ảnh Wikimedia: nhớ dán danh sách nguồn (nút “Nguồn hình”) vào mô tả video")
        # 6b) Nhạc nền: Claude chọn trong kho Nhạc nền / nhạc miễn phí bản quyền
        # 6a) Hình còn thiếu / cần vẽ -> Claude liệt kê trong khung chat (cần tìm + prompt vẽ), bạn gắn file vào từng câu
        draw = style.get("photo_type") == "illustration"
        req_items = [_req(c, "draw" if draw else "find", "kho miễn phí không có hình hợp" if c.index in missing
                          else "phong cách cần ảnh vẽ") for c in todo
                     if (draw or c.index in missing) and c.index not in user_fill]
        if req_items:
            from . import media_worker
            _step(pid, "Claude đang liệt kê hình cần tìm / viết prompt vẽ…")
            n = media_worker.post_requests(pid, req_items, {**style, "id": opts["style"]}, topic)
            notes.append(f"Đã gửi vào chat {n} " + ("prompt vẽ cho từng câu" if draw else "câu cần bạn tìm thêm hình"))
        else:
            chat_log.update_settings(pid, {}, drop=("media_requests",))
        auto_metadata(pid, notes)
        if opts.get("music", True):
            auto_music(pid, notes)

        # 7) Xong + dựng video luôn (True = xem thử 720p; "final" = thành phẩm 1080p vào thư mục đầu ra)
        r = opts.get("render")
        _finish(pid, None, notes, (r if r in ("preview", "final") else "preview") if r and not issues else None)
    except Exception as e:
        log.exception("Kịch bản -> Video lỗi")
        _finish(pid, friendly(e) if not isinstance(e, ValueError) else str(e), notes)
