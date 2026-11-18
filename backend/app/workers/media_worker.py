"""Hình còn thiếu (chế độ Kịch bản → Video): Claude liệt kê trong khung chat những câu cần bạn TỰ TÌM hình/video
hoặc cần VẼ (kèm prompt), rồi bạn gắn file vào đúng câu đó -> app thay hình, kiểm tra và dựng lại ngầm.

project.settings["media_requests"] = {"style", "topic", "items": [{cue, start, end, text, need: find|draw, reason, what,
                                      keywords, where, prompt, status: open|filled, file}]}
"""
import logging
import shutil
import time
from pathlib import Path

from ..database import SessionLocal
from ..models import Asset, EditPlanRow, Project, SrtCue
from ..schemas import Cue
from ..services import broll_service, chat_log, stock_service, styles
from ..services.ffmpeg_service import probe, run_ffmpeg
from ..services.render_service import RES
from ..utils.paths import data_dir

log = logging.getLogger("mre.media")

IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}
VIDEO_EXT = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v"}
MAX_ITEMS = 80


def _fmt(t: float) -> str:
    return f"{int(t // 60)}:{int(t % 60):02d}"


def post_requests(pid: str, items: list[dict], style: dict, topic: str) -> int:
    """items: [{cue,start,end,text,need,reason,q}] -> Claude viết mô tả/từ khoá/prompt -> lưu + nhắn vào chat."""
    if not items:
        return 0
    items = items[:MAX_ITEMS]
    from ..services import chat_service
    try:
        d = chat_service.media_requests(items, style, topic)
    except Exception as e:                       # Claude lỗi -> vẫn báo danh sách từ khoá đã thử
        log.warning("media_requests lỗi: %s", e)
        d = {"intro": "", "items": [{**i, "what": i["text"], "keywords": i.get("q") or [], "where": "", "prompt": ""}
                                    for i in items]}
    reqs = [{**i, "status": "open", "file": None} for i in d["items"]]
    n_draw = sum(1 for r in reqs if r["need"] == "draw")
    n_find = len(reqs) - n_draw
    intro = d.get("intro") or (
        (f"Phong cách “{style.get('label')}” cần ảnh vẽ: mình đã viết {n_draw} prompt cho từng câu. " if n_draw else "")
        + (f"Kho ảnh miễn phí chưa có hình hợp cho {n_find} câu, mình tạm dùng hình gần nhất. " if n_find else "")
        + "Bạn tìm/vẽ xong thì bấm 📎 Gắn file ở đúng câu — mình sẽ thay hình và dựng lại ngay.")
    chat_log.update_settings(pid, {"media_requests": {"style": style.get("id"), "topic": topic, "items": reqs, "at": chat_log.now()}})
    chat_log.append(pid, {"role": "assistant", "kind": "media_request", "text": intro,
                          "counts": {"draw": n_draw, "find": n_find}}, keep_progress=True)
    return len(reqs)


def update_request(pid: str, cue: int, **fields) -> dict | None:
    with SessionLocal() as db:
        st = (db.get(Project, pid).settings or {}).get("media_requests") or {}
    items = st.get("items") or []
    hit = None
    for it in items:
        if it["cue"] == cue:
            it.update({k: v for k, v in fields.items() if v is not None})
            hit = it
    if hit is not None:
        chat_log.update_settings(pid, {"media_requests": {**st, "items": items}})
    return hit


def _style_for(ps: dict) -> dict:
    sid = (ps.get("broll") or {}).get("style") or "fin_faceless"
    try:
        return {**styles.get(sid), "id": sid}
    except (ValueError, KeyError, TypeError):
        return {**styles.get("fin_faceless"), "id": "fin_faceless"}


def fill(pid: str, cue_index: int, src: str) -> dict:
    """Gắn file người dùng tìm/vẽ vào câu cue_index: chuẩn hoá -> thay hình của câu đó trong bản dựng -> lưu phiên bản mới.
    Trả {"version", "valid", "issues", "file"}. Bên gọi xếp hàng dựng lại."""
    from ..services.context_service import build_context
    from ..services.validator_service import validate_plan
    srcp = Path(src)
    ext = srcp.suffix.lower()
    if ext not in IMAGE_EXT | VIDEO_EXT:
        raise ValueError("Chỉ nhận ảnh (jpg, png, webp) hoặc video (mp4, mov, mkv, webm)")
    with SessionLocal() as db:
        p = db.get(Project, pid)
        ps = dict(p.settings or {})
        cue = db.query(SrtCue).filter_by(project_id=pid, cue_index=cue_index).first()
        if cue is None:
            raise ValueError(f"Không có câu phụ đề số {cue_index}")
        row = db.query(EditPlanRow).filter_by(project_id=pid).order_by(EditPlanRow.version.desc()).first()
        if row is None:
            raise ValueError("Chưa có bản dựng — bấm ⚡ Tự động edit trước")
        c = Cue(index=cue.cue_index, start=cue.start_seconds, end=cue.end_seconds, text=cue.text)
        W, H = RES[(p.aspect_ratio or "16:9", "final")]
        title = p.title
    style = _style_for(ps)
    folder = data_dir() / "projects" / pid / "assets"
    folder.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%H%M%S")
    name = f"cau{cue_index:03d}_{stamp}_{srcp.stem[:40]}.mp4".replace(" ", "_")
    out = folder / name
    photo = ext in IMAGE_EXT
    if photo:
        stock_service.photo_to_clip(srcp, out, W, H)
    else:
        secs = min(max(c.end - c.start + 2, 10.0), 60.0)
        try:
            stock_service.trim_video(srcp, out, secs)
            if not probe(out).get("has_video"):
                raise RuntimeError("copy failed")
        except Exception:                          # mã hoá lạ (VP8, ProRes…) không chép thẳng vào MP4 được -> mã hoá lại
            out.unlink(missing_ok=True)
            run_ffmpeg(["-i", str(srcp), "-t", f"{secs:.2f}", "-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
                        "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(out)])
    info = probe(out)
    if not info.get("has_video") or (info.get("duration") or 0) < 1:
        out.unlink(missing_ok=True)
        raise ValueError("Không đọc được hình/video này")
    keep = None
    try:                                           # lưu bản gốc vào Thư mục đầu vào / Tài nguyên AI / <dự án>
        from ..services import folders
        keep = folders.ai_dir(title) / f"cau{cue_index:03d}_{srcp.name}"
        if not keep.exists() and srcp.resolve() != keep.resolve():
            shutil.copy2(srcp, keep)
    except Exception:
        keep = None
    one, scenes = broll_service.build_plan([c], {c.index: [{"name": name, "duration": float(info["duration"]),
                                                             "kind": "photo" if photo else "video"}]}, style)
    new_sec = one["sections"][0]
    with SessionLocal() as db:
        db.add(Asset(project_id=pid, asset_type="broll", file_name=name, file_path=str(out), size_bytes=out.stat().st_size,
                     duration_seconds=float(info["duration"]), width=info.get("width"), height=info.get("height"),
                     processing_status="ready",
                     meta={"user_fill": cue_index, "stock_kind": "photo" if photo else "video", "scenes": scenes.get(name, []),
                           "original": str(keep) if keep else str(srcp)}))
        db.commit()
        plan = broll_service.replace_cue_visuals(dict(row.plan), cue_index, new_sec["visuals"])
        p = db.get(Project, pid)
        ctx = build_context(db, p)
        _, issues = validate_plan(plan, ctx)
        nv = EditPlanRow(project_id=pid, version=db.query(EditPlanRow).filter_by(project_id=pid).count() + 1, plan=plan,
                         validation_status="invalid" if issues else "valid", validation_errors=[str(x) for x in issues])
        db.add(nv)
        if not issues and p.status not in ("rendering", "analyzing"):
            p.status = "plan_ready"
        db.commit()
        version = nv.version
    update_request(pid, cue_index, status="filled", file=srcp.name)
    chat_log.append(pid, {"role": "assistant", "kind": "summary",
                          "text": f"✓ Đã gắn “{srcp.name}” vào câu {cue_index} ({_fmt(c.start)}–{_fmt(c.end)})"
                                  + ("" if not issues else f" — bản dựng còn {len(issues)} lỗi, xem tab Bản dựng")})
    return {"version": version, "valid": not issues, "issues": [str(x) for x in issues], "file": name}
