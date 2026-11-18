import subprocess

from ..config import settings
from ..utils.paths import find_binary
from ..schemas import Cue
from .srt_service import merge_short_cues

_model = None
_model_name = None
_SENTENCE_END = ".!?。！？…"


def _get_model():
    global _model, _model_name
    from faster_whisper import WhisperModel      # import muộn: thư viện nặng, tuỳ chọn
    if _model is None or _model_name != settings.whisper_model:
        _model = WhisperModel(settings.whisper_model, device="auto", compute_type="int8")
        _model_name = settings.whisper_model
    return _model


SAMPLE_RATE = 16000


def load_audio(path: str):
    """Giải mã âm thanh bằng FFmpeg đi kèm app -> mảng float32 16 kHz mono (đúng định dạng Whisper cần).
    Không dùng PyAV: bản PyAV mới đã bỏ tham số 'metadata_errors' mà faster-whisper truyền vào av.open(),
    gây lỗi "open() got an unexpected keyword argument 'metadata_errors'"."""
    import numpy as np
    from .ffmpeg_service import _FLAGS
    ff = find_binary("ffmpeg")
    if not ff:
        raise RuntimeError("Thiếu FFmpeg để đọc file giọng đọc")
    r = subprocess.run([ff, "-nostdin", "-hide_banner", "-loglevel", "error", "-i", str(path), "-vn", "-ac", "1",
                        "-ar", str(SAMPLE_RATE), "-f", "f32le", "-"], capture_output=True, creationflags=_FLAGS)
    if r.returncode != 0 or not r.stdout:
        raise RuntimeError("Không đọc được file giọng đọc: " + r.stderr.decode("utf-8", "replace")[-300:])
    return np.frombuffer(r.stdout, dtype=np.float32).copy()


def transcribe_to_cues(audio_path: str, language: str | None = None,
                       max_chars: int = 42, max_seconds: float = 5.0) -> list[Cue]:
    """Phụ đề từ chính file voice-over: word timestamps -> dòng ngắn, rồi gộp cue < 1.2s."""
    segments, info = _get_model().transcribe(load_audio(audio_path), language=language or None,
                                             word_timestamps=True, vad_filter=True)
    words = [w for seg in segments for w in (seg.words or [])]
    cues: list[Cue] = []
    buf: list = []

    def flush():
        text = "".join(w.word for w in buf).strip()
        if text:
            start, end = round(buf[0].start, 3), round(buf[-1].end, 3)
            cues.append(Cue(index=len(cues) + 1, start=start, end=end if end > start else start + 0.2, text=text))
        buf.clear()

    for w in words:
        if buf and (len("".join(x.word for x in buf)) + len(w.word) > max_chars or w.end - buf[0].start > max_seconds):
            flush()
        buf.append(w)
        if w.word.strip()[-1:] in _SENTENCE_END:
            flush()
    flush()
    return merge_short_cues(cues)
