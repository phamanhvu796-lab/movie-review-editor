import json
from types import SimpleNamespace as NS

from app.services import chat_service


def _client(inp=None, text=None, seen=None):
    def create(**kw):
        if seen is not None:
            seen.append(kw)
        if text is not None:
            return NS(content=[NS(type="text", text=text)])
        return NS(content=[NS(type="tool_use", input=inp)])
    return NS(messages=NS(create=create))


INFO = {"title": "Lãi suất", "mode": "kịch bản → video", "items": [{"label": "Giọng đọc", "status": "ok", "detail": ""}],
        "styles": {"fin_faceless": "Faceless"}}


def test_director_starts_auto_with_style_and_topic():
    seen = []
    d = chat_service.director(INFO, [], "làm video tài chính về lãi suất",
                              _client({"reply": "Mình bắt đầu nhé: tìm hình → dựng → nhạc → xuất 1080p.", "action": "auto_create",
                                       "style": "fin_faceless", "topic": "lãi suất", "quality": "final"}, seen=seen))
    assert d["action"] == "auto_create" and d["style"] == "fin_faceless" and d["topic"] == "lãi suất" and d["quality"] == "final"
    body = seen[0]["messages"][-1]["content"]
    assert json.loads(body.split("\n\nNgười dùng:")[0])["project"]["title"] == "Lãi suất"
    assert seen[0]["tool_choice"]["name"] == "director"


def test_director_history_skips_progress_and_starts_with_user():
    hist = [{"role": "assistant", "text": "chào"}, {"role": "user", "text": "a"},
            {"role": "assistant", "kind": "progress", "text": "Đang tìm hình 3/9", "steps": ["x"]},
            {"role": "assistant", "kind": "summary", "text": "Xong rồi"}]
    seen = []
    chat_service.director(INFO, hist, "đổi phong cách", _client({"reply": "ok", "action": "none"}, seen=seen))
    msgs = seen[0]["messages"]
    assert msgs[0]["role"] == "user" and all("Đang tìm hình" not in str(m["content"]) for m in msgs)
    assert [m["role"] for m in msgs] == ["user", "assistant", "user"]


def test_director_sanitises_bad_output():
    d = chat_service.director(INFO, [], "?", _client({"reply": "", "action": "xoá hết", "quality": "4k"}))
    assert d["action"] == "none" and d["quality"] == "final" and d["reply"]


def test_edit_tool_auto_create_action():
    _, _, acts, notes = chat_service.apply_operations({"sections": []}, [{"op": "auto_create", "style": "news_short"}], {})
    assert acts["auto"] == {"style": "news_short", "quality": "final"} and notes == ["Tự động làm trọn gói"]


def test_summarize_uses_fast_model_text():
    seen = []
    t = chat_service.summarize({"notes": ["Đã tải 12 ảnh"], "exporting": {"resolution": "1920x1080"}},
                               _client(text="Xong rồi! Mình đang xuất video 1080p.", seen=seen))
    assert t.startswith("Xong rồi") and "tools" not in seen[0]
