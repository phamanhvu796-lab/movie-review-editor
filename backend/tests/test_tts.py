import json
from types import SimpleNamespace as NS

from app.services import tts_service as T
from app.services.metadata_service import TOOL, generate_metadata


def test_split_script_groups_sentences():
    assert T.split_script("Một. Hai! Ba?", max_chars=10) == ["Một. Hai!", "Ba?"]


def test_split_long_sentence_and_cjk():
    long = " ".join(["từ"] * 200)
    assert all(len(c) <= 100 for c in T.split_script(long, max_chars=100))
    assert T.split_script("你好。我是谁。", max_chars=4) == ["你好。", "我是谁。"]


def test_decimal_numbers_are_not_split():
    assert T.split_sentences("Phim đạt 8.5 điểm. Rất hay.") == ["Phim đạt 8.5 điểm.", "Rất hay."]


def test_cues_cover_real_audio_duration():
    chunks = ["Xin chào các bạn. Hôm nay ta bàn về phim."]
    cues = T.cues_from_chunks(chunks, [6.0])
    assert cues[0].start == 0 and abs(cues[-1].end - 6.0) < 0.01 and len(cues) == 2
    assert all(abs(a.end - b.start) < 0.01 for a, b in zip(cues, cues[1:]))


def test_cues_accumulate_across_chunks():
    cues = T.cues_from_chunks(["Đoạn một dài đủ rồi nhé.", "Đoạn hai cũng vậy đó."], [3.0, 4.0])
    assert abs(cues[-1].end - 7.0) < 0.01


def test_elevenlabs_request(monkeypatch, tmp_path):
    monkeypatch.setenv("MRE_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ELEVENLABS_API_KEY", "k")
    seen = {}
    monkeypatch.setattr(T, "_request", lambda url, headers, payload=None, method="POST": seen.update(
        url=url, headers=headers, payload=payload) or b"mp3")
    assert T.synthesize("Xin chào", "elevenlabs", "VOICE1", "dramatic", 5.0) == b"mp3"
    assert "VOICE1" in seen["url"] and seen["headers"]["xi-api-key"] == "k"
    assert seen["payload"]["voice_settings"]["speed"] == 1.2          # bị kẹp về giới hạn
    assert seen["payload"]["voice_settings"]["stability"] == 0.3


def test_openai_request_and_missing_key(monkeypatch, tmp_path):
    monkeypatch.setenv("MRE_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    try:
        T.synthesize("x", "openai", "alloy")
        assert False
    except RuntimeError as e:
        assert "OpenAI" in str(e)
    monkeypatch.setenv("OPENAI_API_KEY", "sk")
    seen = {}
    monkeypatch.setattr(T, "_request", lambda url, headers, payload=None, method="POST": seen.update(payload=payload) or b"x")
    T.synthesize("x", "openai", "nova", "calm", 1.0)
    assert seen["payload"]["voice"] == "nova" and "instructions" in seen["payload"]


def test_metadata_uses_tool_output():
    out = {"titles": ["a"], "description": "d", "hashtags": ["#x"], "thumbnail_text": ["t"]}
    calls = []
    client = NS(messages=NS(create=lambda **kw: calls.append(kw) or NS(
        content=[NS(type="tool_use", name=TOOL["name"], input=out)])))
    assert generate_metadata("Phim", "kinh dị", "vi", "dir", "kịch bản", client=client) == out
    assert "Không tiết lộ" in calls[0]["system"]


def test_tts_cache_avoids_second_api_call(monkeypatch, tmp_path):
    monkeypatch.setenv("MRE_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ELEVENLABS_API_KEY", "k")
    n = []
    monkeypatch.setattr(T, "_request", lambda *a, **k: n.append(1) or b"mp3")
    assert T.synthesize_cached("Xin chào", "elevenlabs", "V", "narrative", 1.0, "high", None, "sau") == b"mp3"
    assert T.synthesize_cached("Xin chào", "elevenlabs", "V", "narrative", 1.0, "high", "khác", None) == b"mp3"
    assert len(n) == 1                                   # prev/next_text không nằm trong khoá cache
    T.synthesize_cached("Xin chào", "elevenlabs", "V", "narrative", 1.0, "fast")
    assert len(n) == 2                                   # đổi model thì phải tạo lại


def test_context_fallback_when_model_rejects_previous_text(monkeypatch, tmp_path):
    monkeypatch.setenv("MRE_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ELEVENLABS_API_KEY", "k")
    sent = []

    def fake(url, headers, payload=None, method="POST"):
        sent.append(dict(payload))
        if "previous_text" in payload:
            raise RuntimeError("TTS HTTP 422: previous_text not supported")
        return b"ok"
    monkeypatch.setattr(T, "_request", fake)
    assert T.synthesize("x", "elevenlabs", "V", prev_text="trước", next_text="sau", quality="fast") == b"ok"
    assert len(sent) == 2 and "previous_text" not in sent[1]
    assert sent[0]["model_id"] == "eleven_flash_v2_5"


def test_quota_checked_before_spending(monkeypatch, tmp_path):
    import pytest
    monkeypatch.setenv("MRE_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ELEVENLABS_API_KEY", "k")
    monkeypatch.setattr(T, "get_quota", lambda: {"used": 95, "limit": 100, "remaining": 5})
    monkeypatch.setattr(T, "synthesize", lambda *a, **k: (_ for _ in ()).throw(AssertionError("không được gọi API")))
    with pytest.raises(RuntimeError, match="quota"):
        T.build_voice("x" * 50, tmp_path / "o.wav", tmp_path / "w", "elevenlabs", "V", "narrative", 1.0)


def test_retry_honors_retry_after_and_explains_errors(monkeypatch):
    import io
    import urllib.error
    sleeps = []
    monkeypatch.setattr(T.time, "sleep", sleeps.append)

    class Ok:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return b"audio"
    calls = []

    def fake_urlopen(req, timeout=0):
        calls.append(1)
        if len(calls) == 1:
            raise urllib.error.HTTPError("u", 429, "x", {"Retry-After": "3"}, io.BytesIO(b'{"detail":{"status":"too_many_concurrent_requests"}}'))
        return Ok()
    monkeypatch.setattr(T.urllib.request, "urlopen", fake_urlopen)
    assert T._request("http://x", {}, {"a": 1}) == b"audio"
    assert len(sleeps) == 1 and 3 <= sleeps[0] <= 3.5
    assert "quota" in T._explain(401, '{"detail":{"status":"quota_exceeded","message":"m"}}')
    assert "thiếu quyền" in T._explain(401, '{"detail":{"status":"missing_permissions","message":"user_read"}}')
    assert T._explain(400, '{"error":{"message":"bad voice"}}') == "TTS HTTP 400: bad voice"


def _eleven_sources(tmp_path, monkeypatch):
    monkeypatch.setenv("MRE_DATA_DIR", str(tmp_path))
    from app.config import settings
    settings.save_connections([
        {"id": "a", "name": "A", "base_url": "https://a.example.com/v1", "api_key": "key-a-111111", "enabled": True},
        {"id": "b", "name": "B", "base_url": "", "api_key": "key-b-222222", "enabled": True}], "a", "elevenlabs")


def test_elevenlabs_fails_over_to_next_key_when_quota_exhausted(monkeypatch, tmp_path):
    _eleven_sources(tmp_path, monkeypatch)
    seen = []

    def fake(url, headers, payload=None, method="POST"):
        seen.append((url.split("/v1/")[0], headers["xi-api-key"]))
        if headers["xi-api-key"] == "key-a-111111":
            raise T.TTSError("Hết quota ký tự của gói ElevenLabs.", 401)
        return b"audio-from-b"
    monkeypatch.setattr(T, "_request", fake)
    assert T.synthesize("Xin chào", "elevenlabs", "V") == b"audio-from-b"
    assert seen == [("https://a.example.com", "key-a-111111"), ("https://api.elevenlabs.io", "key-b-222222")]   # mỗi nguồn dùng Base URL riêng


def test_bad_request_does_not_burn_other_keys(monkeypatch, tmp_path):
    import pytest
    _eleven_sources(tmp_path, monkeypatch)
    n = []
    monkeypatch.setattr(T, "_request", lambda *a, **k: n.append(1) or (_ for _ in ()).throw(T.TTSError("TTS HTTP 404: voice not found", 404)))
    with pytest.raises(RuntimeError, match="404"):
        T.synthesize("x", "elevenlabs", "V")
    assert len(n) == 1


def test_quota_is_summed_across_sources(monkeypatch, tmp_path):
    _eleven_sources(tmp_path, monkeypatch)
    data = {"key-a-111111": {"character_limit": 10000, "character_count": 9000},
            "key-b-222222": {"character_limit": 30000, "character_count": 5000}}
    monkeypatch.setattr(T, "_request", lambda url, headers, payload=None, method="GET": json.dumps(data[headers["xi-api-key"]]).encode())
    q = T.get_quota()
    assert q["limit"] == 40000 and q["used"] == 14000 and q["remaining"] == 26000 and len(q["sources"]) == 2
