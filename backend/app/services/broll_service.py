"""KỊCH BẢN -> VIDEO: Claude chọn hình minh hoạ cho từng câu thoại, app lắp thành bản dựng khớp giọng đọc.

1) plan_queries: mỗi câu (cue SRT) -> từ khoá tìm kiếm tiếng Anh + nên dùng ảnh hay video (+ có giữ cảnh câu trước không).
2) build_plan: chia mỗi câu thành các cảnh theo nhịp của phong cách, gán ảnh/video đã tải, zoom/pan, chuyển cảnh, màu.
   Kết quả là EditPlan chuẩn của app -> dùng chung bộ kiểm tra, xem trước, chat AI chỉnh sửa và xuất video.
"""
import json
import math

from ..config import settings
from ..schemas import Cue
from .claude_client import get_client

TOOL = {
    "name": "plan_broll",
    "description": "Chọn hình minh hoạ cho từng câu thoại của video.",
    "input_schema": {"type": "object", "properties": {"shots": {"type": "array", "items": {"type": "object", "properties": {
        "i": {"type": "integer", "description": "số thứ tự câu (cue index)"},
        "q": {"type": "array", "items": {"type": "string"}, "description": "2-3 từ khoá TIẾNG ANH để tìm ảnh/video, cụ thể -> chung chung"},
        "kind": {"type": "string", "enum": ["video", "photo"]},
        "same_as_prev": {"type": "boolean", "description": "true nếu câu này tiếp tục ý câu trước, nên giữ cùng hình"},
    }, "required": ["i", "q", "kind"]}}}, "required": ["shots"]},
}

SYSTEM = """Bạn là biên tập viên hình ảnh cho kênh video {group}. Phong cách: {label}.
Với MỖI câu thoại, chọn hình minh hoạ để người xem hiểu ngay nội dung câu đó.
- q: 2-3 cụm từ khoá TIẾNG ANH ngắn (2-4 từ) để tìm trên kho ảnh/video miễn phí (Pexels, Pixabay, Wikimedia Commons).
  Cụm đầu cụ thể nhất (tên địa danh, sự vật, hành động), cụm cuối chung chung để chắc chắn có kết quả.
  Kho ảnh không có ảnh người nổi tiếng/sự kiện cụ thể gần đây: dùng hình tượng trưng (vd "stock market screen",
  "parliament building", "ancient battle painting"). Lịch sử: thêm "painting", "archive", "map", tên triều đại/thời kỳ.
- kind: {kind_rule}
- same_as_prev: true nếu câu chỉ nói tiếp ý câu trước (giữ hình liền mạch), không lạm dụng.
Gợi ý chủ đề hình: {hint}. Gọi tool plan_broll với đủ mọi câu."""


def plan_queries(cues: list[Cue], style: dict, media: str, topic: str = "", client=None, batch: int = 120) -> dict[int, dict]:
    client = client or get_client()
    kind_rule = {"photo": "luôn là \"photo\"", "video": "luôn là \"video\""}.get(
        media, "\"video\" cho hành động/chuyển động/khung cảnh; \"photo\" cho chân dung, bản đồ, tranh, tư liệu, số liệu")
    from .styles import GROUPS
    system = SYSTEM.format(group=GROUPS[style["group"]], label=style["label"], kind_rule=kind_rule,
                           hint=style.get("hint") or "theo nội dung câu")
    out: dict[int, dict] = {}
    for k in range(0, len(cues), batch):
        part = cues[k:k + batch]
        body = {"topic": topic, "cues": [{"i": c.index, "text": c.text} for c in part]}
        resp = client.messages.create(model=settings.claude_fast_model or settings.claude_model, max_tokens=8000,
                                      system=system, tools=[TOOL], tool_choice={"type": "tool", "name": TOOL["name"]},
                                      messages=[{"role": "user", "content": json.dumps(body, ensure_ascii=False)}])
        block = next((b for b in resp.content if b.type == "tool_use"), None)
        for s in ((block.input or {}).get("shots") or []) if block else []:
            try:
                q = [x.strip() for x in s.get("q") or [] if x and x.strip()][:3]
                if q:
                    out[int(s["i"])] = {"q": q, "kind": s.get("kind") if s.get("kind") in ("video", "photo") else "video",
                                        "same_as_prev": bool(s.get("same_as_prev"))}
            except (KeyError, TypeError, ValueError):
                continue
    # câu bị sót -> dùng lại từ khoá câu gần nhất
    last = None
    for c in cues:
        if c.index in out:
            last = out[c.index]
        elif last:
            out[c.index] = {**last, "same_as_prev": True}
    if media in ("photo", "video"):
        for v in out.values():
            v["kind"] = media
    return out


def shots_needed(cue: Cue, pace: float) -> int:
    """Số cảnh cho một câu: theo nhịp phong cách (cho phép dài hơn nhịp một chút), nhưng mỗi cảnh không quá 7.5 giây."""
    d = max(0.05, cue.end - cue.start)
    return max(1, math.ceil(d / 7.5), math.ceil(d / min(max(pace, 1.5), 7.5) - 0.25))


class _Clip:
    """Một file ảnh-clip/video đã tải; cấp các đoạn nguồn khác nhau (mỗi đoạn dùng tối đa 2 lần)."""

    def __init__(self, name: str, duration: float, kind: str):
        self.name, self.duration, self.kind, self.cursor = name, duration, kind, 0.0
        self.scenes: list[list[float]] = []
        self.uses: dict[tuple[float, float], int] = {}

    def take(self, need: float) -> tuple[float, float, float]:
        """Trả (start, end, speed) sao cho (end-start)/speed = need. Clip ngắn hơn nhu cầu -> làm chậm (tối thiểu 0.5x)."""
        dur = max(1.0, self.duration - 0.05)
        need_src = min(need, 8.0, dur)
        need_src = max(need_src, min(1.0, dur), need * 0.5)       # speed = need_src/need >= 0.5
        need_src = min(need_src, dur, 8.0)
        slack = max(0.0, dur - need_src)
        starts = [self.cursor] if self.cursor + need_src <= dur + 1e-6 else []
        starts += [round(k * 0.25, 3) for k in range(int(slack / 0.25) + 1)]
        pick = None
        for st in starts:
            rng = (round(st, 3), round(st + need_src, 3))
            if self.uses.get(rng, 0) < 2:
                pick = rng
                break
        pick = pick or (round(starts[0], 3) if starts else 0.0, round((starts[0] if starts else 0.0) + need_src, 3))
        self.uses[pick] = self.uses.get(pick, 0) + 1
        self.cursor = pick[1]
        self.scenes.append(list(pick))
        return pick[0], pick[1], round((pick[1] - pick[0]) / need, 4) if need > 0 else 1.0


def build_plan(cues: list[Cue], media_for_cue: dict[int, list[dict]], style: dict, language: str = "vi",
               presenter: dict | None = None) -> tuple[dict, dict[str, list[list[float]]]]:
    """Trả (plan dict, scenes cho từng file). media_for_cue[i] = [{"name","duration","kind"}...] theo thứ tự ưu tiên.
    presenter = {"name","duration"}: video người dẫn quay cùng giọng đọc -> xen kẽ, khớp đúng thời điểm."""
    clips: dict[str, _Clip] = {}
    flip = False
    sections = []
    tr, flt, zoom = style.get("transition", "none"), style.get("filter"), float(style.get("zoom") or 0.12)
    for n, c in enumerate(cues):
        d = round(c.end - c.start, 3)
        visuals = []
        use_presenter = presenter and (n % 3 == 0) and c.end <= presenter["duration"] + 0.05
        if use_presenter:                                     # người dẫn: lấy đúng đoạn trùng thời gian lời nói
            pc = clips.setdefault(presenter["name"], _Clip(presenter["name"], presenter["duration"], "video"))
            k = shots_needed(c, 7.5)
            t = c.start
            for j in range(k):
                part = round(d / k, 3)
                st, en = round(t, 3), round(min(presenter["duration"], t + part), 3)
                if en - st < 1.0:
                    st = max(0.0, en - 1.0)
                pc.scenes.append([st, en])
                visuals.append({"footage": presenter["name"], "start": st, "end": en, "speed": round((en - st) / part, 4)})
                t += part
        else:
            pool = media_for_cue.get(c.index) or []
            if not pool:
                raise ValueError(f"Câu {c.index} chưa có hình minh hoạ")
            k = shots_needed(c, float(style.get("pace") or 4.0))
            part = round(d / k, 3)
            for j in range(k):
                m = pool[j % len(pool)]
                clip = clips.setdefault(m["name"], _Clip(m["name"], float(m["duration"]), m["kind"]))
                st, en, speed = clip.take(part)
                v = {"footage": m["name"], "start": st, "end": en, "speed": max(0.5, min(2.0, speed))}
                if m["kind"] == "photo":                        # Ken Burns: luân phiên zoom vào / ra
                    flip = not flip
                    v.update(zoom_start=1.0 if flip else round(1 + zoom, 3), zoom_end=round(1 + zoom, 3) if flip else 1.0)
                visuals.append(v)
        for j, v in enumerate(visuals):
            if flt:
                v["filter"] = flt
            if tr != "none" and (n > 0 or j > 0) and (j == 0 or tr in ("flash_cut", "whip_pan")):
                v["transition_in"] = tr
        sections.append({"srt_index": c.index, "voice_start": c.start, "voice_end": c.end, "visuals": visuals,
                         "purpose": "hook" if n == 0 else ("cta" if n == len(cues) - 1 and len(cues) > 2 else "plot")})
    plan = {"creative_direction": f"{style['label']} — hình minh hoạ tự động theo từng câu",
            "voice_language": language, "target_duration_seconds": int(round(cues[-1].end)) if cues else 0,
            "sections": sections}
    scenes = {}
    for name, cl in clips.items():
        # sắp đoạn ngắn trước: bộ kiểm tra gán mỗi clip vào đoạn NHỎ NHẤT chứa nó (= đúng đoạn đã cấp)
        uniq = sorted({(round(a, 3), round(b, 3)) for a, b in cl.scenes}, key=lambda r: (r[1] - r[0], r[0]))
        scenes[name] = [{"start": a, "end": b} for a, b in uniq]
    return plan, scenes
