import json
from types import SimpleNamespace as NS

import pytest

from app.services import claude_client as cc
from app.services.openai_compat import convert_messages, convert_response, pick_model


def test_pick_model_prefers_newest_claude_sonnet():
    assert pick_model(["gpt-4o", "claude-haiku-4", "claude-sonnet-4", "claude-sonnet-4-5"]) == "claude-sonnet-4-5"
    assert pick_model(["gpt-4o", "claude-opus-4"]) == "claude-opus-4" and pick_model(["a", "b"]) == "a" and pick_model([]) is None


def test_convert_messages_tool_roundtrip_and_images():
    resp_blocks = [NS(type="tool_use", id="t1", name="plan", input={"a": 1})]
    msgs = [
        {"role": "user", "content": [{"type": "text", "text": "A"}, {"type": "text", "text": "B", "cache_control": {"type": "ephemeral"}}]},
        {"role": "assistant", "content": resp_blocks},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "is_error": True, "content": "sai"}]},
        {"role": "user", "content": [{"type": "text", "text": "x"}, {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": "QQ=="}}]},
    ]
    out = convert_messages("SYS", msgs)
    assert out[0] == {"role": "system", "content": "SYS"} and out[1] == {"role": "user", "content": "A\n\nB"}
    assert out[2]["tool_calls"][0]["function"]["arguments"] == '{"a": 1}' and out[2]["content"] is None
    assert out[3] == {"role": "tool", "tool_call_id": "t1", "content": "sai"}
    assert out[4]["content"][1]["image_url"]["url"] == "data:image/jpeg;base64,QQ=="


def test_convert_response_tool_call_and_json_in_text_fallback():
    r = convert_response({"choices": [{"finish_reason": "tool_calls", "message": {"content": None, "tool_calls": [
        {"id": "c1", "function": {"name": "plan", "arguments": '{"x": 2}'}}]}}]})
    assert r.stop_reason == "tool_use" and r.content[0].input == {"x": 2} and r.content[0].name == "plan"
    tools = [{"name": "plan"}]
    r = convert_response({"choices": [{"finish_reason": "stop", "message": {"content": 'Đây: {"x": 3} xong'}}]}, tools, {"type": "tool", "name": "plan"})
    assert [b.type for b in r.content] == ["text", "tool_use"] and r.content[1].input == {"x": 3}
    r = convert_response({"choices": [{"finish_reason": "length", "message": {"content": "cắt"}}]})
    assert r.stop_reason == "max_tokens"


@pytest.fixture
def sources(monkeypatch, tmp_path):
    monkeypatch.setenv("MRE_DATA_DIR", str(tmp_path))
    from app.config import settings
    settings.save_connections([
        {"id": "a", "name": "A", "protocol": "anthropic", "base_url": "", "api_key": "k1234567890", "model": "ma", "fast_model": "", "enabled": True},
        {"id": "b", "name": "B", "protocol": "openai", "base_url": "https://b.com", "api_key": "k2234567890", "model": "mb", "fast_model": "fb", "enabled": True}], "a")
    return settings


def _fake(calls, name, fail=None):
    def create(**kw):
        calls.append((name, kw["model"]))
        if fail:
            raise fail
        return NS(content=[], stop_reason="end_turn")
    return NS(messages=NS(create=create))


def test_failover_to_next_source_and_model_mapping(monkeypatch, sources):
    calls = []
    err = RuntimeError("HTTP 503: quá tải")
    monkeypatch.setattr(cc, "_client_for", lambda conn, retries: _fake(calls, conn["id"], err if conn["id"] == "a" else None))
    cc.get_client().messages.create(model=sources.claude_fast_model, max_tokens=1, messages=[])
    assert calls == [("a", "ma"), ("b", "fb")]          # nguồn A lỗi -> sang B, dùng model nhanh của B


def test_bad_request_is_not_retried_on_other_sources(monkeypatch, sources):
    calls = []
    monkeypatch.setattr(cc, "_client_for", lambda conn, retries: _fake(calls, conn["id"], RuntimeError("HTTP 400: bad")))
    with pytest.raises(RuntimeError):
        cc.get_client().messages.create(model="x", max_tokens=1, messages=[])
    assert len(calls) == 1


def test_active_source_goes_first_and_disabled_is_skipped(monkeypatch, sources):
    calls = []
    sources.save_connections([{**c, "enabled": c["id"] != "a"} for c in sources.connections()], "a")
    monkeypatch.setattr(cc, "_client_for", lambda conn, retries: _fake(calls, conn["id"]))
    cc.get_client().messages.create(model="x", max_tokens=1, messages=[])
    assert calls == [("b", "mb")] and sources.anthropic_api_key == "k2234567890"


def test_no_sources_gives_clear_error(monkeypatch, tmp_path):
    monkeypatch.setenv("MRE_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="nguồn kết nối"):
        cc.get_client().messages.create(model="x", max_tokens=1, messages=[])


def test_legacy_single_key_still_works(monkeypatch, tmp_path):
    monkeypatch.setenv("MRE_DATA_DIR", str(tmp_path))
    (tmp_path / "settings.json").write_text(json.dumps({"anthropic_api_key": "sk-legacy-123456", "anthropic_base_url": "https://r.com/v1"}))
    from app.config import settings
    c = settings.connections()[0]
    assert c["api_key"] == "sk-legacy-123456" and c["base_url"] == "https://r.com/v1" and settings.anthropic_api_key == "sk-legacy-123456"


def test_each_service_has_its_own_sources_and_active(tmp_path, monkeypatch):
    monkeypatch.setenv("MRE_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
    from app.config import settings
    mk = lambda i, k, svc: {"id": i, "service": svc, "name": i, "protocol": "x", "base_url": "", "api_key": k, "enabled": True}
    settings.save_connections([mk("l1", "claude-key-1234", "llm")], "l1", "llm")
    settings.save_connections([mk("e1", "eleven-key-111", "elevenlabs"), mk("e2", "eleven-key-222", "elevenlabs")], "e2", "elevenlabs")
    assert [c["id"] for c in settings.usable_connections("llm")] == ["l1"]
    assert [c["id"] for c in settings.usable_connections("elevenlabs")] == ["e2", "e1"]      # nguồn đang chọn đứng đầu
    assert settings.elevenlabs_api_key == "eleven-key-222" and settings.openai_api_key == ""
    settings.save_connections([], "", "elevenlabs")                                          # xoá hết không ảnh hưởng dịch vụ khác
    assert settings.elevenlabs_api_key == "" and settings.anthropic_api_key == "claude-key-1234"


def test_cloudflare_1010_is_explained_not_reported_as_bad_key():
    from app.services.api_errors import friendly
    for raw in ("HTTP 403: error code: 1010", "Error code: 403 - error code: 1010"):
        msg = friendly(Exception(raw))
        assert "Cloudflare" in msg and "1010" in msg and "key không có quyền" not in msg


def test_openai_compat_sends_browser_user_agent_and_allows_override(monkeypatch):
    import urllib.request
    from app.config import DEFAULT_UA
    from app.services.openai_compat import OpenAICompat
    seen = []

    class R:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return b'{"choices":[{"finish_reason":"stop","message":{"content":"ok"}}]}'
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=0: seen.append(req.get_header("User-agent")) or R())
    OpenAICompat("https://r.com/v1", "sk-x").messages.create(model="m", messages=[{"role": "user", "content": "hi"}])
    OpenAICompat("https://r.com/v1", "sk-x", "MyAgent/1.0").messages.create(model="m", messages=[{"role": "user", "content": "hi"}])
    assert seen == [DEFAULT_UA, "MyAgent/1.0"] and "Mozilla" in DEFAULT_UA


def test_same_error_from_both_protocols_is_reported_once(monkeypatch):
    from app.services import llm_sources as ls

    def boom(conn):
        return NS(messages=NS(create=lambda **kw: (_ for _ in ()).throw(RuntimeError("HTTP 403: error code: 1010"))))
    monkeypatch.setattr(ls, "build_single_client", boom)
    monkeypatch.setattr(ls, "probe_models", lambda c: [])
    r = ls.test_connection({"id": "x", "protocol": "auto", "base_url": "https://r.com/v1", "api_key": "sk-good-1234567", "model": ""})
    assert not r["ok"] and r["detail"].count("Cloudflare") == 1 and "|" not in r["detail"]
