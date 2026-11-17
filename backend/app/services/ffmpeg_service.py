import json
import os
import subprocess
import threading
from collections import deque
from pathlib import Path

from ..utils.paths import find_binary

class RenderCancelled(Exception):
    pass


_FLAGS = 0x08000000 if os.name == "nt" else 0   # CREATE_NO_WINDOW


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def probe(path: str | Path) -> dict:
    exe = find_binary("ffprobe")
    if not exe:
        raise RuntimeError("Không tìm thấy ffprobe. Đặt ffmpeg.exe/ffprobe.exe vào thư mục bin/ hoặc cài vào PATH.")
    p = subprocess.run([exe, "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)],
                       capture_output=True, text=True, encoding="utf-8", errors="replace", creationflags=_FLAGS)
    if p.returncode:
        raise RuntimeError(p.stderr.strip()[-500:] or "ffprobe lỗi")
    d = json.loads(p.stdout)
    v = next((s for s in d.get("streams", []) if s.get("codec_type") == "video"), None)
    return {
        "duration": _f(d.get("format", {}).get("duration")),
        "width": v.get("width") if v else None,
        "height": v.get("height") if v else None,
        "has_video": v is not None,
        "has_audio": any(s.get("codec_type") == "audio" for s in d.get("streams", [])),
    }


def run_ffmpeg(args: list[str], cwd: str | Path | None = None, duration: float | None = None, progress_cb=None,
               cancel=None):
    """Chạy ffmpeg; progress_cb(0..1) nếu biết duration của output. Ném RuntimeError kèm log khi lỗi."""
    exe = find_binary("ffmpeg")
    if not exe:
        raise RuntimeError("Không tìm thấy ffmpeg. Đặt ffmpeg.exe vào thư mục bin/ hoặc cài vào PATH.")
    cmd = [exe, "-y", "-hide_banner", "-loglevel", "error", "-progress", "pipe:1", "-nostats", *map(str, args)]
    from . import sysinfo
    # Ưu tiên thấp (Windows): dựng video không giành CPU của chuột/bàn phím/app khác -> máy không bị đơ
    proc = subprocess.Popen(cmd, cwd=str(cwd) if cwd else None, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, encoding="utf-8", errors="replace", creationflags=_FLAGS | sysinfo.priority_flags())
    tail: deque[str] = deque(maxlen=25)
    for line in proc.stdout:
        if cancel and cancel():
            proc.kill()
            proc.wait()
            raise RenderCancelled()
        line = line.strip()
        if line.startswith(("out_time_us=", "out_time_ms=")):
            if progress_cb and duration:
                try:
                    progress_cb(min(max(int(line.split("=")[1]) / 1e6 / duration, 0.0), 1.0))
                except ValueError:
                    pass
        elif "=" not in line or line.startswith("Error"):
            tail.append(line)
    if proc.wait() != 0:
        raise RuntimeError("ffmpeg lỗi:\n" + "\n".join(tail))


def extract_frame(path: str | Path, t: float, out: str | Path, width: int = 384):
    run_ffmpeg(["-ss", f"{max(t, 0):.3f}", "-i", path, "-frames:v", "1", "-vf", f"scale={width}:-2", "-q:v", "4", out])


# ---------- Tăng tốc phần cứng ----------
_hw_cache: dict = {}
_hw_lock = threading.Lock()


def _run_quiet(cmd: list[str]) -> tuple[int, str]:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=30, creationflags=_FLAGS)
        return p.returncode, p.stdout
    except (OSError, subprocess.SubprocessError):
        return 1, ""


def _cpu_threads() -> list[str]:
    from . import sysinfo
    n = sysinfo.cpu_threads()
    return ["-threads", str(n)] if n else []


def segment_codec_args(encoder: str | None, crf: int = 16) -> list[str]:
    """Mã hoá đoạn trung gian: chất lượng cao, tốc độ ưu tiên (bản cuối còn được mã hoá lại một lần)."""
    if encoder == "h264_nvenc":
        return ["-c:v", "h264_nvenc", "-preset", "p4", "-rc", "vbr", "-cq", str(crf + 3), "-b:v", "0"]
    if encoder == "h264_qsv":
        return ["-c:v", "h264_qsv", "-preset", "veryfast", "-global_quality", str(crf + 4)]
    if encoder == "h264_amf":
        return ["-c:v", "h264_amf", "-quality", "speed", "-rc", "cqp", "-qp_i", str(crf + 4), "-qp_p", str(crf + 4)]
    return ["-c:v", "libx264", "-preset", "veryfast", "-crf", str(crf), *_cpu_threads()]


def final_codec_args(encoder: str | None, preview: bool, kbps: int) -> list[str]:
    rate = ["-b:v", f"{kbps}k", "-maxrate", f"{kbps}k", "-bufsize", f"{kbps * 2}k"]
    if encoder == "h264_nvenc":
        return ["-c:v", "h264_nvenc", "-preset", "p4" if preview else "p5", "-rc", "vbr", *rate]
    if encoder == "h264_qsv":
        return ["-c:v", "h264_qsv", "-preset", "veryfast" if preview else "medium", *rate]
    if encoder == "h264_amf":
        return ["-c:v", "h264_amf", "-quality", "speed" if preview else "balanced", "-rc", "vbr_peak", *rate]
    return ["-c:v", "libx264", "-preset", "veryfast" if preview else "medium", *rate, *_cpu_threads()]


def detect_hw_encoder() -> str | None:
    """h264_nvenc | h264_qsv | h264_amf, chỉ khi ffmpeg liệt kê VÀ mã hoá thử thành công (có GPU/driver thật)."""
    with _hw_lock:
        if "enc" in _hw_cache:
            return _hw_cache["enc"]
        found = None
        exe = find_binary("ffmpeg")
        if exe:
            _, listing = _run_quiet([exe, "-hide_banner", "-encoders"])
            for name in ("h264_nvenc", "h264_qsv", "h264_amf"):
                if name in listing:
                    rc, _ = _run_quiet([exe, "-v", "error", "-f", "lavfi", "-i", "color=c=black:s=256x256:d=0.2:r=30",
                                        *segment_codec_args(name), "-pix_fmt", "yuv420p", "-f", "null", "-"])
                    if rc == 0:
                        found = name
                        break
        _hw_cache["enc"] = found
        return found
