import os
import shutil
import sys
from pathlib import Path


def resource_dir() -> Path:
    """Thư mục chứa static/ và bin/ (dev: backend/, exe: _internal/)."""
    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[2]))


def data_dir() -> Path:
    d = Path(os.getenv("MRE_DATA_DIR") or (Path.home() / "MovieReviewEditor"))
    d.mkdir(parents=True, exist_ok=True)
    return d


def find_binary(name: str) -> str | None:
    exe = name + (".exe" if os.name == "nt" else "")
    for base in (resource_dir() / "bin", Path(sys.executable).parent / "bin", data_dir() / "bin"):
        if (base / exe).exists():
            return str(base / exe)
    return shutil.which(name)
