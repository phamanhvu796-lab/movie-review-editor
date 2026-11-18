"""Phụ đề từ giọng đọc qua API (nhanh, chính xác) — thay cho Whisper chạy trên máy.

API Claude KHÔNG nhận được âm thanh, nên bước "nghe giọng đọc" phải dùng dịch vụ nhận giọng nói:
  - ElevenLabs Scribe v2  (dùng chung API key ElevenLabs đã gắn ở mục Giọng AI)
  - OpenAI whisper-1      (dùng chung API key OpenAI TTS)
Cả hai trả từng từ kèm mốc thời gian. Âm thanh được nén về mono 16 kHz trước khi gửi (file 5 phút ~1-2 MB)
để tải lên nhanh và luôn dưới giới hạn 25 MB của OpenAI.
Chọn nguồn ở Cài đặt (stt_provider): auto (API nếu có, không thì máy) | elevenlabs | openai | local.
"""
import json
import logging
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from types import SimpleNamespace as NS

from ..config import DEFAULT_UA, settings
from ..utils.paths import find_binary
from .ffmpeg_service import _FLAGS, probe
from .tts_service import _eleven_base, _h, _openai_base

log = logging.getLogger("mre.stt")

ELEVEN_MODEL = "scribe_v2"
OPENAI_MODEL = "whisper-1"
MAX_UPLOAD = 24 * 1024 * 1024
LABELS = {"elevenlabs": "ElevenLabs Scribe", "openai": "OpenAI Whisper API", "local": "Whisper trên máy"}


class STTError(RuntimeError):
    def __init__(self, msg: str, code: int | None = None):
        super().__init__(msg)
        self.code = code


def mode() -> str:
    m = (settings._load().get("stt_provider") or "auto").lower()      # noqa: SLF001
    return m if m in ("auto", "elevenlabs", "openai", "local") else "auto"


def providers() -> list[tuple[str, dict]]:
    """Các nguồn API dùng được theo thứ tự ưu tiên (rỗng = dùng Whisper trên máy)."""
    m = mode()
    if m == "local":
        return []
    out = []
    if m in ("auto", "elevenlabs"):
        out += [("elevenlabs", c) for c in settings.usable_connections("elevenlabs")]
    if m in ("auto", "openai"):
        out += [("openai", c) for c in settings.usable_connections("openai_tts")]
    return out


def _compress(src: str, dest: Path) -> Path:
    ff = find_binary("ffmpeg")
    if not ff:
        raise STTError("Thiếu FFmpeg để chuẩn bị file giọng đọc")
    for kbps in ("48k", "24k"):
        r = subprocess.run([ff, "-y", "-nostdin", "-hide_banner", "-loglevel", "error", "-i", str(src), "-vn", "-ac", "1",
                            "-ar", "16000", "-b:a", kbps, str(dest)], capture_output=True, creationflags=_FLAGS)
        if r.returncode != 0:
            raise STTError("Không đọc được file giọng đọc: " + r.stderr.decode("utf-8", "replace")[-300:])
        if dest.stat().st_size <= MAX_UPLOAD:
            return dest
    raise STTError("File giọng đọc quá dài để gửi một lần (trên ~2 giờ)")


def _multipart(fields: dict, file_path: Path) -> tuple[bytes, str]:
    b = "----mre" + uuid.uuid4().hex
    parts = []
    for k, v in fields.items():
        for one in (v if isinstance(v, list) else [v]):
            parts.append(f'--{b}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{one}\r\n'.encode())
    parts.append(f'--{b}\r\nContent-Disposition: form-data; name="file"; filename="voice.mp3"\r\n'
                 f"Content-Type: audio/mpeg\r\n\r\n".encode() + file_path.read_bytes() + b"\r\n")
    parts.append(f"--{b}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={b}"


def _post(url: str, headers: dict, fields: dict, file_path: Path, timeout: float = 600) -> dict:
    body, ctype = _multipart(fields, file_path)
    h = {"User-Agent": DEFAULT_UA, "Content-Type": ctype, **headers}
    for attempt in range(4):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, data=body, headers=h, method="POST"), timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as e:
            msg = e.read().decode("utf-8", "replace")[:400]
            if e.code in (429, 500, 502, 503, 504) and attempt < 3:
                time.sleep(2 * (attempt + 1))
                continue
            if e.code == 401:
                raise STTError("API key không hợp lệ hoặc thiếu quyền nhận giọng nói (Speech to Text)", 401)
            raise STTError(f"HTTP {e.code}: {msg}", e.code)
        except urllib.error.URLError as e:
            if attempt < 2:
                time.sleep(2)
                continue
            raise STTError(f"Không kết nối được: {e.reason}")
    raise STTError("Dịch vụ nhận giọng nói đang quá tải, thử lại sau")


def _eleven(conn: dict, audio: Path, language: str | None) -> list:
    url = f"{_eleven_base(conn)}/v1/speech-to-text"
    fields = {"model_id": ELEVEN_MODEL, "timestamps_granularity": "word", "tag_audio_events": "false"}
    hdr = _h(conn, {"xi-api-key": conn["api_key"]})
    try:
        d = _post(url, hdr, {**fields, **({"language_code": language} if language else {})}, audio)
    except STTError as e:
        if language and e.code in (400, 422):           # mã ngôn ngữ không được nhận -> để tự nhận diện
            d = _post(url, hdr, fields, audio)
        else:
            raise
    return [NS(word=" " + w["text"].strip(), start=float(w["start"]), end=float(w["end"]))
            for w in d.get("words") or [] if w.get("type", "word") == "word" and w.get("start") is not None
            and (w.get("text") or "").strip()]


def _openai(conn: dict, audio: Path, language: str | None) -> list:
    d = _post(f"{_openai_base(conn)}/audio/transcriptions", _h(conn, {"Authorization": f"Bearer {conn['api_key']}"}),
              {"model": OPENAI_MODEL, "response_format": "verbose_json", "timestamp_granularities[]": ["word", "segment"],
               **({"language": language} if language else {})}, audio)
    words = d.get("words") or []
    if words:
        return [NS(word=" " + w["word"].strip(), start=float(w["start"]), end=float(w["end"]))
                for w in words if (w.get("word") or "").strip()]
    # dịch vụ trung gian không trả từng từ -> rải đều theo đoạn
    out = []
    for s in d.get("segments") or []:
        toks = (s.get("text") or "").split()
        if not toks:
            continue
        step = (float(s["end"]) - float(s["start"])) / len(toks)
        out += [NS(word=" " + t, start=float(s["start"]) + i * step, end=float(s["start"]) + (i + 1) * step)
                for i, t in enumerate(toks)]
    return out


def transcribe_words(audio_path: str, language: str | None = None, progress=None):
    """Thử lần lượt các nguồn API. Trả (từ, tổng giây, tên nguồn) hoặc None nếu không có nguồn API nào.
    Mọi nguồn đều lỗi -> ném STTError (bên gọi có thể chuyển sang Whisper trên máy)."""
    provs = providers()
    if not provs:
        return None
    say = progress or (lambda f, t: None)
    total = float(probe(audio_path).get("duration") or 0)
    errors = []
    with tempfile.TemporaryDirectory(prefix="mre_stt_") as tmp:
        small = _compress(audio_path, Path(tmp) / "voice.mp3")
        for kind, conn in provs:
            label = LABELS[kind]
            say(None, f"Đang gửi giọng đọc lên {label} để tạo phụ đề…")
            t0 = time.time()
            try:
                words = (_eleven if kind == "elevenlabs" else _openai)(conn, small, (language or "").split("-")[0] or None)
            except STTError as e:
                log.warning("%s lỗi: %s", label, e)
                errors.append(f"{label}: {e}")
                continue
            if words:
                say(1.0, f"{label} nghe xong {total / 60:.1f} phút giọng đọc trong {time.time() - t0:.0f} giây")
                return words, total, kind
            errors.append(f"{label}: không nghe ra lời nào")
    raise STTError("; ".join(errors)[:400])
