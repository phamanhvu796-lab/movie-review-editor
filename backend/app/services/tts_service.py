import hashlib
import json
import logging
import os
import random
import re
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from ..config import DEFAULT_UA, clean_key, key_problem, normalize_base_url, settings, url_problem
from ..schemas import Cue
from ..utils.paths import data_dir, find_binary
from .ffmpeg_service import probe, run_ffmpeg
from .srt_service import merge_short_cues

_SENT = re.compile(r"(?<=[.!?…])\s+|(?<=[。！？])\s*")
STYLE_PRESETS = {   # ElevenLabs voice_settings
    "narrative": {"stability": 0.5, "style": 0.3},
    "dramatic": {"stability": 0.3, "style": 0.6},
    "calm": {"stability": 0.75, "style": 0.1},
}
OPENAI_VOICES = ["alloy", "ash", "ballad", "coral", "echo", "fable", "nova", "onyx", "sage", "shimmer"]
OPENAI_STYLE = {"narrative": "Kể chuyện tự nhiên, ấm, nhịp vừa phải như reviewer phim.",
                "dramatic": "Giọng kịch tính, nhấn nhá cảm xúc, có khoảng lặng.",
                "calm": "Giọng trầm, chậm rãi, bình tĩnh."}


# ---------- chia kịch bản ----------
def _hard_split(s: str, max_chars: int) -> list[str]:
    if len(s) <= max_chars:
        return [s]
    if " " not in s:                       # CJK: cắt theo ký tự
        return [s[i:i + max_chars] for i in range(0, len(s), max_chars)]
    out, cur = [], ""
    for w in s.split(" "):
        if cur and len(cur) + 1 + len(w) > max_chars:
            out.append(cur)
            cur = w
        else:
            cur = f"{cur} {w}".strip()
    return out + [cur] if cur else out


def split_sentences(text: str) -> list[str]:
    return [s.strip() for s in _SENT.split(re.sub(r"\s*\n+\s*", " ", text.replace("\r", ""))) if s and s.strip()]


def split_script(text: str, max_chars: int = 250) -> list[str]:
    """Gom câu thành đoạn <= max_chars (không cắt giữa câu, trừ câu quá dài)."""
    chunks, cur = [], ""
    for sent in split_sentences(text):
        for piece in _hard_split(sent, max_chars):
            if cur and len(cur) + 1 + len(piece) > max_chars:
                chunks.append(cur)
                cur = piece
            else:
                cur = f"{cur} {piece}".strip()
    return chunks + [cur] if cur else chunks


def cues_from_chunks(chunks: list[str], durations: list[float], max_line: int = 84) -> list[Cue]:
    """Phụ đề từ chính kịch bản: chia thời lượng thật của từng đoạn audio theo tỉ lệ số ký tự."""
    cues, t = [], 0.0
    for text, dur in zip(chunks, durations):
        pieces = [p for s in split_sentences(text) for p in _hard_split(s, max_line)]
        total = sum(len(p) for p in pieces) or 1
        acc = 0
        for p in pieces:
            start = t + dur * acc / total
            acc += len(p)
            end = t + dur * acc / total
            cues.append(Cue(index=len(cues) + 1, start=round(start, 3), end=round(end, 3), text=p))
        t += dur
    return merge_short_cues(cues)


# ---------- HTTP ----------
log = logging.getLogger("mre.tts")


class TTSError(RuntimeError):
    def __init__(self, message: str, code: int | None = None):
        super().__init__(message)
        self.code = code


def _explain(code: int, body: str) -> str:
    """Rút thông điệp lỗi gọn từ phản hồi ElevenLabs/OpenAI."""
    status = msg = None
    try:
        j = json.loads(body)
        d = j.get("detail", j.get("error"))
        if isinstance(d, dict):
            status, msg = d.get("status"), d.get("message")
        elif isinstance(d, str):
            msg = d
        elif isinstance(d, list) and d:
            msg = str(d[0].get("msg", d[0]))
    except (ValueError, AttributeError):
        pass
    if status == "quota_exceeded":
        return "Hết quota ký tự của gói ElevenLabs. Nâng gói hoặc đợi chu kỳ mới."
    if code == 401:
        return f"API key không hợp lệ hoặc thiếu quyền: {msg or ''}".strip()
    if code == 429:
        return "Vượt giới hạn tốc độ/số request đồng thời (đã tự thử lại nhiều lần). Giảm tts_concurrency hoặc thử lại sau."
    return f"TTS HTTP {code}: {msg or body[:200]}"


def _request(url: str, headers: dict, payload: dict | None = None, method: str = "POST") -> bytes:
    """Retry 429/5xx với backoff mũ + jitter, tôn trọng Retry-After."""
    data = json.dumps(payload).encode() if payload is not None else None
    headers = {"User-Agent": DEFAULT_UA, **headers}
    for attempt in range(5):
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")[:500]
            if e.code in (429, 500, 502, 503, 504) and attempt < 4:
                ra = (e.headers.get("Retry-After") if e.headers else None) or ""
                try:
                    delay = float(ra)
                except ValueError:
                    delay = min(2 ** attempt * 2, 30)
                time.sleep(min(delay, 60) + random.uniform(0, 0.5))
                continue
            raise TTSError(_explain(e.code, body), e.code)
        except urllib.error.URLError as e:
            if attempt < 3:
                time.sleep(2 ** attempt)
                continue
            raise TTSError(f"Không kết nối được dịch vụ TTS: {e.reason}")
    raise TTSError("TTS thất bại")


def _clamp(x, lo, hi):
    return max(lo, min(hi, x))


# ---------- nhiều nguồn (Base URL + key) cho từng dịch vụ ----------
_FAILOVER = {None, 401, 402, 403, 429, 500, 502, 503, 504}     # lỗi nên thử nguồn khác (hết quota, key hỏng, quá tải, mất mạng)


def _code(e: Exception) -> int | None:
    c = getattr(e, "code", None)
    if isinstance(c, int):
        return c
    m = re.search(r"HTTP (\d{3})", str(e))
    return int(m.group(1)) if m else None


def _service(provider: str) -> str:
    return "elevenlabs" if provider == "elevenlabs" else "openai_tts"


def _with_sources(provider: str, fn):
    """Chạy fn(nguồn) lần lượt: nguồn đang chọn trước, rồi các nguồn bật còn lại. 400/404/422 là lỗi yêu cầu nên không thử nguồn khác."""
    srcs = settings.usable_connections(_service(provider))
    if not srcs:
        raise RuntimeError("Chưa nhập ElevenLabs API key" if provider == "elevenlabs" else "Chưa nhập OpenAI API key")
    for i, s in enumerate(srcs):
        try:
            return fn(s)
        except RuntimeError as e:
            if i == len(srcs) - 1 or _code(e) not in _FAILOVER:
                raise
            log.warning("Nguồn TTS '%s' lỗi (%s), chuyển sang nguồn kế tiếp", s.get("name"), str(e)[:150])


def _h(src: dict, headers: dict) -> dict:
    """Thêm User-Agent (mặc định giống trình duyệt, hoặc theo cấu hình nguồn) để không bị Cloudflare chặn."""
    return {"User-Agent": src.get("user_agent") or DEFAULT_UA, **headers}


def _openai_model(src: dict | None = None) -> str:
    """Model giọng đọc OpenAI-compatible: theo cấu hình của nguồn (relay thường đặt tên khác), mặc định gpt-4o-mini-tts."""
    src = src or settings.active_connection("openai_tts") or {}
    return (src.get("model") or "").strip() or settings.openai_tts_model


def _sniff(data: bytes) -> str:
    h = data[:4]
    if h[:3] == b"ID3" or h[:2] in (b"\xff\xfb", b"\xff\xf3", b"\xff\xf2", b"\xff\xe3"):
        return "mp3"
    return {b"RIFF": "wav", b"OggS": "ogg", b"fLaC": "flac"}.get(h, "không phải âm thanh")


def _ensure_audio(data: bytes) -> bytes:
    """Một số relay trả HTTP 200 kèm JSON/HTML báo lỗi. Chặn sớm để lỗi hiện đúng nội dung thay vì 'Invalid data' của FFmpeg."""
    if not data or data.lstrip()[:1] in (b"{", b"<"):
        raise RuntimeError("Dịch vụ không trả về âm thanh mà trả về: " + data[:200].decode("utf-8", "replace"))
    return data


def _eleven_base(src: dict) -> str:
    return normalize_base_url(src.get("base_url", ""), "elevenlabs")


def _openai_base(src: dict) -> str:
    return normalize_base_url(src.get("base_url", ""), "openai")


ELEVEN_MODELS = {"high": "eleven_multilingual_v2", "fast": "eleven_flash_v2_5"}


def _cache_path(text: str, provider: str, voice_id: str, style: str, speed: float, quality: str) -> Path:
    model = ELEVEN_MODELS.get(quality, ELEVEN_MODELS["high"]) if provider == "elevenlabs" else _openai_model()
    h = hashlib.sha1(json.dumps([provider, voice_id, style, round(speed, 2), model, text],
                                ensure_ascii=False).encode()).hexdigest()[:32]
    return data_dir() / "cache" / "tts" / f"{h}.mp3"


def synthesize(text: str, provider: str, voice_id: str, style: str = "narrative", speed: float = 1.0,
               quality: str = "high", prev_text: str | None = None, next_text: str | None = None) -> bytes:
    """Trả về bytes MP3 cho một đoạn văn bản. prev/next_text giúp ElevenLabs giữ ngữ điệu liền mạch giữa các đoạn.
    Có nhiều nguồn thì tự chuyển nguồn khi lỗi (vd hết quota ở key này thì dùng key khác)."""
    if provider not in ("elevenlabs", "openai"):
        raise RuntimeError(f"Provider không hỗ trợ: {provider}")

    def one(src: dict) -> bytes:
        key = src["api_key"]
        if provider == "elevenlabs":
            vs = {**STYLE_PRESETS.get(style, STYLE_PRESETS["narrative"]), "similarity_boost": 0.75,
                  "use_speaker_boost": True, "speed": _clamp(speed, 0.7, 1.2)}
            payload = {"text": text, "model_id": ELEVEN_MODELS.get(quality, ELEVEN_MODELS["high"]), "voice_settings": vs}
            url = f"{_eleven_base(src)}/v1/text-to-speech/{voice_id}?output_format=mp3_44100_128"
            headers = _h(src, {"xi-api-key": key, "Content-Type": "application/json", "Accept": "audio/mpeg"})
            with_ctx = dict(payload)
            if prev_text:
                with_ctx["previous_text"] = prev_text
            if next_text:
                with_ctx["next_text"] = next_text
            try:
                return _ensure_audio(_request(url, headers, with_ctx))
            except RuntimeError as e:      # model không hỗ trợ ngữ cảnh -> thử lại không có
                if with_ctx != payload and ("HTTP 400" in str(e) or "HTTP 422" in str(e)):
                    return _ensure_audio(_request(url, headers, payload))
                raise
        # OpenAI-compatible: model riêng của từng nguồn; relay không có model đó thì thử model phổ biến khác
        last = None
        for model in dict.fromkeys(m for m in (_openai_model(src), "tts-1", "gpt-4o-mini-tts") if m):
            payload = {"model": model, "voice": voice_id or "alloy", "input": text, "response_format": "mp3",
                       "speed": _clamp(speed, 0.25, 4.0)}
            if model.startswith("gpt-4o"):
                payload["instructions"] = OPENAI_STYLE.get(style, OPENAI_STYLE["narrative"])
            try:
                return _ensure_audio(_request(f"{_openai_base(src)}/audio/speech",
                                              _h(src, {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}), payload))
            except RuntimeError as e:
                last = e
                if _code(e) in (400, 404) and "model" in str(e).lower():
                    log.warning("Nguồn '%s' không có model '%s', thử model khác", src.get("name"), model)
                    continue
                raise
        raise last

    return _with_sources(provider, one)


def synthesize_cached(text: str, provider: str, voice_id: str, style: str = "narrative", speed: float = 1.0,
                      quality: str = "high", prev_text: str | None = None, next_text: str | None = None) -> bytes:
    """Đoạn đã tạo rồi (cùng chữ + giọng + cài đặt) lấy từ ổ đĩa: tạo lại voice sau khi sửa vài câu không tốn thêm credit.
    prev/next_text và nguồn nào tạo ra không nằm trong khoá cache."""
    p = _cache_path(text, provider, voice_id, style, speed, quality)
    if p.exists():
        p.touch()
        return p.read_bytes()
    data = synthesize(text, provider, voice_id, style, speed, quality, prev_text, next_text)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(f".{threading.get_ident()}.part")
    tmp.write_bytes(data)
    os.replace(tmp, p)
    return data


_voices_cache: dict = {}


def list_voices(provider: str) -> list[dict]:
    if provider == "openai":
        return [{"id": v, "name": v, "labels": ""} for v in OPENAI_VOICES]

    def one(src: dict) -> list[dict]:
        key, base = src["api_key"], _eleven_base(src)
        ck = key[-8:] + base
        hit = _voices_cache.get(ck)
        if hit and time.time() - hit[0] < 600:          # danh sách giọng ít đổi: nhớ 10 phút
            return hit[1]
        data = json.loads(_request(f"{base}/v1/voices", _h(src, {"xi-api-key": key}), method="GET"))
        out = [{"id": v["voice_id"], "name": v["name"],
                "labels": ", ".join(str(x) for x in (v.get("labels") or {}).values())} for v in data.get("voices", [])]
        _voices_cache[ck] = (time.time(), out)
        return out
    return _with_sources("elevenlabs", one)


def _quota_of(src: dict) -> dict:
    d = json.loads(_request(f"{_eleven_base(src)}/v1/user/subscription", _h(src, {"xi-api-key": src["api_key"]}), method="GET"))
    limit, used = int(d.get("character_limit") or 0), int(d.get("character_count") or 0)
    return {"name": src.get("name", ""), "used": used, "limit": limit, "remaining": max(limit - used, 0)}


def get_quota() -> dict:
    """Quota ElevenLabs CỘNG DỒN qua mọi nguồn bật (kèm chi tiết từng nguồn)."""
    srcs = settings.usable_connections("elevenlabs")
    if not srcs:
        raise RuntimeError("Chưa nhập ElevenLabs API key")
    per, first_err = [], None
    for s in srcs:
        try:
            per.append(_quota_of(s))
        except RuntimeError as e:
            first_err = first_err or e
    if not per:
        raise first_err
    used, limit = sum(p["used"] for p in per), sum(p["limit"] for p in per)
    return {"used": used, "limit": limit, "remaining": max(limit - used, 0), "sources": per}


def check_openai():
    srcs = settings.usable_connections("openai_tts")
    if not srcs:
        raise RuntimeError("Chưa nhập OpenAI API key")
    last = None
    for s in srcs:
        try:
            _request(f"{_openai_base(s)}/models", _h(s, {"Authorization": f"Bearer {s['api_key']}"}), method="GET")
            return
        except RuntimeError as e:
            last = e
    raise last


def test_tts_source(conn: dict) -> dict:
    """Kiểm tra riêng MỘT nguồn giọng đọc."""
    conn = {**conn, "api_key": clean_key(conn.get("api_key"))}
    bad = key_problem(conn["api_key"]) or url_problem(conn.get("base_url"))
    if bad:
        return {"ok": False, "detail": bad}
    try:
        if conn.get("service") == "openai_tts":
            raw = _request(f"{_openai_base(conn)}/models", _h(conn, {"Authorization": f"Bearer {conn['api_key']}"}), method="GET")
            try:
                ids = [m["id"] for m in json.loads(raw).get("data", []) if isinstance(m, dict) and "id" in m]
            except ValueError:
                ids = []
            tts = [m for m in ids if "tts" in m.lower()]
            want = (conn.get("model") or "").strip()
            note = f"; model giọng đọc khả dụng: {', '.join(tts[:8])}" if tts else "; (nguồn không liệt kê model giọng đọc)"
            if want and ids and want not in ids:
                note = f"; ⚠ model '{want}' không có trong danh sách của nguồn" + note
            return {"ok": True, "detail": "Kết nối OK" + note}
        q = _quota_of(conn)
        return {"ok": True, "detail": f"Kết nối OK, còn {q['remaining']:,} / {q['limit']:,} ký tự"}
    except RuntimeError as e:
        if conn.get("service") == "elevenlabs" and "permission" in str(e).lower():
            return {"ok": True, "detail": "Key hợp lệ nhưng không đọc được quota (thiếu quyền user_read)"}
        return {"ok": False, "detail": str(e)}


def diagnose(provider: str, voice_id: str = "", style: str = "narrative", quality: str = "high") -> dict:
    """Thử THẬT một câu ngắn (tốn ~35 ký tự) rồi kiểm tra từng bước: nguồn, FFmpeg, âm thanh nhận về, giải mã."""
    trace: list[str] = []

    def fail(msg: str) -> dict:
        return {"ok": False, "detail": msg, "trace": trace}

    srcs = settings.usable_connections(_service(provider))
    if not srcs:
        return fail("Chưa có nguồn nào cho dịch vụ này (Cài đặt → thêm Base URL + API key).")
    trace.append("Nguồn sẽ dùng (theo thứ tự): " + ", ".join(f"{x.get('name')} [{x.get('base_url') or 'mặc định'}]" for x in srcs))
    if provider == "elevenlabs" and not voice_id:
        return fail("Chưa chọn giọng: bấm 'Tải danh sách giọng' rồi chọn một giọng.")
    ff, fp = find_binary("ffmpeg"), find_binary("ffprobe")
    trace.append(f"FFmpeg: {'OK' if ff else 'THIẾU'} · FFprobe: {'OK' if fp else 'THIẾU'}")
    t0 = time.time()
    try:
        data = synthesize("Xin chào, đây là bản thử giọng đọc.", provider, voice_id, style, 1.0, quality)
    except RuntimeError as e:
        return fail(f"Tạo giọng thất bại: {e}")
    kind = _sniff(data)
    trace.append(f"Nhận {len(data):,} byte ({kind}) sau {time.time() - t0:.1f}s")
    if kind == "không phải âm thanh":
        return fail("Dịch vụ trả dữ liệu không phải âm thanh: " + data[:200].decode("utf-8", "replace"))
    if not (ff and fp):
        return fail("Dịch vụ tạo giọng được nhưng máy thiếu FFmpeg/FFprobe để xử lý âm thanh. Bấm 'Tải FFmpeg tự động'.")
    import tempfile
    with tempfile.TemporaryDirectory(prefix="mre_vt_") as d:
        src, wav = Path(d) / f"t.{kind}", Path(d) / "t.wav"
        src.write_bytes(data)
        try:
            run_ffmpeg(["-i", src, "-ar", "44100", "-ac", "1", "-c:a", "pcm_s16le", wav])
            dur = probe(wav)["duration"]
        except RuntimeError as e:
            return fail(f"FFmpeg không giải mã được âm thanh nhận về: {e}")
    trace.append(f"Giải mã bằng FFmpeg OK: {dur:.1f}s âm thanh")
    return {"ok": True, "detail": f"Giọng đọc hoạt động ({dur:.1f}s âm thanh thử)", "trace": trace}


# ---------- dựng file voice hoàn chỉnh ----------
def build_voice(text: str, out_wav: Path, workdir: Path, provider: str, voice_id: str, style: str,
                speed: float, quality: str = "high", progress_cb=None) -> tuple[list[str], list[float]]:
    """Tạo audio từng đoạn (song song, có cache, kiểm tra quota trước) -> WAV mono 44.1k (độ dài chính xác)
    -> nối thành out_wav. Trả về (các đoạn, thời lượng từng đoạn) để sinh phụ đề khớp tuyệt đối."""
    chunks = split_script(text)
    if not chunks:
        raise RuntimeError("Kịch bản trống")
    workdir.mkdir(parents=True, exist_ok=True)

    if provider == "elevenlabs":        # báo thiếu quota TRƯỚC khi tốn tiền (chỉ tính đoạn chưa có trong cache)
        need = sum(len(c) for c in chunks if not _cache_path(c, provider, voice_id, style, speed, quality).exists())
        need = int(need * (0.5 if quality == "fast" else 1))
        try:
            q = get_quota()
        except RuntimeError:
            q = None                    # key không có quyền đọc quota: vẫn cho tạo
        if q and q["limit"] and need > q["remaining"]:
            raise RuntimeError(f"Không đủ quota ElevenLabs: cần ~{need:,} ký tự, còn {q['remaining']:,}.")

    def work(i: int) -> tuple[int, float]:
        prev = chunks[i - 1][-200:] if i > 0 else None
        nxt = chunks[i + 1][:200] if i + 1 < len(chunks) else None
        mp3, wav = workdir / f"c{i:04d}.mp3", workdir / f"c{i:04d}.wav"
        mp3.write_bytes(synthesize_cached(chunks[i], provider, voice_id, style, speed, quality, prev, nxt))
        run_ffmpeg(["-i", mp3, "-ar", "44100", "-ac", "1", "-c:a", "pcm_s16le", wav])
        return i, probe(wav)["duration"]

    conc = max(1, min(settings.tts_concurrency if provider == "elevenlabs" else 4, len(chunks)))
    durations = [0.0] * len(chunks)
    with ThreadPoolExecutor(max_workers=conc) as ex:
        futs = {ex.submit(work, i): i for i in range(len(chunks))}
        done = 0
        try:
            for f in as_completed(futs):
                i, d = f.result()
                durations[i] = d
                done += 1
                if progress_cb:
                    progress_cb(done / len(chunks))
        except BaseException:
            for f in futs:
                f.cancel()
            raise
    (workdir / "list.txt").write_text("".join(f"file 'c{i:04d}.wav'\n" for i in range(len(chunks))), "utf-8")
    out_wav.parent.mkdir(parents=True, exist_ok=True)
    run_ffmpeg(["-f", "concat", "-safe", "0", "-i", "list.txt", "-c", "copy", str(out_wav)], cwd=workdir)
    return chunks, durations
