from collections import Counter
from dataclasses import dataclass

from pydantic import ValidationError

from .. import config as C
from ..schemas import EditPlan, Footage, PlanContext


@dataclass
class Issue:
    code: str
    message: str
    srt_index: int | None = None

    def __str__(self):
        where = f"[cue {self.srt_index}] " if self.srt_index is not None else ""
        return f"{self.code}: {where}{self.message}"


def format_issues(issues: list[Issue]) -> str:
    return "\n".join(f"- {i}" for i in issues)


def scene_index(f: Footage, start: float, end: float) -> int | None:
    """Vị trí (0-based) của scene chứa trọn [start, end], hoặc None."""
    return next((k for k, s in enumerate(f.scenes)
                 if s.start - C.SCENE_EPS <= start and end <= s.end + C.SCENE_EPS), None)


def collect_usage(plan: EditPlan, ctx: PlanContext) -> tuple[Counter, set[str]]:
    """Đếm scene đã dùng và filter đã dùng (để truyền sang batch kế tiếp)."""
    footage = {f.file: f for f in ctx.available_footage}
    use: Counter = Counter()
    filters: set[str] = set()
    for sec in plan.sections:
        for v in sec.visuals:
            f = footage.get(v.footage)
            if f is not None:
                idx = scene_index(f, v.start, v.end)
                if idx is not None:
                    use[(v.footage, idx)] += 1
            if v.filter:
                filters.add(v.filter)
    return use, filters


def validate_plan(plan_data: dict, ctx: PlanContext, prior_scene_use: Counter | None = None,
                  prior_filters: set[str] | None = None) -> tuple[EditPlan | None, list[Issue]]:
    """Trả về (plan đã parse | None, danh sách lỗi). Danh sách rỗng = hợp lệ.

    prior_* là phần đã dùng ở các batch trước khi plan được sinh theo từng đoạn.
    """
    try:
        plan = EditPlan.model_validate(plan_data)
    except ValidationError as e:
        return None, [Issue("SCHEMA", f"{'.'.join(map(str, err['loc']))}: {err['msg']}") for err in e.errors()]

    issues: list[Issue] = []
    cues = {c.index: c for c in ctx.srt_cues}
    footage = {f.file: f for f in ctx.available_footage}
    seen_cues: list[int] = []
    scene_use: Counter = Counter(prior_scene_use or {})
    filters_used: set[str] = set(prior_filters or ())
    total = 0.0

    for sec in plan.sections:
        i = sec.srt_index
        seen_cues.append(i)
        cue = cues.get(i)
        if cue is None:
            issues.append(Issue("UNKNOWN_CUE", "srt_index không tồn tại trong SRT", i))
            continue

        if abs(sec.voice_start - cue.start) > C.CUE_TIME_TOLERANCE or abs(sec.voice_end - cue.end) > C.CUE_TIME_TOLERANCE:
            issues.append(Issue("VOICE_TIME_MISMATCH",
                f"voice_start/end phải là {cue.start}/{cue.end}, nhận {sec.voice_start}/{sec.voice_end}", i))

        sec_duration = 0.0
        for v in sec.visuals:
            sec_duration += v.timeline_duration
            f = footage.get(v.footage)
            if f is None:
                issues.append(Issue("UNKNOWN_FOOTAGE", f"'{v.footage}' không có trong danh sách footage", i))
                continue

            idx = scene_index(f, v.start, v.end)
            if idx is None:
                issues.append(Issue("OUT_OF_SCENE",
                    f"{v.footage} {v.start}-{v.end} không nằm trọn trong một scene đã detect", i))
            else:
                scene_use[(v.footage, idx)] += 1
                if f.scenes[idx].spoiler and not ctx.allow_spoilers:
                    issues.append(Issue("SPOILER", f"scene {idx} của {v.footage} bị gắn nhãn spoiler", i))

            lo, hi = C.VISUAL_DURATION_RANGE
            if not lo - 1e-6 <= v.source_duration <= hi + 1e-6:
                issues.append(Issue("CLIP_LENGTH", f"clip dài {v.source_duration:.2f}s, cho phép {lo}-{hi}s", i))
            if v.filter:
                filters_used.add(v.filter)
                if v.filter not in ctx.available_filters:
                    issues.append(Issue("UNKNOWN_FILTER", f"filter '{v.filter}' không khả dụng", i))

        cue_len = cue.end - cue.start
        if abs(sec_duration - cue_len) > C.SECTION_DURATION_TOLERANCE:
            issues.append(Issue("SECTION_DURATION",
                f"tổng visual {sec_duration:.2f}s lệch cue {cue_len:.2f}s quá {C.SECTION_DURATION_TOLERANCE}s", i))
        total += sec_duration

    for (file, idx), n in scene_use.items():
        if n > C.MAX_SCENE_REUSE:
            issues.append(Issue("SCENE_REUSED", f"scene {idx} của {file} dùng {n} lần, tối đa {C.MAX_SCENE_REUSE}"))

    if len(filters_used) > C.MAX_FILTERS:
        issues.append(Issue("TOO_MANY_FILTERS", f"dùng {len(filters_used)} filter ({sorted(filters_used)}), tối đa {C.MAX_FILTERS}"))

    missing = sorted(set(cues) - set(seen_cues))
    if missing:
        issues.append(Issue("MISSING_CUES", f"thiếu section cho cue: {missing}"))
    dup = sorted(k for k, n in Counter(seen_cues).items() if n > 1)
    if dup:
        issues.append(Issue("DUPLICATE_CUES", f"cue xuất hiện nhiều lần: {dup}"))
    if seen_cues != sorted(seen_cues):
        issues.append(Issue("ORDER", "sections phải theo thứ tự srt_index tăng dần"))

    # So với tổng độ dài các cue (khoảng lặng giữa cue được renderer tự kéo giãn).
    expected = sum(c.end - c.start for c in ctx.srt_cues)
    if abs(total - expected) > C.TOTAL_DURATION_TOLERANCE and not missing:
        issues.append(Issue("TOTAL_DURATION",
            f"tổng visual {total:.1f}s lệch tổng thời lượng cue {expected:.1f}s quá {C.TOTAL_DURATION_TOLERANCE}s"))

    return plan, issues
