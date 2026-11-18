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
                                                   "set_render_option", "set_cue_text", "regenerate_plan", "render_preview",
                                                   "auto_create", "update_media_request"]},
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
                "prompt": {"type": "string", "description": "update_media_request: prompt vẽ mới (tiếng Anh) cho câu srt_index"},
                "what": {"type": "string", "description": "update_media_request: mô tả hình cần tìm mới (tiếng Việt)"},
                "style": {"type": "string", "description": "regenerate_plan / auto_create: phong cách mới"},
                "quality": {"type": "string", "enum": ["preview", "final"],
                            "description": "auto_create: final = thành phẩm 1080p (mặc định), preview = xem thử 720p"},
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
- Người dùng muốn AI TỰ LÀM TRỌN GÓI từ đầu ("tự động edit", "làm lại hết rồi xuất video") -> auto_create
  (phân tích, bản dựng mới, nhạc nền, xuất video); reply kể ngắn các bước sẽ làm.
- Có danh sách HÌNH CẦN TÌM / CẦN VẼ (media_requests): người dùng hỏi về hình, nhờ viết lại/đổi phong cách prompt, hỏi tìm ở đâu
  -> trả lời trong reply (viết prompt tiếng Anh đầy đủ trong reply) VÀ dùng update_media_request để cập nhật prompt/mô tả của câu đó.
  Nhắc người dùng bấm 📎 Gắn file ở đúng câu khi đã có hình.
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
    actions: dict = {"regenerate": None, "render": False, "cue_texts": {}, "auto": None}
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
            elif op == "update_media_request":
                if o.get("srt_index") is not None and (o.get("prompt") or o.get("what")):
                    actions.setdefault("media", []).append({"cue": int(o["srt_index"]), "prompt": o.get("prompt"),
                                                            "what": o.get("what")})
                    notes.append(f"Cập nhật hình cần cho câu {o['srt_index']}")
            elif op == "auto_create":
                actions["auto"] = {"style": o.get("style"), "quality": o.get("quality") or "final"}
                notes.append("Tự động làm trọn gói")
            elif op == "render_preview":
                actions["render"] = True
                notes.append("Dựng video xem thử")
        except Exception as e:      # một thao tác lỗi không làm hỏng các thao tác khác
            notes.append(f"Bỏ qua thao tác {op}: {e}")
    return plan, options, actions, notes


def chat_edit(ctx: PlanContext, plan: dict | None, options: dict, history: list[dict], message: str,
              client=None, focus: str | None = None) -> dict:
    """Một lượt chat. Trả {reply, plan (dict|None nếu không đổi), options, actions, notes, issues}."""
    client = client or get_client()
    content = [
        {"type": "text", "text": static_block(ctx), "cache_control": {"type": "ephemeral"}},
        {"type": "text", "text": json.dumps({"plan": compact_plan(plan), "render_options": options}, ensure_ascii=False)},
    ]
    msgs = []
    for h in history[-8:]:          # vài lượt gần nhất để hiểu ngữ cảnh "làm tiếp", "như vừa nãy"
        if h.get("kind") == "progress":
            continue
        role = "assistant" if h.get("role") == "assistant" else "user"
        if msgs and msgs[-1]["role"] == role:
            msgs[-1]["content"] += "\n" + h.get("text", "")
        else:
            msgs.append({"role": role, "content": h.get("text", "")})
    while msgs and msgs[0]["role"] != "user":
        msgs.pop(0)
    if focus:                       # người xem bấm "Sửa đoạn này" ở một thời điểm trên video
        content.append({"type": "text", "text": focus})
    content.append({"type": "text", "text": "Yêu cầu: " + message})
    if msgs and msgs[-1]["role"] == "user":
        msgs[-1] = {"role": "user", "content": [{"type": "text", "text": msgs[-1]["content"]}] + content}
    else:
        msgs.append({"role": "user", "content": content})

    new_plan, new_opts, actions, notes, issues, reply = plan, options, {"regenerate": None, "render": False, "cue_texts": {}, "auto": None}, [], [], ""
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
        actions["auto"] = acts["auto"] or actions["auto"]
        actions.setdefault("media", []).extend(acts.get("media") or [])
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



# ------------------------------------------------------------------ chế độ "đạo diễn": chưa có bản dựng / bấm ⚡ Tự động
DIRECTOR_TOOL = {
    "name": "director",
    "description": "Trả lời người dùng và (nếu cần) bắt đầu tự động làm video.",
    "input_schema": {"type": "object", "properties": {
        "reply": {"type": "string", "description": "Trả lời thân thiện, ngắn (2-5 câu), ngôn ngữ của người dùng. Nếu bắt đầu làm: "
                  "nói rõ các bước sẽ làm dựa trên tài nguyên đang có. Nếu thiếu thứ chỉ người dùng cung cấp được: hỏi đúng thứ đó."},
        "action": {"type": "string", "enum": ["none", "auto_create"]},
        "style": {"type": "string", "description": "auto_create: mã phong cách (chỉ khi người dùng nêu/ngụ ý; trống = giữ như cũ)"},
        "topic": {"type": "string", "description": "auto_create: chủ đề nếu người dùng vừa nêu (dùng khi chưa có kịch bản)"},
        "quality": {"type": "string", "enum": ["preview", "final"]},
    }, "required": ["reply", "action"]},
}

DIRECTOR_SYSTEM = """Bạn là Claude, trợ lý dựng video trong ứng dụng Movie Review Editor, trò chuyện với người dùng ở khung chat.
Bạn điều khiển quy trình TỰ ĐỘNG EDIT: phân tích footage/cảnh -> (viết kịch bản, tạo giọng, phụ đề nếu thiếu) -> lên bản dựng ->
chọn nhạc nền -> xuất video. Không có phim của người dùng thì làm chế độ Kịch bản -> Video: tự tìm ảnh/video minh hoạ miễn phí
bản quyền theo từng câu. `project` cho biết tình trạng từng mục (ok / ai = AI tự làm được / user = cần người dùng / optional).
- Người dùng bấm nút ⚡ Tự động hoặc nhờ làm video -> action auto_create, reply = kế hoạch ngắn theo đúng tài nguyên đang có.
- Mục nào 'user' (vd không có giọng đọc và không có key TTS) mà quy trình cần -> vẫn auto_create nếu phần còn lại làm được,
  nhưng nói rõ cần bổ sung gì; nếu không thể bắt đầu -> action none và hỏi người dùng đúng thứ còn thiếu.
- Câu hỏi thường -> action none, trả lời ngắn. Không bịa tính năng.
- Bạn chỉ thao tác được BÊN TRONG app (không sửa mã nguồn, không cài đặt máy). Gặp lỗi kỹ thuật: giải thích ngắn bằng lời
  dễ hiểu và chỉ cách xử lý trong app (vd nạp file SRT ở mục Kịch bản, dán kịch bản, kiểm tra ⚙ Cài đặt). Không dùng markdown tiêu đề."""


def director(info: dict, history: list[dict], message: str, client=None) -> dict:
    client = client or get_client()
    msgs = []
    for h in history[-8:]:
        if h.get("kind") == "progress":
            continue
        role = "assistant" if h.get("role") == "assistant" else "user"
        if msgs and msgs[-1]["role"] == role:
            msgs[-1]["content"] += "\n" + h.get("text", "")
        else:
            msgs.append({"role": role, "content": h.get("text", "")})
    while msgs and msgs[0]["role"] != "user":
        msgs.pop(0)
    turn = json.dumps({"project": info}, ensure_ascii=False) + "\n\nNgười dùng: " + message
    if msgs and msgs[-1]["role"] == "user":
        msgs[-1]["content"] += "\n" + turn
    else:
        msgs.append({"role": "user", "content": turn})
    resp = client.messages.create(model=settings.claude_model, max_tokens=1500, system=DIRECTOR_SYSTEM, tools=[DIRECTOR_TOOL],
                                  tool_choice={"type": "tool", "name": "director"}, messages=msgs)
    block = next((b for b in resp.content if getattr(b, "type", "") == "tool_use"), None)
    d = (block.input or {}) if block else {}
    action = d.get("action") if d.get("action") in ("none", "auto_create") else "none"
    return {"reply": (d.get("reply") or "").strip() or "Mình chưa hiểu ý bạn, bạn nói rõ hơn nhé?", "action": action,
            "style": (d.get("style") or "").strip() or None, "topic": (d.get("topic") or "").strip() or None,
            "quality": d.get("quality") if d.get("quality") in ("preview", "final") else "final"}


SUMMARY_SYSTEM = """Bạn là Claude, vừa chạy xong một lượt tự động edit video cho người dùng. Viết tin nhắn chat NGẮN (2-5 câu,
tiếng Việt, thân thiện, không dùng tiêu đề markdown): đã làm được gì, điều gì cần người dùng chú ý/bổ sung (nếu có),
và nếu video đang được xuất thì nói sẽ báo khi xong. Kết thúc bằng một câu mời người dùng nói điều muốn chỉnh
(gợi ý 1 ví dụ cụ thể hợp với video). Không bịa số liệu ngoài dữ liệu được cho."""


def summarize(data: dict, client=None) -> str:
    """Tin nhắn tổng kết cuối quy trình tự động (model nhanh). Lỗi -> trả chuỗi rỗng để bên gọi dùng mẫu có sẵn."""
    client = client or get_client()
    resp = client.messages.create(model=settings.claude_fast_model or settings.claude_model, max_tokens=600,
                                  system=SUMMARY_SYSTEM,
                                  messages=[{"role": "user", "content": json.dumps(data, ensure_ascii=False)}])
    return "".join(getattr(b, "text", "") for b in resp.content if getattr(b, "type", "") == "text").strip()


# ------------------------------------------------------------------ hình còn thiếu: danh sách cần tìm / prompt cần vẽ
MEDIA_TOOL = {
    "name": "media_requests",
    "description": "Danh sách hình/video người dùng cần tự tìm hoặc vẽ cho từng câu.",
    "input_schema": {"type": "object", "properties": {
        "intro": {"type": "string", "description": "1-2 câu tiếng Việt mở đầu tin nhắn chat (thân thiện, nói rõ cần làm gì)."},
        "items": {"type": "array", "items": {"type": "object", "properties": {
            "cue": {"type": "integer"},
            "what": {"type": "string", "description": "Tiếng Việt: hình cần có (chủ thể, bối cảnh, góc máy), 1 câu."},
            "keywords": {"type": "array", "items": {"type": "string"}, "maxItems": 4,
                         "description": "Từ khoá tìm (tiếng Anh + tiếng Việt) cho Google Images / kho ảnh."},
            "where": {"type": "string", "description": "Gợi ý nơi tìm (vd Pexels, Wikimedia Commons, kho ảnh báo chí có bản quyền…)."},
            "prompt": {"type": "string", "description": "Prompt TIẾNG ANH để vẽ bằng AI tạo ảnh (Midjourney / DALL·E / Ideogram…): "
                       "chủ thể, hành động, bối cảnh, phong cách vẽ, ánh sáng, màu, bố cục, tỉ lệ khung. Không chữ trong ảnh."},
        }, "required": ["cue", "what", "prompt"]}},
    }, "required": ["intro", "items"]},
}

MEDIA_SYSTEM = """Bạn là trợ lý hình ảnh cho video {kind} — phong cách "{label}", khung hình {aspect}.
Mỗi mục là một câu lời đọc cần hình. need = "draw": cần ẢNH VẼ (hoạt hình/minh hoạ) -> prompt phải thật cụ thể, cùng một phong cách vẽ
nhất quán cho cả video ({art}). need = "find": kho ảnh miễn phí không có hình hợp -> mô tả + từ khoá để người dùng tự tìm (vẫn viết
prompt để họ có thể vẽ thay). Prompt kết thúc bằng tham số tỉ lệ khung, vd "--ar {ar}". Không đưa người thật/nhân vật có bản quyền vào prompt;
nhân vật lịch sử thì mô tả trang phục, thời kỳ, bối cảnh."""


def media_requests(items: list[dict], style: dict, topic: str, client=None, batch: int = 40) -> dict:
    """items: [{cue, start, end, text, need: find|draw, q: [...]}] -> {intro, items:[...+what/keywords/where/prompt]}."""
    from .styles import GROUPS
    client = client or get_client()
    aspect = style.get("aspect") or "16:9"
    system = MEDIA_SYSTEM.format(kind=GROUPS.get(style.get("group"), "thông tin"), label=style.get("label", ""), aspect=aspect,
                                 ar=aspect, art=style.get("hint") or "flat 2D illustration")
    out, intro = {}, ""
    for k in range(0, len(items), batch):
        part = items[k:k + batch]
        body = {"topic": topic, "items": [{"cue": i["cue"], "text": i["text"], "need": i["need"], "tried_keywords": i.get("q") or []}
                                          for i in part]}
        resp = client.messages.create(model=settings.claude_fast_model or settings.claude_model, max_tokens=8000, system=system,
                                      tools=[MEDIA_TOOL], tool_choice={"type": "tool", "name": "media_requests"},
                                      messages=[{"role": "user", "content": json.dumps(body, ensure_ascii=False)}])
        block = next((b for b in resp.content if getattr(b, "type", "") == "tool_use"), None)
        d = (block.input or {}) if block else {}
        intro = intro or (d.get("intro") or "").strip()
        for it in d.get("items") or []:
            try:
                out[int(it["cue"])] = it
            except (KeyError, TypeError, ValueError):
                continue
    res = []
    for i in items:
        g = out.get(i["cue"]) or {}
        res.append({**i, "what": (g.get("what") or i["text"]).strip(),
                    "keywords": [x for x in (g.get("keywords") or i.get("q") or []) if isinstance(x, str)][:4],
                    "where": (g.get("where") or "").strip(), "prompt": (g.get("prompt") or "").strip()})
    return {"intro": intro, "items": res}
