"""Tự sửa các lỗi cơ học của edit plan TRƯỚC khi gọi lại Claude (tiết kiệm API và thời gian).

Chỉ sửa những gì có đáp án xác định: kẹp giá trị vào giới hạn, đưa clip vào đúng scene, chốt
voice_start/end và subtitle theo cue, co/giãn clip cuối để khớp độ dài cue. Những lỗi cần phán đoán
(thiếu cue, cảnh bị lặp, spoiler, footage không tồn tại) vẫn để validator báo và Claude sửa.
"""
from copy import deepcopy

from .. import config as C
from ..schemas import PlanContext
from .validator_service import scene_index

_PURPOSES = {"hook", "plot", "acting", "cinematography", "music", "theme", "verdict", "cta"}
_TRANSITIONS = {"none", "flash_cut", "fade", "cross_dissolve", "whip_pan", "shake"}
_NUM = {"zoom_start": C.ZOOM_RANGE, "zoom_end": C.ZOOM_RANGE, "speed": C.SPEED_RANGE,
        "brightness": C.BRIGHTNESS_RANGE, "contrast": C.CONTRAST_RANGE, "saturation": C.SATURATION_RANGE}
LO, HI = C.VISUAL_DURATION_RANGE


def _num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _fit_into_scene(f, start: float, end: float):
    """Đưa [start, end] vào một scene thật và giữ độ dài trong 1-8s. Trả (start, end, changed)."""
    if not f.scenes:
        return start, end, False
    idx = scene_index(f, start, end)
    changed = False
    if idx is None:
        overlap = lambda s: max(0.0, min(end, s.end) - max(start, s.start))
        best = max(f.scenes, key=overlap)
        if overlap(best) > 0:
            sc, (start, end) = best, (max(start, best.start), min(end, best.end))
        else:  # timestamp rơi vào khoảng hở giữa các scene: lấy scene gần nhất
            mid = (start + end) / 2
            sc = min(f.scenes, key=lambda s: abs((s.start + s.end) / 2 - mid))
            start, end = sc.start, min(sc.end, sc.start + min(max(end - start, LO), HI))
        changed = True
    else:
        sc = f.scenes[idx]
    if end - start > HI:
        end, changed = start + HI, True
    if end - start < LO:
        end = min(sc.end, start + LO)
        if end - start < LO:
            start = max(sc.start, end - LO)
        changed = True
    return round(start, 3), round(end, 3), changed


def _repair_visual(v: dict, footage: dict, allowed_filters: set, notes: list[str]):
    for k, (lo, hi) in _NUM.items():
        if k in v:
            x = _num(v[k])
            if x is None:
                v.pop(k)
                notes.append(f"bỏ {k} không phải số")
                continue
            y = min(max(x, lo), hi)
            if abs(x - y) > 1e-9:
                v[k] = y
                notes.append(f"{k} {x}→{y}")
    for k in ("transition_in", "transition_out"):
        if k in v and v[k] not in _TRANSITIONS:
            v[k] = "none"
            notes.append(f"{k} không hợp lệ → none")
    if v.get("filter") and v["filter"] not in allowed_filters:
        notes.append(f"bỏ filter lạ '{v['filter']}'")
        v["filter"] = None
    f, s, e = footage.get(v.get("footage")), _num(v.get("start")), _num(v.get("end"))
    if f is None or s is None or e is None or e <= s:
        return
    ns, ne, changed = _fit_into_scene(f, s, e)
    if changed:
        v["start"], v["end"] = ns, ne
        notes.append(f"{v['footage']} {s}-{e} → {ns}-{ne}")


def _fit_duration(sec: dict, cue, footage: dict, notes: list[str]):
    """Co/giãn clip cuối của section (đổi end, rồi speed nếu cần) để tổng khớp độ dài cue."""
    vis = [v for v in sec.get("visuals", []) if isinstance(v, dict) and _num(v.get("start")) is not None
           and _num(v.get("end")) is not None and (_num(v.get("speed", 1.0)) or 0) > 0]
    if not vis:
        return
    tl = lambda v: (float(v["end"]) - float(v["start"])) / float(v.get("speed", 1.0))
    need = (cue.end - cue.start) - sum(tl(v) for v in vis[:-1])
    last = vis[-1]
    if need < 0.2 or abs(tl(last) - need) <= 0.05:
        return          # clip trước đã quá dài hoặc đã khớp
    speed, start = float(last.get("speed", 1.0)), float(last["start"])
    f = footage.get(last.get("footage"))
    sc = next((s for s in f.scenes if s.start - C.SCENE_EPS <= start < s.end), None) if f else None
    hi_end = min(start + HI, sc.end if sc else float("inf"))
    end = max(min(start + need * speed, hi_end), min(hi_end, start + LO))
    if abs(end - float(last["end"])) > 1e-6:
        last["end"] = round(end, 3)
        notes.append(f"cue {sec.get('srt_index')}: end → {last['end']}")
    if abs((end - start) / speed - need) > 0.05:
        new_speed = min(max((end - start) / need, C.SPEED_RANGE[0]), C.SPEED_RANGE[1])
        last["speed"] = round(new_speed, 4)
        notes.append(f"cue {sec.get('srt_index')}: speed → {last['speed']} để khớp lời")


def repair_plan(raw: dict, ctx: PlanContext) -> tuple[dict, list[str]]:
    """Trả về (plan đã sửa, danh sách việc đã sửa). Không bao giờ ném lỗi: plan hỏng cấu trúc giữ nguyên."""
    try:
        plan = deepcopy(raw)
        sections = plan["sections"]
        assert isinstance(sections, list)
    except Exception:
        return raw, []
    notes: list[str] = []
    cues = {c.index: c for c in ctx.srt_cues}
    footage = {f.file: f for f in ctx.available_footage}
    allowed = set(ctx.available_filters)

    seen, clean = set(), []
    for s in sorted((s for s in sections if isinstance(s, dict) and s.get("srt_index") in cues),
                    key=lambda s: s["srt_index"]):
        if s["srt_index"] in seen:
            notes.append(f"bỏ section trùng cue {s['srt_index']}")
            continue
        seen.add(s["srt_index"])
        clean.append(s)
    if len(clean) != len(sections):
        notes.append("loại section sai cue / sắp xếp lại")

    for s in clean:
        cue = cues[s["srt_index"]]
        if s.get("voice_start") != cue.start or s.get("voice_end") != cue.end:
            s["voice_start"], s["voice_end"] = cue.start, cue.end
            notes.append(f"cue {cue.index}: chốt voice_start/end")
        s["subtitle"] = cue.text
        if s.get("purpose") not in _PURPOSES:
            s["purpose"] = "plot"
        if isinstance(s.get("visuals"), list):
            for v in s["visuals"]:
                if isinstance(v, dict):
                    _repair_visual(v, footage, allowed, notes)
            _fit_duration(s, cue, footage, notes)
    plan["sections"] = clean
    return plan, notes
