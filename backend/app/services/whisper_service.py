from ..config import settings
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


def transcribe_to_cues(audio_path: str, language: str | None = None,
                       max_chars: int = 42, max_seconds: float = 5.0) -> list[Cue]:
    """Phụ đề từ chính file voice-over: word timestamps -> dòng ngắn, rồi gộp cue < 1.2s."""
    segments, info = _get_model().transcribe(str(audio_path), language=language or None,
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
