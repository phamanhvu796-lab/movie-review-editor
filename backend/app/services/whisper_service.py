import difflib
import os
import re
import subprocess
import unicodedata

from ..config import settings
from ..utils.paths import find_binary
from ..schemas import Cue
from .srt_service import merge_short_cues

_models: dict = {}
_cuda_broken = False
_SENTENCE_END = ".!?。！？…"
FAST_MODEL = "base"          # có kịch bản: chỉ cần mốc thời gian -> mô hình nhỏ (~140 MB), nhanh gấp ~4 lần "small"
_MODEL_MB = {"tiny": 75, "base": 145, "small": 480, "medium": 1500, "large-v3": 3000}


def _cuda_ok() -> bool:
    if _cuda_broken:
        return False
    try:
        import ctranslate2
        return ctranslate2.get_cuda_device_count() > 0
    except Exception:
        return False


def _cpu_threads() -> int:
    try:
        from .sysinfo import cpu_threads
        n = cpu_threads()
    except Exception:
        n = 0
    return n or max(2, (os.cpu_count() or 4) - 1)


def is_cached(name: str) -> bool:
    try:
        from faster_whisper.utils import download_model
        download_model(name, local_files_only=True)
        return True
    except Exception:
        return False


def _get_model(name: str | None = None, device: str | None = None):
    from faster_whisper import WhisperModel      # import muộn: thư viện nặng, tuỳ chọn
    name = name or settings.whisper_model
    device = device or ("cuda" if _cuda_ok() else "cpu")
    key = (name, device)
    if key not in _models:
        _models.clear()                          # chỉ giữ 1 mô hình trong RAM
        _models[key] = WhisperModel(name, device=device, compute_type="int8_float16" if device == "cuda" else "int8",
                                    cpu_threads=_cpu_threads())
    return _models[key], device


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


def transcribe_words(audio_path: str, language: str | None = None, model_name: str | None = None,
                     progress=None, beam_size: int = 1) -> tuple[list, float]:
    """Chạy Whisper, trả (danh sách từ có mốc thời gian, tổng giây). progress(phân_số, câu_mô_tả) để báo tiến độ.
    Có GPU NVIDIA -> chạy GPU; GPU thiếu thư viện CUDA (cublas/cudnn) -> tự chuyển CPU và nhớ cho các lần sau."""
    global _cuda_broken
    name = model_name or settings.whisper_model
    say = progress or (lambda f, t: None)
    if not is_cached(name):
        say(None, f"Đang tải mô hình Whisper “{name}” (~{_MODEL_MB.get(name, 300)} MB, chỉ lần đầu)…")
    audio = load_audio(audio_path)
    total = len(audio) / SAMPLE_RATE

    def run(device=None):
        model, dev = _get_model(name, device)
        say(0.0, f"Đang nghe giọng đọc ({'GPU' if dev == 'cuda' else 'CPU'})… 0%")
        segs, _ = model.transcribe(audio, language=language or None, word_timestamps=True, vad_filter=True,
                                   beam_size=beam_size, condition_on_previous_text=False)
        words, last = [], -1
        for seg in segs:                         # máy chạy thật ở vòng lặp này -> báo % theo vị trí đã nghe
            words += list(seg.words or [])
            pct = int(min(99, seg.end / max(total, 1) * 100))
            if pct >= last + 5:
                last = pct
                say(pct / 100, f"Đang nghe giọng đọc ({'GPU' if dev == 'cuda' else 'CPU'})… {pct}%")
        return words, dev

    try:
        words, _ = run()
    except Exception:
        if _cuda_ok():                           # lỗi phía GPU -> thử lại bằng CPU
            _cuda_broken = True
            _models.clear()
            words, _ = run("cpu")
        else:
            raise
    return words, total


def _norm(t: str) -> str:
    t = unicodedata.normalize("NFC", t.lower())
    return "".join(ch for ch in t if ch.isalnum())


def script_lines(script: str, max_chars: int = 60) -> list[str]:
    """Chia kịch bản thành dòng phụ đề vừa mắt: theo câu, câu dài tách ở dấu phẩy/khoảng trắng."""
    from .tts_service import split_sentences
    out = []
    for sent in split_sentences(script):
        s = sent.strip()
        while len(s) > max_chars:
            cut = max(s.rfind(", ", 0, max_chars), s.rfind("; ", 0, max_chars))
            cut = cut + 1 if cut > max_chars // 3 else s.rfind(" ", 0, max_chars)
            cut = cut if cut > 15 else max_chars
            out.append(s[:cut].strip())
            s = s[cut:].strip()
        if s:
            out.append(s)
    return out


def align_script(script: str, words: list, total: float) -> tuple[list[Cue], float]:
    """Phụ đề = CHỮ của kịch bản + THỜI GIAN Whisper nghe được. So khớp từng từ (bỏ dấu câu, chữ thường),
    từ không khớp (Whisper nghe nhầm) thì nội suy giữa các điểm khớp gần nhất."""
    lines = script_lines(script)
    toks, owner = [], []
    for i, ln in enumerate(lines):
        for w in re.split(r"\s+", ln):
            n = _norm(w)
            if n:
                toks.append(n)
                owner.append(i)
    tw = [(_norm(w.word), w.start, w.end) for w in words if _norm(w.word)]
    if not toks or not tw:
        return [], 0.0
    sm = difflib.SequenceMatcher(None, toks, [t[0] for t in tw], autojunk=False)
    blocks = sm.get_matching_blocks()
    ratio = sum(n for _, _, n in blocks) / len(toks)       # tỉ lệ từ kịch bản khớp với lời Whisper nghe được
    anchors = [(0.0, 0.0)]
    for a, b, n in blocks:
        for k in range(n):
            anchors.append((a + k, b + k))
    anchors.append((float(len(toks) - 1), float(len(tw) - 1)))
    anchors.sort()

    def to_t(si: float) -> float:
        lo = max((x for x in anchors if x[0] <= si), default=anchors[0])
        hi = min((x for x in anchors if x[0] >= si), default=anchors[-1])
        if hi[0] == lo[0]:
            return lo[1]
        return lo[1] + (si - lo[0]) * (hi[1] - lo[1]) / (hi[0] - lo[0])

    def at(ti: float, edge: str) -> float:
        i = min(len(tw) - 1, max(0, int(round(ti))))
        return tw[i][1] if edge == "start" else tw[i][2]

    first = {}
    last = {}
    for k, o in enumerate(owner):
        first.setdefault(o, k)
        last[o] = k
    cues: list[Cue] = []
    for i, ln in enumerate(lines):
        if i not in first:
            continue
        st, en = at(to_t(first[i]), "start"), at(to_t(last[i]), "end")
        if cues and st < cues[-1].end:
            st = cues[-1].end
        en = max(en, st + 0.4)
        cues.append(Cue(index=len(cues) + 1, start=round(st, 3), end=round(min(en, total) if total else en, 3), text=ln))
    for a, b in zip(cues, cues[1:]):             # lấp khoảng hở nhỏ để chữ không nhấp nháy
        if 0 < b.start - a.end < 0.35:
            a.end = b.start
    return [c for c in cues if c.end > c.start], ratio


def local_available() -> bool:
    import importlib.util
    return importlib.util.find_spec("faster_whisper") is not None


def can_transcribe() -> bool:
    """Có cách tạo phụ đề từ giọng đọc: API nhận giọng nói (ElevenLabs/OpenAI) hoặc Whisper trên máy."""
    from . import stt_service
    return bool(stt_service.providers()) or local_available()


def _words_to_cues(words: list, max_chars: int = 42, max_seconds: float = 5.0) -> list[Cue]:
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


def transcribe_to_cues(audio_path: str, language: str | None = None, max_chars: int = 42, max_seconds: float = 5.0,
                       script: str | None = None, progress=None) -> list[Cue]:
    """1) API nhận giọng nói (ElevenLabs Scribe / OpenAI) nếu đã gắn key — nhanh nhất, không tốn máy.
    2) Không có / lỗi -> Whisper trên máy. Có kịch bản: chữ phụ đề lấy đúng theo kịch bản, chỉ lấy thời gian từ giọng."""
    from . import stt_service
    say = progress or (lambda f, t: None)
    try:
        api = stt_service.transcribe_words(audio_path, language, say)
    except stt_service.STTError as e:
        if not local_available():
            raise
        say(None, f"API nhận giọng nói lỗi ({str(e)[:120]}) — chuyển sang Whisper trên máy…")
        api = None
    if api:
        words, total, _ = api
        if script and script.strip():
            cues, ratio = align_script(script, words, total)
            if cues and ratio >= 0.4:
                return cues
        return _words_to_cues(words, max_chars, max_seconds)
    if not local_available():
        raise RuntimeError("Chưa có cách tạo phụ đề từ giọng đọc: gắn API key ElevenLabs hoặc OpenAI ở ⚙ Cài đặt, "
                           "hoặc nạp file SRT ở mục Kịch bản")
    if script and script.strip():
        words, total = transcribe_words(audio_path, language, FAST_MODEL, progress, beam_size=1)
        cues, ratio = align_script(script, words, total)
        if cues and ratio >= 0.4:
            return cues
        # kịch bản không khớp giọng đọc (kịch bản khác / sửa nhiều) -> nghe đầy đủ cả chữ
        say(None, "Kịch bản không khớp giọng đọc — nghe lại đầy đủ để lấy chữ…")
    words, _ = transcribe_words(audio_path, language, None, progress, beam_size=2)
    return _words_to_cues(words, max_chars, max_seconds)
