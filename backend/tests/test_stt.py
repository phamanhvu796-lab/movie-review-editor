import json
import shutil
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from app.services import stt_service, whisper_service

pytestmark = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="cần ffmpeg")
SCRIPT = "Lạm phát là khi giá cả tăng đều theo thời gian. Cùng một số tiền, bạn mua được ít hàng hơn trước."


class H(BaseHTTPRequestHandler):
    log = []
    mode = {}

    def log_message(self, *a):
        pass

    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        H.log.append((self.path, self.headers, body))
        if self.path.endswith("/v1/speech-to-text"):
            if H.mode.get("eleven") == "401":
                return self._send(401, {"detail": "missing_permissions"})
            if b'name="language_code"' in body and H.mode.get("eleven") == "422lang":
                return self._send(422, {"detail": "bad language"})
            words, t = [], 0.3
            for w in SCRIPT.replace("Lạm phát", "lạm phác").split():
                words += [{"text": w, "start": t, "end": t + 0.3, "type": "word"}, {"text": " ", "start": t + 0.3, "end": t + 0.35, "type": "spacing"}]
                t += 0.4
            return self._send(200, {"language_code": "vie", "text": SCRIPT, "words": words + [{"text": "(cười)", "start": t, "end": t + 1, "type": "audio_event"}]})
        if self.path.endswith("/v1/audio/transcriptions"):
            return self._send(200, {"text": "xin chào các bạn.", "words": [
                {"word": "xin", "start": 0.0, "end": 0.3}, {"word": "chào", "start": 0.3, "end": 0.6},
                {"word": "các", "start": 0.6, "end": 0.8}, {"word": "bạn.", "start": 0.8, "end": 1.2}]})
        self._send(404, {})

    def _send(self, code, obj):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)


@pytest.fixture
def server():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    H.log.clear()
    H.mode.clear()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


@pytest.fixture
def voice(tmp_path):
    p = tmp_path / "voice.wav"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "sine=d=8:sample_rate=44100", "-ac", "2", str(p)], check=True)
    return str(p)


def _conns(monkeypatch, url, eleven=True, openai=True, mode="auto"):
    monkeypatch.setattr(stt_service, "mode", lambda: mode)
    c = {"elevenlabs": [{"id": "e", "name": "EL", "api_key": "el-key", "base_url": url}] if eleven else [],
         "openai_tts": [{"id": "o", "name": "OA", "api_key": "oa-key", "base_url": url}] if openai else []}
    monkeypatch.setattr(stt_service.settings, "usable_connections", lambda svc="llm": c.get(svc, []))


def test_elevenlabs_upload_and_script_alignment(server, voice, monkeypatch):
    _conns(monkeypatch, server)
    msgs = []
    cues = whisper_service.transcribe_to_cues(voice, "vi", script=SCRIPT, progress=lambda f, t: msgs.append(t))
    path, headers, body = H.log[0]
    assert path == "/v1/speech-to-text" and headers["xi-api-key"] == "el-key"
    assert b'name="model_id"\r\n\r\nscribe_v2' in body and b'name="timestamps_granularity"\r\n\r\nword' in body
    assert b'name="language_code"\r\n\r\nvi' in body and b"Content-Type: audio/mpeg" in body
    assert len(body) < 200_000                                          # đã nén mono 16 kHz trước khi gửi
    assert [c.text for c in cues] == ["Lạm phát là khi giá cả tăng đều theo thời gian.",
                                      "Cùng một số tiền, bạn mua được ít hàng hơn trước."]   # chữ đúng theo kịch bản
    assert abs(cues[0].start - 0.3) < 0.01 and any("ElevenLabs Scribe" in m for m in msgs)


def test_language_retry_and_audio_events_skipped(server, voice, monkeypatch):
    _conns(monkeypatch, server, openai=False)
    H.mode["eleven"] = "422lang"
    words, total, kind = stt_service.transcribe_words(voice, "vi")
    assert kind == "elevenlabs" and len(H.log) == 2 and b"language_code" not in H.log[1][2]
    assert all("cười" not in w.word for w in words) and abs(total - 8) < 0.1


def test_falls_back_to_openai_when_elevenlabs_key_lacks_permission(server, voice, monkeypatch):
    _conns(monkeypatch, server)
    H.mode["eleven"] = "401"
    cues = whisper_service.transcribe_to_cues(voice, "vi")
    path, headers, body = H.log[1]
    assert path == "/v1/audio/transcriptions" and headers["Authorization"] == "Bearer oa-key"
    assert b'name="timestamp_granularities[]"\r\n\r\nword' in body and b'name="model"\r\n\r\nwhisper-1' in body
    assert cues[0].text == "xin chào các bạn." and cues[0].end == 1.2


def test_local_mode_and_no_keys_skip_api(server, voice, monkeypatch):
    _conns(monkeypatch, server, mode="local")
    assert stt_service.transcribe_words(voice) is None and not H.log
    _conns(monkeypatch, server, eleven=False, openai=False)
    assert stt_service.providers() == []


def test_all_api_fail_without_local_whisper_gives_clear_error(server, voice, monkeypatch):
    _conns(monkeypatch, server, openai=False)
    H.mode["eleven"] = "401"
    monkeypatch.setattr(whisper_service, "local_available", lambda: False)
    with pytest.raises(stt_service.STTError, match="thiếu quyền"):
        whisper_service.transcribe_to_cues(voice, "vi")
