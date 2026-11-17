"""Trợ lý AI sửa bản dựng theo câu lệnh (khung chat bên cạnh).

Claude KHÔNG trả lại cả bản dựng (tốn token với video dài) mà trả một danh sách THAO TÁC nhỏ. App áp dụng thao tác,
tự sửa lỗi cơ học (repair_plan), kiểm tra (validate_plan); còn lỗi thì gửi lại Claude sửa thêm tối đa 1 lần.
Khối dữ liệu tĩnh (danh sách cảnh + phụ đề) được đánh dấu cache để các lượt chat sau rẻ hơn.
"""
import json
import logging
from copy import deepcopy

from ..config import settings
from ..schemas import PlanContext
from .claude_client import get_client
from .repair_service import repair_plan
from .validator_service import format_issues, validate_plan

log = logging.getLogger("mre.chat")
TOOL_NAME = "apply_edits"

VISUAL_KEYS = {"footage", "start", "end", "zoom_start", "zoom_end", "speed", "brightness", "contrast", "saturation",
               "filter", "transition_in", "transition_out"}
OPTION_KEYS = {"burn_subtitles", "subtitle_font", "subtitle_size", "subtitle_color", "subtitle_outline_color", "subtitle_margin_v",
               "music_volume", "music_ducking", "vignette", "film_grain", "logo_position", "logo_width_pct", "logo_opacity",
               "logo_margin", "aspect_ratio", "fps"}

TOOL = {
    "name": TOOL_NAME,
    "description": "Áp dụng các thay đổi lên bản dựng video và trả lời người dùng.",
    "input_schema": {"type": "object", "properties": {
        "reply": {"type": "string", "description": "Trả lời NGẮN (1-3 câu, ngôn ngữ của người dùng): đã/không làm gì và vì sao."},
        "operations": {"type": "array", "description": "Danh sách thao tác theo thứ tự. Rỗng nếu chỉ trả lời câu hỏi.", "items": {
            "type": "object", "properties": {
                "op": {"type": "string", "enum": ["update_visual", "replace_visuals", "update_all_visuals", "update_section",
                                                   "set_render_option", "set_cue_text", "regenerate_plan", "render_preview"]},
                "srt_index": {"type": "integer", "description": "Câu thoại (section) cần sửa"},
                "srt_indices": {"type": "array", "items": {"type": "integer"},
                                "description": "update_all_visuals: chỉ áp cho các câu này (bỏ trống = cả video)"},
                "visual_index": {"type": "integer", "description": "Vị trí clip trong section (0 = clip đầu)"},
                "changes": {"type": "object", "description": "Trường cần đổi. Visual: footage,start,end,zoom_start,zoom_end,speed,"
                            "brightness,contrast,saturation,filter,transition_in,transition_out. Section: purpose. "
                            "Tuỳ chọn xuất: burn_subtitles,subtitle_font,subtitle_size,subtitle_color,subtitle_outline_color,"
                            "music_volume(0-1),music_ducking,vignette,film_grain,logo_position,logo_width_pct,logo_opacity,aspect_ratio,fps"},
                "visuals": {"type": "array", "items": {"type": "object"}, "description": "replace_visuals: danh sách clip mới của section"},
                "text": {"type": "string", "description": "set_cue_text: nội dung phụ đề mới"},
                "style": {"type": "string", "description": "regenerate_plan: phong cách mới"},
            }, "required": ["op"]}},
    }, "required": ["reply", "operations"]},
}

SYSTEM = """Bạn là trợ lý dựng video review phim, làm việc trong một trình dựng video. Người dùng ra lệnh bằng lời,
bạn sửa bản dựng hiện có bằng cách GỌI TOOL `apply_edits` với các thao tác nhỏ, chính xác. Luôn gọi tool.

Dữ liệu: `footage` (mỗi file có danh sách cảnh [index] start-end, loại cảnh, mô tả), `cues` (câu thoại voice-over),
`plan` (bản dựng hiện tại: mỗi section ứng với một câu thoại srt_index, có danh sách visuals), `render_options`.

Quy tắc khi sửa clip:
- Mỗi clip [start, end] phải nằm TRỌN trong một cảnh của đúng footage đó, dài 1-8 giây; không dùng 1 cảnh quá 2 lần.
- Tổng (end-start)/speed của các clip trong section nên bằng độ dài câu thoại (hệ thống sẽ tự co giãn clip cuối).
- Giới hạn: zoom 1.0-1.5, speed 0.5-2, brightness 0.7-1.4, contrast 0.7-1.5, saturation 0-1.8; tối đa 3 filter khác nhau
  trong cả video, chỉ dùng filter trong available_filters; transition: none, flash_cut, fade, cross_dissolve, whip_pan.
- Thay đổi áp cho nhiều clip (vd "màu điện ảnh toàn bộ", "thêm zoom chậm") -> dùng update_all_visuals.
- Đổi cỡ chữ/màu phụ đề, nhạc nền, logo, khung hình -> set_render_option.
- Yêu cầu làm lại toàn bộ hoặc chưa có bản dựng -> regenerate_plan. Muốn xem kết quả -> thêm render_preview.
- Không chắc người dùng muốn gì: không thao tác, hỏi lại ngắn gọn trong reply.
- Câu hỏi về dự án: trả lời trong reply, operations rỗng."""


def _r(x, n=2):
    return round(float(x), n)


def static_block(ctx: PlanContext) -> str:
    foot = []
    for f in ctx.available_footage:
        sc = []
        for k, s in enumerate(f.scenes):
            extra = " ".join(x for x in (s.shot_type or "", (s.description or "")[:80], "SPOILER" if s.spoiler else "") if x)
            sc.append(f"[{k}] {_r(s.start, 1)}-{_r(s.end, 1)}" + (f" {extra}" if extra else ""))
        foot.append({"file": f.file, "duration": _r(f.duration, 1), "scenes": sc})
    return json.dumps({"movie": ctx.movie_title, "genre": ctx.genre, "allow_spoilers": ctx.allow_spoilers,
                       "available_filters": ctx.available_filters, "footage": foot,
                       "cues": [f"{c.index}: {_r(c.start)}-{_r(c.end)} {c.text}" for c in ctx.srt_cues]}, ensure_ascii=False)


def compact_plan(plan: dict | None) -> dict | None:
    """Bỏ trường mặc định/subtitle để tiết kiệm token."""
    if not plan:
        return None
    defaults = {"zoom_start": 1.0, "zoom_end": 1.0, "speed": 1.0, "brightness": 1.0, "contrast": 1.0, "saturation": 1.0,
                "filter": None, "transition_in": "none", "transition_out": "none"}
    secs = []
    for s in plan.get("sections") or []:
        vis = [{k: (_r(v, 3) if isinstance(v, float) else v) for k, v in vv.items() if k in VISUAL_KEYS and defaults.get(k, object()) != v}
               for vv in s.get("visuals") or []]
        secs.append({"srt_index": s.get("srt_index"), "purpose": s.get("purpose"), "visuals": vis})
    return {"creative_direction": plan.get("creative_direction", ""), "sections": secs}


def apply_operations(plan: dict | None, ops: list[dict], options: dict) -> tuple[dict | None, dict, dict, list[str]]:
    """Áp thao tác. Trả (plan mới, render_options mới, hành động {regenerate, render, cue_texts}, ghi chú)."""
    plan = deepcopy(plan) if plan else None
    options = dict(options or {})
    actions: dict = {"regenerate": None, "render": False, "cue_texts": {}}
    notes: list[str] = []
    by_idx = {s.get("srt_index"): s for s in (plan or {}).get("sections") or []}

    def clean(ch: dict, keys: set) -> dict:
        return {k: v for k, v in (ch or {}).items() if k in keys}

    for o in ops or []:
        op = o.get("op")
        sec = by_idx.get(o.get("srt_index"))
        try:
            if op == "update_visual":
                if sec is None:
                    notes.append(f"Bỏ qua: không có câu {o.get('srt_index')}")
                    continue
                vi = int(o.get("visual_index") or 0)
                if not 0 <= vi < len(sec["visuals"]):
                    notes.append(f"Bỏ qua: câu {o.get('srt_index')} không có clip {vi + 1}")
                    continue
                sec["visuals"][vi].update(clean(o.get("changes"), VISUAL_KEYS))
                notes.append(f"Sửa clip {vi + 1} của câu {o.get('srt_index')}")
            elif op == "replace_visuals":
                vis = [clean(v, VISUAL_KEYS) for v in (o.get("visuals") or []) if v.get("footage")]
                if sec is None or not vis:
                    notes.append("Bỏ qua thay clip: thiếu câu hoặc danh sách clip")
                    continue
                sec["visuals"] = vis
                notes.append(f"Thay {len(vis)} clip cho câu {o.get('srt_index')}")
            elif op == "update_all_visuals":
                ch = clean(o.get("changes"), VISUAL_KEYS - {"footage", "start", "end"})
                only = set(o.get("srt_indices") or [])
                n = 0
                for s in (plan or {}).get("sections") or []:
                    if only and s.get("srt_index") not in only:
                        continue
                    for v in s["visuals"]:
                        v.update(ch)
                        n += 1
                if ch.get("filter") is None and "filter" in ch:
                    for s in (plan or {}).get("sections") or []:
                        for v in s["visuals"]:
                            v.pop("filter", None)
                notes.append(f"Áp {', '.join(ch) or 'không đổi gì'} cho {n} clip")
            elif op == "update_section":
                if sec is not None and (o.get("changes") or {}).get("purpose"):
                    sec["purpose"] = o["changes"]["purpose"]
                    notes.append(f"Đổi mục đích câu {o.get('srt_index')}")
            elif op == "set_render_option":
                ch = clean(o.get("changes"), OPTION_KEYS)
                options.update(ch)
                if ch:
                    notes.append("Đổi tuỳ chọn xuất: " + ", ".join(ch))
            elif op == "set_cue_text":
                if o.get("srt_index") is not None and (o.get("text") or "").strip():
                    actions["cue_texts"][int(o["srt_index"])] = o["text"].strip()
                    if sec is not None:
                        sec["subtitle"] = o["text"].strip()
                    notes.append(f"Sửa phụ đề câu {o['srt_index']}")
            elif op == "regenerate_plan":
                actions["regenerate"] = {"style": o.get("style")}
                notes.append("Tạo lại toàn bộ bản dựng")
            elif op == "render_preview":
                actions["render"] = True
                notes.append("Dựng video xem thử")
        except Exception as e:      # một thao tác lỗi không làm hỏng các thao tác khác
            notes.append(f"Bỏ qua thao tác {op}: {e}")
    return plan, options, actions, notes


def chat_edit(ctx: PlanContext, plan: dict | None, options: dict, history: list[dict], message: str,
              client=None) -> dict:
    """Một lượt chat. Trả {reply, plan (dict|None nếu không đổi), options, actions, notes, issues}."""
    client = client or get_client()
    content = [
        {"type": "text", "text": static_block(ctx), "cache_control": {"type": "ephemeral"}},
        {"type": "text", "text": json.dumps({"plan": compact_plan(plan), "render_options": options}, ensure_ascii=False)},
    ]
    msgs = []
    for h in history[-8:]:          # vài lượt gần nhất để hiểu ngữ cảnh "làm tiếp", "như vừa nãy"
        role = "assistant" if h.get("role") == "assistant" else "user"
        if msgs and msgs[-1]["role"] == role:
            msgs[-1]["content"] += "\n" + h.get("text", "")
        else:
            msgs.append({"role": role, "content": h.get("text", "")})
    while msgs and msgs[0]["role"] != "user":
        msgs.pop(0)
    content.append({"type": "text", "text": "Yêu cầu: " + message})
    if msgs and msgs[-1]["role"] == "user":
        msgs[-1] = {"role": "user", "content": [{"type": "text", "text": msgs[-1]["content"]}] + content}
    else:
        msgs.append({"role": "user", "content": content})

    new_plan, new_opts, actions, notes, issues, reply = plan, options, {"regenerate": None, "render": False, "cue_texts": {}}, [], [], ""
    for attempt in range(2):
        resp = client.messages.create(model=settings.claude_model, max_tokens=6000, system=SYSTEM, tools=[TOOL],
                                      tool_choice={"type": "tool", "name": TOOL_NAME}, messages=msgs)
        block = next((b for b in resp.content if b.type == "tool_use"), None)
        if block is None:
            reply = "".join(getattr(b, "text", "") for b in resp.content) or "AI không trả về thao tác nào."
            break
        data = block.input or {}
        reply = data.get("reply") or reply
        cand, new_opts, acts, n = apply_operations(plan if attempt == 0 else new_plan, data.get("operations") or [], new_opts)
        notes += n
        actions["render"] = actions["render"] or acts["render"]
        actions["regenerate"] = acts["regenerate"] or actions["regenerate"]
        actions["cue_texts"].update(acts["cue_texts"])
        if cand is None or cand == (plan if attempt == 0 else new_plan):
            new_plan = cand
            issues = []
            break
        cand, fixes = repair_plan(cand, ctx)
        _, issues = validate_plan(cand, ctx)
        new_plan = cand
        if not issues or attempt == 1:
            break
        msgs.append({"role": "assistant", "content": resp.content})
        msgs.append({"role": "user", "content": [{"type": "tool_result", "tool_use_id": block.id, "is_error": True,
                                                  "content": "Đã áp dụng nhưng bản dựng còn lỗi. Gọi lại tool với thao tác SỬA các lỗi sau "
                                                             "(bản dựng hiện tại đã gồm thay đổi vừa rồi):\n" + format_issues(issues)
                                                             + "\n" + json.dumps({"plan": compact_plan(new_plan)}, ensure_ascii=False)}]})
    changed = new_plan is not None and new_plan != plan
    return {"reply": reply, "plan": new_plan if changed else None, "options": new_opts, "actions": actions,
            "notes": notes, "issues": [str(i) for i in issues]}
