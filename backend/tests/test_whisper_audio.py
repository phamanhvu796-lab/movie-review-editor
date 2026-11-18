import shutil
import subprocess
from types import SimpleNamespace as NS

import pytest

np = pytest.importorskip("numpy")
from app.services import whisper_service  # noqa: E402

pytestmark = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="cần ffmpeg")


def test_load_audio_mp3_to_16k_mono_float(tmp_path):
    mp3 = tmp_path / "voice.mp3"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "sine=f=440:d=3:sample_rate=44100",
                    "-ac", "2", "-c:a", "libmp3lame", str(mp3)], check=True)
    a = whisper_service.load_audio(str(mp3))
    assert a.dtype == np.float32 and a.ndim == 1
    assert abs(len(a) / 16000 - 3.0) < 0.1 and 0.1 < float(np.abs(a).max()) <= 1.0   # sine của ffmpeg mặc định biên độ 1/8


def test_load_audio_bad_file_raises_clear_error(tmp_path):
    bad = tmp_path / "x.mp3"
    bad.write_bytes(b"not audio")
    with pytest.raises(RuntimeError, match="Không đọc được file giọng đọc"):
        whisper_service.load_audio(str(bad))


def test_transcribe_passes_array_not_path(tmp_path, monkeypatch):
    """faster-whisper nhận mảng âm thanh -> không gọi av.open() (nguồn lỗi metadata_errors)."""
    wav = tmp_path / "v.wav"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "sine=d=2", str(wav)], check=True)
    seen = {}
    W = lambda w, s, e: NS(word=w, start=s, end=e)  # noqa: E731

    class FakeModel:
        def transcribe(self, audio, **kw):
            seen["audio"] = audio
            return iter([NS(words=[W(" Xin", 0.0, 0.4), W(" chào.", 0.4, 1.3), W(" Tạm", 1.4, 1.7), W(" biệt.", 1.7, 2.0)])]), None
    monkeypatch.setattr(whisper_service, "_get_model", lambda: FakeModel())
    cues = whisper_service.transcribe_to_cues(str(wav), "vi")
    assert isinstance(seen["audio"], np.ndarray)
    assert [c.text for c in cues][0].startswith("Xin chào.")
