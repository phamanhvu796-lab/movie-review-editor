from types import SimpleNamespace as NS

from app.services.claude_service import TOOL_NAME, generate_edit_plan
from tests.helpers import make_ctx, make_plan


class FakeClient:
    def __init__(self, plans):
        self.plans, self.calls = list(plans), []
        self.messages = NS(create=self._create)

    def _create(self, **kw):
        self.calls.append(kw)
        plan = self.plans.pop(0)
        return NS(stop_reason="tool_use",
                  content=[NS(type="tool_use", name=TOOL_NAME, id=f"t{len(self.calls)}", input=plan)])


def test_retry_then_success_sends_errors_back():
    bad = make_plan()
    bad["sections"][0]["visuals"][0]["footage"] = "zzz.mp4"   # lỗi repair không sửa được
    client = FakeClient([bad, make_plan()])
    res = generate_edit_plan(make_ctx(), client=client)
    assert res.status == "ok" and res.attempts == 2
    feedback = client.calls[1]["messages"][-1]["content"][0]
    assert feedback["is_error"] and "UNKNOWN_FOOTAGE" in feedback["content"]


def test_gives_up_after_max_retries():
    bad = make_plan()
    bad["sections"].pop()
    client = FakeClient([bad] * 4)
    res = generate_edit_plan(make_ctx(), client=client, max_retries=3)
    assert res.status == "needs_manual_review" and res.attempts == 4 and len(client.calls) == 4
    assert any(i.code == "MISSING_CUES" for i in res.issues)


def test_chunked_generation_merges_parts():
    full = make_plan(3)
    part1 = {**full, "sections": full["sections"][:2]}
    part2 = {**full, "sections": full["sections"][2:]}
    client = FakeClient([part1, part2])
    res = generate_edit_plan(make_ctx(), client=client, chunk_size=2)
    assert res.status == "ok" and len(res.plan.sections) == 3 and len(client.calls) == 2


def test_mechanical_errors_are_repaired_without_retry():
    bad = make_plan()
    bad["sections"][0]["visuals"][0].update(start=8.0, end=12.0, zoom_end=1.9)   # vắt scene + zoom vượt giới hạn
    client = FakeClient([bad])
    res = generate_edit_plan(make_ctx(), client=client)
    assert res.status == "ok" and res.attempts == 1 and len(client.calls) == 1


class SmartFake:
    """Trả plan theo 'part' trong request (chạy song song nên thứ tự gọi không cố định)."""
    def __init__(self, by_part):
        self.by_part, self.calls = {k: list(v) for k, v in by_part.items()}, []
        self.messages = NS(create=self._create)

    def _create(self, **kw):
        import json
        self.calls.append(kw)
        part = json.loads(kw["messages"][0]["content"][1]["text"])["part"]
        return NS(stop_reason="tool_use",
                  content=[NS(type="tool_use", name=TOOL_NAME, id=f"t{len(self.calls)}", input=self.by_part[part].pop(0))])


def _sec(i, scene):
    return {"srt_index": i + 1, "voice_start": 4.0 * i, "voice_end": 4.0 * (i + 1), "purpose": "plot",
            "visuals": [{"footage": "a.mp4", "start": 10.0 * scene + 1, "end": 10.0 * scene + 5}]}


def _plan(secs):
    return {"creative_direction": "t", "voice_language": "vi", "target_duration_seconds": 24, "sections": secs}


def test_parallel_chunks_conflict_is_reconciled():
    ctx = make_ctx(6)
    client = SmartFake({
        "1/3": [_plan([_sec(0, 0), _sec(1, 1)])],
        "2/3": [_plan([_sec(2, 4), _sec(3, 4)])],                       # scene 4 dùng 2 lần (hợp lệ khi đứng riêng)
        "3/3": [_plan([_sec(4, 4), _sec(5, 4)]), _plan([_sec(4, 5), _sec(5, 5)])],   # trùng scene 4 -> phải dựng lại
    })
    res = generate_edit_plan(ctx, client=client, chunk_size=2)
    assert res.status == "ok" and len(res.plan.sections) == 6
    assert len(client.calls) == 4 and res.attempts == 4


def test_subtitle_can_be_omitted_by_claude():
    plan = make_plan()
    for s in plan["sections"]:
        s.pop("subtitle")
    res = generate_edit_plan(make_ctx(), client=FakeClient([plan]))
    assert res.status == "ok" and [s.subtitle for s in res.plan.sections] == ["c1", "c2", "c3"]
