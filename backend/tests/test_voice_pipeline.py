import json
import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest

from app.config import settings
from app.services import ffmpeg_install as FI
from app.services import tts_service as T

needs_ffmpeg = pytest.mark.skipif(not (shutil.which("ffmpeg") and shutil.which("ffprobe")), reason="cần FFmpeg trong PATH")


def mp3(seconds=1.0) -> bytes:
    return subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
                           "-c:a", "libmp3lame", "-f", "mp3", "pipe:1"], capture_output=True, check=True).stdout


def src(i, svc, model="", base=""):
    return {"id": i, "service": svc, "name": i, "protocol": "x", "base_url": base, "api_key": f"k-{i}-123456", "model": model, "enabled": True}


@pytest.fixture(autouse=True)
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("MRE_DATA_DIR", str(tmp_path))
    for k in ("ELEVENLABS_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(k, raising=False)


def test_ensure_audio_rejects_json_and_html_returned_with_http_200():
    for bad in (b'{"error":{"message":"model not found"}}', b"<html>Cloudflare</html>", b""):
        with pytest.raises(RuntimeError, match="không trả về âm thanh"):
            T._ensure_audio(bad)
    assert T._ensure_audio(b"ID3\x04\x00") == b"ID3\x04\x00"


def test_openai_model_comes_from_source_and_falls_back_when_relay_lacks_it(monkeypatch):
    settings.save_connections([src("o", "openai_tts", "", "https://relay.example.com")], "o", "openai_tts")
    tried = []

    def relay(url, headers, payload=None, method="POST"):
        tried.append(payload["model"])
        if payload["model"] != "tts-1":
            raise T.TTSError(f"TTS HTTP 404: The model `{payload['model']}` does not exist", 404)
        return b"ID3audio"
    monkeypatch.setattr(T, "_request", relay)
    T.synthesize("x", "openai", "alloy")
    assert tried == ["gpt-4o-mini-tts", "tts-1"]
    tried.clear()
    settings.save_connections([src("o", "openai_tts", "tts-1", "https://relay.example.com")], "o", "openai_tts")
    T.synthesize("y", "openai", "nova")
    assert tried == ["tts-1"]                               # model đã cấu hình cho nguồn được dùng trước


def test_extract_ffmpeg_takes_only_ffmpeg_and_ffprobe(tmp_path):
    z = tmp_path / "f.zip"
    with zipfile.ZipFile(z, "w") as zf:
        for n in ("pkg/bin/ffmpeg.exe", "pkg/bin/ffprobe.exe", "pkg/bin/ffplay.exe", "pkg/README.txt"):
            zf.writestr(n, b"x")
    dest = tmp_path / "bin"
    assert sorted(FI.extract_ffmpeg(z, dest)) == ["ffmpeg.exe", "ffprobe.exe"]
    assert sorted(p.name for p in dest.iterdir()) == ["ffmpeg.exe", "ffprobe.exe"]


@needs_ffmpeg
def test_build_voice_end_to_end_with_real_ffmpeg(monkeypatch, tmp_path):
    from app.services.ffmpeg_service import probe
    settings.save_connections([src("e", "elevenlabs")], "e", "elevenlabs")
    calls = []

    def fake(url, headers, payload=None, method="POST"):
        if method == "GET":
            return json.dumps({"character_limit": 100000, "character_count": 10}).encode()
        calls.append(payload["text"])
        return mp3(2.0)
    monkeypatch.setattr(T, "_request", fake)
    text = " ".join(f"Đây là câu thứ {i} của kịch bản review phim, nói về diễn xuất và quay phim rất ấn tượng." for i in range(1, 9))
    out = tmp_path / "voice.wav"
    chunks, durs = T.build_voice(text, out, tmp_path / "w", "elevenlabs", "V", "narrative", 1.0, "high")
    assert len(chunks) >= 3 and abs(probe(out)["duration"] - sum(durs)) < 0.05
    cues = T.cues_from_chunks(chunks, durs)
    assert cues[0].start == 0 and abs(cues[-1].end - sum(durs)) < 0.1
    n = len(calls)
    T.build_voice(text, tmp_path / "v2.wav", tmp_path / "w2", "elevenlabs", "V", "narrative", 1.0, "high")
    assert len(calls) == n                                   # tạo lại: lấy từ cache, không tốn thêm credit


@needs_ffmpeg
def test_diagnose_reports_each_step(monkeypatch):
    settings.save_connections([src("o", "openai_tts", "tts-1")], "o", "openai_tts")
    monkeypatch.setattr(T, "_request", lambda *a, **k: mp3(2))
    r = T.diagnose("openai", "nova")
    assert r["ok"] and "Giải mã bằng FFmpeg OK" in "\n".join(r["trace"])
    monkeypatch.setattr(T, "_request", lambda *a, **k: b"<html>Cloudflare</html>")
    r = T.diagnose("openai", "nova")
    assert not r["ok"] and "Cloudflare" in r["detail"]
    r = T.diagnose("elevenlabs", "V")                        # chưa có nguồn ElevenLabs nào
    assert not r["ok"] and "Chưa có nguồn" in r["detail"]
