import shutil
import subprocess
from types import SimpleNamespace as NS

import pytest

np = pytest.importorskip("numpy")
from app.services import whisper_service  # noqa: E402

pytestmark = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="cần ffmpeg")


@pytest.fixture(autouse=True)
def _local_only(monkeypatch):
    """Các bài này thử nhánh Whisper TRÊN MÁY: giả lập đã cài faster-whisper và không có key API nhận giọng nói."""
    from app.services import stt_service
    monkeypatch.setattr(whisper_service, "local_available", lambda: True)
    monkeypatch.setattr(stt_service, "providers", lambda: [])


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
            return iter([NS(end=2.0, words=[W(" Xin", 0.0, 0.4), W(" chào.", 0.4, 1.3), W(" Tạm", 1.4, 1.7), W(" biệt.", 1.7, 2.0)])]), None
    monkeypatch.setattr(whisper_service, "_get_model", lambda name=None, device=None: (FakeModel(), "cpu"))
    monkeypatch.setattr(whisper_service, "is_cached", lambda name: True)
    cues = whisper_service.transcribe_to_cues(str(wav), "vi")
    assert isinstance(seen["audio"], np.ndarray)
    assert [c.text for c in cues][0].startswith("Xin chào.")


# ---------------------------------------------------------------- căn phụ đề theo kịch bản
SCRIPT = ("Lạm phát là khi giá cả tăng đều theo thời gian. Cùng một số tiền, bạn mua được ít hàng hơn trước. "
          "Ngân hàng trung ương dùng lãi suất để kìm lạm phát.")


def _words(text, t0=0.0, step=0.4, gap_after=()):
    out, t = [], t0
    for i, w in enumerate(text.split()):
        out.append(NS(word=" " + w, start=round(t, 2), end=round(t + step - 0.05, 2)))
        t += step + (0.8 if i in gap_after else 0)
    return out


def test_align_uses_script_text_and_voice_timing():
    # Whisper nghe nhầm vài từ ("lạm phác", "nhân hàng") và có khoảng lặng dài sau câu 1
    heard = SCRIPT.replace("Lạm phát là", "lạm phác là").replace("Ngân hàng", "nhân hàng")
    words = _words(heard, t0=0.5, gap_after=(10,))
    cues, ratio = whisper_service.align_script(SCRIPT, words, words[-1].end + 0.5)
    assert ratio > 0.85
    assert [c.text for c in cues][0] == "Lạm phát là khi giá cả tăng đều theo thời gian."   # chữ đúng kịch bản
    assert abs(cues[0].start - 0.5) < 0.05 and cues[0].end < cues[1].start + 1e-6
    assert abs(cues[1].start - words[11].start) < 0.05                                      # câu 2 bắt đầu sau khoảng lặng
    assert all(b.start >= a.end - 1e-6 for a, b in zip(cues, cues[1:]))
    assert cues[-1].end <= words[-1].end + 0.5


def test_script_lines_splits_long_sentences():
    long = "Đây là một câu rất dài, có nhiều vế, được viết ra để kiểm tra việc tách dòng phụ đề cho dễ đọc trên màn hình nhỏ."
    lines = whisper_service.script_lines(long)
    assert len(lines) >= 2 and all(len(x) <= 60 for x in lines) and " ".join(lines) == long


class _Model:
    def __init__(self, words, fail=False):
        self.words, self.fail, self.calls = words, fail, []

    def transcribe(self, audio, **kw):
        self.calls.append(kw)
        if self.fail:
            raise RuntimeError("Library cublas64_12.dll is not found or cannot be loaded")
        half = len(self.words) // 2
        return iter([NS(end=self.words[half - 1].end, words=self.words[:half]),
                     NS(end=self.words[-1].end, words=self.words[half:])]), None


def _wav(tmp_path, secs=12):
    wav = tmp_path / "v.wav"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", f"sine=d={secs}", str(wav)], check=True)
    return str(wav)


def test_script_mode_uses_fast_model_and_reports_progress(tmp_path, monkeypatch):
    m, used, msgs = _Model(_words(SCRIPT)), [], []
    monkeypatch.setattr(whisper_service, "is_cached", lambda n: False)
    monkeypatch.setattr(whisper_service, "_get_model", lambda name=None, device=None: (used.append(name) or m, "cpu"))
    cues = whisper_service.transcribe_to_cues(_wav(tmp_path), "vi", script=SCRIPT, progress=lambda f, t: msgs.append(t))
    assert used == ["base"] and m.calls[0]["beam_size"] == 1
    assert cues[0].text.startswith("Lạm phát là khi")
    assert "Đang tải mô hình Whisper “base”" in msgs[0] and any("%" in x for x in msgs)


def test_mismatched_script_falls_back_to_full_transcription(tmp_path, monkeypatch):
    m, used = _Model(_words("một hai ba bốn năm sáu bảy tám chín mười mười một mười hai")), []
    monkeypatch.setattr(whisper_service, "is_cached", lambda n: True)
    monkeypatch.setattr(whisper_service, "_get_model", lambda name=None, device=None: (used.append(name) or m, "cpu"))
    cues = whisper_service.transcribe_to_cues(_wav(tmp_path), "vi", script=SCRIPT)
    assert used == ["base", "small"] and "một hai" in cues[0].text


def test_gpu_error_falls_back_to_cpu(tmp_path, monkeypatch):
    gpu, cpu = _Model([], fail=True), _Model(_words("xin chào các bạn."))
    monkeypatch.setattr(whisper_service, "_cuda_broken", False)
    monkeypatch.setattr(whisper_service, "_cuda_ok", lambda: not whisper_service._cuda_broken)
    monkeypatch.setattr(whisper_service, "is_cached", lambda n: True)
    monkeypatch.setattr(whisper_service, "_get_model",
                        lambda name=None, device=None: (cpu, "cpu") if device == "cpu" else (gpu, "cuda"))
    words, total = whisper_service.transcribe_words(_wav(tmp_path, 3), "vi")
    assert len(words) == 4 and whisper_service._cuda_broken is True and abs(total - 3) < 0.1
