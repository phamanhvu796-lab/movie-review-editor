"""Quét cấu hình máy + theo dõi tải (CPU, RAM, GPU, ổ đĩa) cho thanh trạng thái, và chọn cách dựng tối ưu.

Chế độ hiệu năng (Cài đặt -> perf_mode):
  gpu  (mặc định): mã hoá bằng GPU (NVENC / Quick Sync / AMF) nếu có; ffmpeg chạy ưu tiên thấp + giới hạn luồng CPU
        -> máy vẫn mượt khi đang xuất video.
  max : dùng hết sức máy (nhanh nhất, máy có thể giật khi đang dựng).
  eco : nhẹ máy nhất (1 tiến trình, ít luồng) — cho laptop yếu hoặc vừa dựng vừa làm việc khác.
"""
import os
import platform
import shutil
import subprocess
import threading
import time

from ..config import settings

_FLAGS = 0x08000000 if os.name == "nt" else 0
BELOW_NORMAL = 0x00004000
_scan_cache: dict = {}
_lock = threading.Lock()
_live: dict = {"t": 0.0}
_prev_io: dict = {}

try:
    import psutil
except Exception:          # bản chạy mã nguồn chưa cài psutil: vẫn chạy, chỉ thiếu số liệu
    psutil = None


def _run(cmd: list[str], timeout: float = 8) -> str:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=timeout, creationflags=_FLAGS)
        return p.stdout.strip() if p.returncode == 0 else ""
    except Exception:
        return ""


def _cpu_name() -> str:
    if os.name == "nt":
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DESCRIPTION\System\CentralProcessor\0") as k:
                return " ".join(winreg.QueryValueEx(k, "ProcessorNameString")[0].split())
        except OSError:
            pass
    try:
        for line in open("/proc/cpuinfo", encoding="utf-8"):
            if line.startswith("model name"):
                return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return platform.processor() or "CPU"


def _nvidia() -> list[dict]:
    exe = shutil.which("nvidia-smi") or (r"C:\Windows\System32\nvidia-smi.exe" if os.name == "nt" and
                                          os.path.exists(r"C:\Windows\System32\nvidia-smi.exe") else None)
    if not exe:
        return []
    out = _run([exe, "--query-gpu=name,memory.total,driver_version", "--format=csv,noheader,nounits"])
    gpus = []
    for line in out.splitlines():
        parts = [x.strip() for x in line.split(",")]
        if len(parts) >= 3:
            gpus.append({"name": parts[0], "vram_gb": round(float(parts[1] or 0) / 1024, 1), "driver": parts[2],
                         "vendor": "nvidia"})
    return gpus


def _windows_gpus() -> list[dict]:
    out = _run(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                "Get-CimInstance Win32_VideoController | ForEach-Object { \"$($_.Name)|$($_.AdapterRAM)\" }"], 20)
    gpus = []
    for line in out.splitlines():
        name, _, ram = line.partition("|")
        name = name.strip()
        if not name or "basic display" in name.lower() or "remote" in name.lower():
            continue
        vendor = "nvidia" if "nvidia" in name.lower() else "amd" if ("amd" in name.lower() or "radeon" in name.lower()) \
            else "intel" if "intel" in name.lower() else "other"
        try:
            vram = round(int(ram) / 1024 ** 3, 1) if ram.strip() else None   # AdapterRAM tối đa 4 GB (giới hạn của Windows)
        except ValueError:
            vram = None
        gpus.append({"name": name, "vram_gb": vram, "vendor": vendor})
    return gpus


def scan(force: bool = False) -> dict:
    """Cấu hình máy (quét 1 lần, nhớ lại)."""
    with _lock:
        if _scan_cache and not force:
            return dict(_scan_cache)
    gpus = _nvidia()
    if os.name == "nt":
        others = [g for g in _windows_gpus() if not (gpus and g["vendor"] == "nvidia")]
        gpus += others
    ram = round(psutil.virtual_memory().total / 1024 ** 3, 1) if psutil else None
    cores = os.cpu_count() or 2
    phys = psutil.cpu_count(logical=False) if psutil else None
    try:
        from .ffmpeg_service import detect_hw_encoder
        enc = detect_hw_encoder()
    except Exception:
        enc = None
    info = {"cpu": _cpu_name(), "threads": cores, "cores": phys or cores, "ram_gb": ram, "gpus": gpus,
            "encoder": enc, "encoder_label": {"h264_nvenc": "NVIDIA NVENC", "h264_qsv": "Intel Quick Sync",
                                               "h264_amf": "AMD AMF"}.get(enc, "CPU (x264)"),
            "os": f"{platform.system()} {platform.release()}"}
    with _lock:
        _scan_cache.clear()
        _scan_cache.update(info)
    return info


def _nvidia_live() -> dict | None:
    exe = shutil.which("nvidia-smi") or (r"C:\Windows\System32\nvidia-smi.exe" if os.name == "nt" else None)
    if not exe or not os.path.exists(exe):
        return None
    out = _run([exe, "--query-gpu=utilization.gpu,memory.used,memory.total,temperature.gpu,encoder.stats.sessionCount",
                "--format=csv,noheader,nounits"], 5)
    line = out.splitlines()[0] if out else ""
    p = [x.strip() for x in line.split(",")]
    if len(p) < 5:
        return None

    def num(x):
        try:
            return float(x)
        except ValueError:
            return None
    return {"util": num(p[0]), "mem_used_gb": round((num(p[1]) or 0) / 1024, 1), "mem_total_gb": round((num(p[2]) or 0) / 1024, 1),
            "temp": num(p[3]), "nvenc_sessions": int(num(p[4]) or 0)}


def live(paths: list[str] | None = None) -> dict:
    """Số liệu tải hiện tại (gọi mỗi 2-3 giây). Nhớ 1.5 giây để nhiều cửa sổ gọi không tốn thêm."""
    now = time.time()
    with _lock:
        if now - _live["t"] < 1.5:
            return dict(_live["data"])
    d: dict = {}
    if psutil:
        d["cpu"] = psutil.cpu_percent(interval=None)
        vm = psutil.virtual_memory()
        d["ram_used_gb"], d["ram_total_gb"], d["ram_pct"] = round(vm.used / 1024 ** 3, 1), round(vm.total / 1024 ** 3, 1), vm.percent
        try:
            io = psutil.disk_io_counters()
            if _prev_io:
                dt = max(now - _prev_io["t"], 0.1)
                d["disk_read_mb"] = round((io.read_bytes - _prev_io["r"]) / dt / 1024 ** 2, 1)
                d["disk_write_mb"] = round((io.write_bytes - _prev_io["w"]) / dt / 1024 ** 2, 1)
            _prev_io.update(t=now, r=io.read_bytes, w=io.write_bytes)
        except Exception:
            pass
    d["gpu"] = _nvidia_live()
    disks = []
    seen = set()
    for p in paths or []:
        try:
            drive = os.path.splitdrive(os.path.abspath(p))[0] or "/"
            if drive in seen:
                continue
            seen.add(drive)
            u = shutil.disk_usage(p if os.path.exists(p) else os.path.dirname(os.path.abspath(p)) or "/")
            disks.append({"drive": drive, "free_gb": round(u.free / 1024 ** 3, 1), "total_gb": round(u.total / 1024 ** 3, 1)})
        except OSError:
            pass
    d["disks"] = disks
    d["mode"] = perf_mode()
    with _lock:
        _live.update(t=now, data=d)
    return d


# ------------------------------------------------------------------ chính sách dựng video
def perf_mode() -> str:
    m = settings._load().get("perf_mode", "gpu")          # noqa: SLF001
    return m if m in ("gpu", "max", "eco") else "gpu"


def cpu_threads() -> int:
    """Số luồng CPU cho mỗi tiến trình x264/bộ lọc: chừa lại lõi cho Windows và các app khác."""
    n = os.cpu_count() or 2
    m = perf_mode()
    if m == "max":
        return 0                                       # 0 = ffmpeg tự dùng hết
    if m == "eco":
        return max(1, n // 4)
    return max(2, n - 2) if n <= 8 else max(4, int(n * 0.6))


def parallel_segments(encoder: str | None) -> int:
    """Số đoạn dựng song song."""
    n = os.cpu_count() or 2
    m = perf_mode()
    if m == "eco":
        return 1
    ram = psutil.virtual_memory().total / 1024 ** 3 if psutil else 8
    by_ram = max(1, int(ram // 4))                     # ~4 GB RAM cho mỗi tiến trình dựng 1080p
    if encoder:                                        # GPU mã hoá: CPU chỉ còn giải mã + bộ lọc
        want = 4 if m == "max" else 3
    else:
        want = max(1, n // 4) if m == "gpu" else max(1, n // 2)
    return max(1, min(want, by_ram, 6))


def low_priority() -> bool:
    return perf_mode() != "max"


def priority_flags() -> int:
    return BELOW_NORMAL if (os.name == "nt" and low_priority()) else 0
