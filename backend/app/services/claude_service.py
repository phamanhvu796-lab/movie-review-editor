import json
import logging
from collections import Counter
from dataclasses import dataclass, field

from concurrent.futures import ThreadPoolExecutor

from typing import TYPE_CHECKING

if TYPE_CHECKING:                       # chỉ để gợi ý kiểu; không bắt buộc cài SDK khi import module
    from anthropic import Anthropic

from ..config import settings
from ..schemas import EditPlan, PlanContext
from .claude_client import get_client
from .repair_service import repair_plan
from .validator_service import Issue, collect_usage, format_issues, validate_plan

log = logging.getLogger("mre.plan")
TOOL_NAME = "create_professional_edit_plan"

SYSTEM_PROMPT = """Bạn là senior video editor chuyên dựng video review phim cho YouTube.

Nhiệm vụ: đọc SRT của voice-over, chọn footage/scene phù hợp từng câu nói, trả về edit plan
bằng cách GỌI TOOL `create_professional_edit_plan`. Không viết gì ngoài tool call.

Quy tắc bắt buộc:
1. Voice-over là xương sống. Hình ảnh phục vụ lời nói, không đi theo thứ tự video gốc.
2. Không bịa timestamp: mỗi clip [start, end] phải nằm TRỌN trong một scene trong danh sách
   (scene được đánh số `index`, tính từ 0 trong từng footage).
3. Mỗi cue SRT có đúng một section; voice_start/voice_end bằng đúng start/end của cue.
4. Tổng độ dài timeline của các visual trong section (= (end - start) / speed) phải bằng độ dài cue.
   Cue dài hơn 8s thì tách thành nhiều visual.
5. Mỗi clip nguồn dài 1-8 giây. Không dùng một scene quá 2 lần (tính cả `already_used_scenes`).
6. Hook (0-8s đầu video) phải mạnh: zoom hoặc filter ấn tượng.
7. Nói về diễn xuất -> ưu tiên shot_type close_up. Nói về quay phim -> ưu tiên wide hoặc camera_motion.
8. Không dùng cảnh có spoiler = true nếu allow_spoilers = false.
9. Tối đa 3 filter khác nhau trong cả video (tính cả `filters_already_used`), chỉ dùng filter trong available_filters.
10. Chỉ tăng tốc (speed > 1) cho B-roll/montage. Có thể đổi thứ tự cảnh để tăng tính hấp dẫn.
11. KHÔNG điền `subtitle` (hệ thống tự điền). Bỏ các trường đang bằng giá trị mặc định (speed 1, brightness/contrast/saturation 1,
    zoom 1.0, transition none, filter null) để tiết kiệm token. Giá trị số phải nằm trong giới hạn của schema.
12. Video được xử lý theo từng phần (`part`); chỉ trả sections cho các cue trong phần hiện tại."""

CHUNK_SIZE = 40


@dataclass
class PlanResult:
    status: str                      # "ok" | "needs_manual_review"
    plan: EditPlan | None
    raw_plan: dict | None
    issues: list[Issue] = field(default_factory=list)
    attempts: int = 0


def _tool_def() -> dict:
    return {
        "name": TOOL_NAME,
        "description": "Trả về edit plan JSON cho video review phim.",
        "input_schema": EditPlan.model_json_schema(),
    }


def _static_block(ctx: PlanContext) -> str:
    """Phần không đổi giữa các batch -> được cache prompt."""
    d = {
        "movie_title": ctx.movie_title, "genre": ctx.genre, "language": ctx.language, "style": ctx.style,
        "allow_spoilers": ctx.allow_spoilers, "target_duration_seconds": ctx.target_duration_seconds,
        "available_footage": [{
            "file": f.file, "duration": f.duration, "description": f.description,
            "scenes": [{"index": k, **s.model_dump(exclude_none=True)} for k, s in enumerate(f.scenes)],
        } for f in ctx.available_footage],
    }
    return json.dumps(d, ensure_ascii=False)


def _chunk_block(ctx: PlanContext, part: str, use: Counter, filters: set[str]) -> str:
    return json.dumps({
        "part": part,
        "available_filters": ctx.available_filters,
        "srt_cues": [c.model_dump() for c in ctx.srt_cues],
        "already_used_scenes": [{"footage": f, "scene_index": i, "count": n} for (f, i), n in use.items()],
        "filters_already_used": sorted(filters),
    }, ensure_ascii=False)


def _generate_chunk(client, ctx: PlanContext, part: str, use: Counter, filters: set[str],
                    max_retries: int) -> PlanResult:
    content = [
        {"type": "text", "text": _static_block(ctx), "cache_control": {"type": "ephemeral"}},
        {"type": "text", "text": _chunk_block(ctx, part, use, filters)},
    ]
    messages = [{"role": "user", "content": content}]
    raw, issues = None, []

    for attempt in range(1, max_retries + 2):          # 1 lần đầu + max_retries lần sửa
        resp = client.messages.create(
            model=settings.claude_model, max_tokens=16000, system=SYSTEM_PROMPT,
            tools=[_tool_def()], tool_choice={"type": "tool", "name": TOOL_NAME}, messages=messages,
        )
        block = next((b for b in resp.content if b.type == "tool_use" and b.name == TOOL_NAME), None)

        if block is None or resp.stop_reason == "max_tokens":
            raw = None
            issues = [Issue("NO_PLAN", "Không nhận được tool call hợp lệ hoặc output bị cắt; "
                                       "hãy trả plan gọn hơn (không thêm trường thừa).")]
            tool_id = block.id if block else None
        else:
            raw, tool_id = block.input, block.id
            raw, fixes = repair_plan(raw, ctx)   # sửa lỗi cơ học miễn phí trước khi tốn thêm 1 lượt gọi API
            if fixes:
                log.info("Tự sửa %d lỗi cơ học: %s", len(fixes), "; ".join(fixes[:8]))
            plan, issues = validate_plan(raw, ctx, use, filters)
            if not issues:
                return PlanResult("ok", plan, raw, [], attempt)

        if attempt > max_retries:
            break
        messages.append({"role": "assistant", "content": resp.content})
        if tool_id is None:
            messages.append({"role": "user", "content": format_issues(issues)})
        else:
            messages.append({"role": "user", "content": [{
                "type": "tool_result", "tool_use_id": tool_id, "is_error": True,
                "content": "Plan không hợp lệ. Sửa TẤT CẢ lỗi sau và gọi lại tool:\n" + format_issues(issues),
            }]})

    return PlanResult("needs_manual_review", None, raw, issues, max_retries + 1)


def generate_edit_plan(ctx: PlanContext, client: "Anthropic | None" = None, max_retries: int | None = None,
                       chunk_size: int = CHUNK_SIZE, max_parallel: int = 3) -> PlanResult:
    """Phần 1 chạy trước (chốt bảng filter và các cảnh hook); các phần còn lại chạy SONG SONG chỉ với filter
    của phần 1 (đồng nhất màu, luôn <= 3 filter). Sau đó đối chiếu tuần tự: phần nào xung đột cảnh đã dùng
    với phần song song khác thì dựng lại với trạng thái thật. Static block giống hệt nhau nên prompt cache dùng chung."""
    client = client or get_client()
    max_retries = settings.plan_max_retries if max_retries is None else max_retries

    cues = ctx.srt_cues
    chunks = [cues[i:i + chunk_size] for i in range(0, len(cues), chunk_size)]
    n = len(chunks)
    subs = [ctx.model_copy(update={"srt_cues": c, "voice_duration": sum(x.end - x.start for x in c)}) for c in chunks]
    parts = [f"{k + 1}/{n}" for k in range(n)]
    attempts = 0

    def fail(sections, direction, res):
        partial = sections + ((res.raw_plan or {}).get("sections") or [])
        raw = {"creative_direction": direction, "voice_language": ctx.language,
               "target_duration_seconds": ctx.target_duration_seconds, "sections": partial}
        return PlanResult("needs_manual_review", None, raw, res.issues, attempts)

    first = _generate_chunk(client, subs[0], parts[0], Counter(), set(), max_retries)
    attempts += first.attempts
    if first.status != "ok":
        return fail([], "", first)
    results = [first]
    if n > 1:
        use1, f1 = collect_usage(first.plan, subs[0])
        for k in range(1, n):
            subs[k] = subs[k].model_copy(update={"available_filters": sorted(f1)})
        with ThreadPoolExecutor(max_workers=min(max_parallel, n - 1)) as ex:
            futs = [ex.submit(_generate_chunk, client, subs[k], parts[k], use1, f1, max_retries) for k in range(1, n)]
            results += [f.result() for f in futs]

    sections: list[dict] = []
    direction = ""
    use: Counter = Counter()
    filters: set[str] = set()
    for k, res in enumerate(results):
        if k > 0:
            attempts += res.attempts
            if res.status == "ok":
                _, issues = validate_plan(res.raw_plan, subs[k], use, filters)
                if issues:      # xung đột với phần chạy song song khác -> dựng lại với trạng thái thật
                    res = _generate_chunk(client, subs[k], parts[k], use, filters, max_retries)
                    attempts += res.attempts
        if res.status != "ok":
            return fail(sections, direction, res)
        direction = direction or res.plan.creative_direction
        sections += [s.model_dump() for s in res.plan.sections]
        u, f = collect_usage(res.plan, subs[k])
        use.update(u)
        filters |= f

    merged = {"creative_direction": direction, "voice_language": ctx.language,
              "target_duration_seconds": ctx.target_duration_seconds, "sections": sections}
    plan, issues = validate_plan(merged, ctx)        # kiểm tra lần cuối trên toàn bộ video
    if issues:
        return PlanResult("needs_manual_review", None, merged, issues, attempts)
    return PlanResult("ok", plan, merged, [], attempts)
