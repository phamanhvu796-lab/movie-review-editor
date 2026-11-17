import json
import os
import tempfile
from types import SimpleNamespace as NS

from app.services import chat_service, usage_service
from app.services.script_service import estimate_cues
from app.services.validator_service import validate_plan
from tests.helpers import make_ctx, make_plan


class FakeClient:
    def __init__(self, replies):
        self.replies, self.calls = list(replies), []
        self.messages = NS(create=self._create)

    def _create(self, **kw):
        self.calls.append(kw)
        data = self.replies.pop(0)
        return NS(stop_reason="tool_use", content=[NS(type="tool_use", name=chat_service.TOOL_NAME, id=f"t{len(self.calls)}", input=data)])


def test_filter_for_whole_video_and_render_option():
    client = FakeClient([{"reply": "Đã đổi màu", "operations": [
        {"op": "update_all_visuals", "changes": {"filter": "noir", "zoom_end": 1.2}},
        {"op": "set_render_option", "changes": {"subtitle_size": 70, "hacker_field": 1}},
        {"op": "render_preview"}]}])
    res = chat_service.chat_edit(make_ctx(), make_plan(), {}, [], "đen trắng toàn bộ, chữ to hơn", client=client)
    assert res["plan"] is not None and not res["issues"]
    assert all(v["filter"] == "noir" and v["zoom_end"] == 1.2 for s in res["plan"]["sections"] for v in s["visuals"])
    assert res["options"] == {"subtitle_size": 70}            # trường lạ bị bỏ
    assert res["actions"]["render"] is True
    _, issues = validate_plan(res["plan"], make_ctx())
    assert not issues
    # khối tĩnh được đánh dấu cache để các lượt sau rẻ hơn
    assert client.calls[0]["messages"][-1]["content"][0]["cache_control"]["type"] == "ephemeral"


def test_invalid_edit_is_sent_back_once_for_fixing():
    bad = {"reply": "ok", "operations": [{"op": "update_visual", "srt_index": 2, "visual_index": 0, "changes": {"footage": "nope.mp4"}}]}
    fix = {"reply": "đã sửa", "operations": [{"op": "update_visual", "srt_index": 2, "visual_index": 0, "changes": {"footage": "a.mp4"}}]}
    client = FakeClient([bad, fix])
    res = chat_service.chat_edit(make_ctx(), make_plan(), {}, [], "đổi cảnh câu 2", client=client)
    assert len(client.calls) == 2
    fb = client.calls[1]["messages"][-1]["content"][0]
    assert fb["type"] == "tool_result" and fb["is_error"] and "UNKNOWN_FOOTAGE" in fb["content"]
    assert res["plan"] is None or not res["issues"]          # sau khi sửa quay về hợp lệ


def test_question_only_and_history_roles_alternate():
    client = FakeClient([{"reply": "Video có 3 câu thoại.", "operations": []}])
    hist = [{"role": "assistant", "text": "chào"}, {"role": "user", "text": "a"}, {"role": "user", "text": "b"},
            {"role": "assistant", "text": "c"}]
    res = chat_service.chat_edit(make_ctx(), make_plan(), {}, hist, "video có mấy câu?", client=client)
    assert res["plan"] is None and res["reply"].startswith("Video")
    roles = [m["role"] for m in client.calls[0]["messages"]]
    assert roles[0] == "user" and all(a != b for a, b in zip(roles, roles[1:]))


def test_regenerate_and_cue_text_actions():
    client = FakeClient([{"reply": "Làm lại", "operations": [{"op": "regenerate_plan", "style": "nhịp nhanh"},
                                                              {"op": "set_cue_text", "srt_index": 1, "text": "Mở đầu mới"}]}])
    res = chat_service.chat_edit(make_ctx(), make_plan(), {}, [], "làm lại nhịp nhanh", client=client)
    assert res["actions"]["regenerate"] == {"style": "nhịp nhanh"}
    assert res["actions"]["cue_texts"] == {1: "Mở đầu mới"}


def test_estimate_cues_cover_voice_duration():
    cues = estimate_cues("Câu một ngắn. Câu thứ hai dài hơn một chút nhé! Và câu ba?", 12.0)
    assert len(cues) == 3 and cues[0].start == 0 and abs(cues[-1].end - 12.0) < 0.01
    assert all(b.start == a.end for a, b in zip(cues, cues[1:]))


def test_usage_recorded_per_connection(monkeypatch):
    monkeypatch.setenv("MRE_DATA_DIR", tempfile.mkdtemp())
    usage_service.record({"id": "c1"}, "claude-sonnet-x", NS(input_tokens=1_000_000, output_tokens=100_000,
                                                             cache_read_input_tokens=0, cache_creation_input_tokens=0))
    usage_service.record({"id": "c1"}, "claude-haiku-x", {"input_tokens": 1_000_000, "output_tokens": 0})
    u = usage_service.get("c1")
    assert u["requests"] == 2 and abs(u["usd"] - (3 + 1.5 + 1)) < 1e-6 and u["month_usd"] == u["usd"]
    usage_service.reset("c1")
    assert usage_service.get("c1")["requests"] == 0
