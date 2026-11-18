"""Nhạc nền tự động: Claude đọc nội dung video -> chọn bài hợp nhất trong kho nhạc.

Kho nhạc = Thư mục đầu vào / Nhạc nền (dùng chung mọi dự án):
  - Bạn thả nhạc của mình vào đây (mp3/wav/m4a/ogg/flac). Tên file nên mô tả tâm trạng, vd "Hồi hộp - trống điện tử.mp3".
  - Nếu kho chưa có bài hợp, app tìm nhạc miễn phí bản quyền trên Openverse (Jamendo, Freesound, Wikimedia; không cần key),
    chỉ lấy giấy phép cho phép dùng thương mại & chỉnh sửa (CC0, PDM, CC BY, CC BY-SA), tải về Nhạc nền/AI để lần sau dùng lại.
"""
import json
import logging
import re
import time
import urllib.parse
from pathlib import Path

from ..config import settings
from . import folders, stock_service
from .claude_client import get_client
from .ffmpeg_service import probe

log = logging.getLogger("mre.music")

AUDIO_EXT = {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac", ".opus"}
OPENVERSE = "https://api.openverse.org/v1/audio/"
SAFE_LICENSES = {"cc0", "pdm", "by", "by-sa"}          # dùng thương mại + được ghép vào video
MAX_LIBRARY = 300
_CACHE_FILE = ".mre_music_index.json"


# ------------------------------------------------------------------ kho nhạc trong máy
def _meta_from_name(path: Path) -> str:
    return re.sub(r"[_\-]+", " ", path.stem).strip()


def scan_library(root: Path | None = None) -> list[dict]:
    """Liệt kê nhạc trong kho (kể cả thư mục con 1 cấp), có thời lượng; lưu đệm theo ngày sửa file."""
    root = root or folders.music_dir()
    cache_path = root / _CACHE_FILE
    try:
        cache = json.loads(cache_path.read_text("utf-8"))
    except (OSError, ValueError):
        cache = {}
    files = [p for p in root.iterdir() if p.is_file()] if root.exists() else []
    for d in [p for p in root.iterdir() if p.is_dir()] if root.exists() else []:
        files += [p for p in d.iterdir() if p.is_file()]
    files = sorted((p for p in files if p.suffix.lower() in AUDIO_EXT), key=lambda p: p.stat().st_mtime, reverse=True)
    out, changed = [], False
    for p in files[:MAX_LIBRARY]:
        key, mt = str(p.relative_to(root)), int(p.stat().st_mtime)
        c = cache.get(key)
        if not c or c.get("mt") != mt:
            try:
                dur = float(probe(p).get("duration") or 0)
            except Exception:
                dur = 0.0
            sidecar = p.with_suffix(".json")                     # thông tin giấy phép của bài AI đã tải
            extra = {}
            if sidecar.exists():
                try:
                    extra = json.loads(sidecar.read_text("utf-8"))
                except (OSError, ValueError):
                    extra = {}
            c = {"mt": mt, "duration": dur, "desc": extra.get("desc") or _meta_from_name(p),
                 "license": extra.get("license"), "attribution": extra.get("attribution")}
            cache[key], changed = c, True
        if c["duration"] >= 20:
            out.append({"id": "lib:" + key, "path": str(p), "title": p.stem, "desc": c["desc"], "duration": c["duration"],
                        "source": "library", "license": c.get("license"), "attribution": c.get("attribution")})
    if changed:
        try:
            cache_path.write_text(json.dumps(cache, ensure_ascii=False), "utf-8")
        except OSError:
            pass
    return out


# ------------------------------------------------------------------ nhạc miễn phí trên mạng (Openverse)
def search_online(query: str, min_seconds: float = 0, n: int = 12) -> list[dict]:
    params = {"q": query[:190], "category": "music", "license": ",".join(sorted(SAFE_LICENSES)),
              "length": "medium,long", "page_size": min(n, 20), "mature": "false"}
    try:
        d = stock_service._get_json(OPENVERSE + "?" + urllib.parse.urlencode(params))   # noqa: SLF001
    except Exception as e:
        log.warning("Openverse lỗi (%s): %s", query, e)
        return []
    out = []
    for r in d.get("results") or []:
        lic = (r.get("license") or "").lower()
        url = r.get("url") or ""
        dur = (r.get("duration") or 0) / 1000
        if lic not in SAFE_LICENSES or not url or dur < max(30, min_seconds * 0.35):
            continue
        tags = [t.get("name") for t in (r.get("tags") or []) if isinstance(t, dict) and t.get("name")][:12]
        out.append({"id": "ov:" + str(r.get("id")), "url": url, "title": r.get("title") or "Không tên",
                    "creator": r.get("creator") or "", "desc": ", ".join((r.get("genres") or []) + tags),
                    "duration": dur, "source": "openverse:" + (r.get("source") or ""), "license": lic,
                    "license_version": r.get("license_version"), "landing": r.get("foreign_landing_url"),
                    "attribution": r.get("attribution") or "", "ext": (r.get("filetype") or "mp3").lower()})
    return out


def download_track(item: dict) -> Path:
    """Tải bài nhạc online vào Nhạc nền/AI (kèm file .json ghi giấy phép) để dùng lại cho các dự án sau."""
    d = folders.music_dir() / "AI"
    d.mkdir(parents=True, exist_ok=True)
    ext = "." + re.sub(r"[^a-z0-9]", "", item.get("ext") or "mp3")[:5]
    if ext not in AUDIO_EXT:
        ext = ".mp3"
    name = folders.safe_name(f"{item['title']} - {item.get('creator') or 'Openverse'}")[:90]
    dest = d / f"{name}{ext}"
    if not dest.exists():
        stock_service.download(item["url"], dest, max_bytes=60 * 1024 * 1024)
    meta = {"desc": item.get("desc") or "", "license": item.get("license"), "license_version": item.get("license_version"),
            "attribution": item.get("attribution"), "landing": item.get("landing"), "source": item.get("source")}
    dest.with_suffix(".json").write_text(json.dumps(meta, ensure_ascii=False, indent=1), "utf-8")
    return dest


# ------------------------------------------------------------------ Claude chọn nhạc
BRIEF_TOOL = {
    "name": "music_brief",
    "description": "Mô tả nhạc nền phù hợp cho video.",
    "input_schema": {"type": "object", "properties": {
        "mood": {"type": "string", "description": "Tâm trạng/nhịp bằng tiếng Việt, 1 câu."},
        "queries": {"type": "array", "items": {"type": "string"}, "maxItems": 3,
                    "description": "2-3 từ khoá TIẾNG ANH để tìm nhạc không lời, vd 'suspense cinematic', 'calm piano'."},
    }, "required": ["mood", "queries"]},
}
PICK_TOOL = {
    "name": "pick_music",
    "description": "Chọn bài nhạc nền.",
    "input_schema": {"type": "object", "properties": {
        "choice": {"type": "integer", "description": "Số thứ tự bài chọn; -1 nếu không bài nào hợp."},
        "volume": {"type": "number", "minimum": 0.05, "maximum": 0.35,
                   "description": "Âm lượng nhạc so với giọng (0.12-0.2 thường hợp; nhạc dồn dập thì thấp hơn)."},
        "reason": {"type": "string", "description": "Lý do ngắn bằng tiếng Việt."},
    }, "required": ["choice", "volume", "reason"]},
}


def _ask(client, tool: dict, system: str, payload: dict, max_tokens: int = 1200) -> dict:
    resp = client.messages.create(model=settings.claude_fast_model or settings.claude_model, max_tokens=max_tokens,
                                  system=system, tools=[tool], tool_choice={"type": "tool", "name": tool["name"]},
                                  messages=[{"role": "user", "content": json.dumps(payload, ensure_ascii=False)}])
    block = next((b for b in resp.content if getattr(b, "type", "") == "tool_use"), None)
    return (block.input or {}) if block else {}


def brief(summary: dict, client=None) -> dict:
    client = client or get_client()
    d = _ask(client, BRIEF_TOOL, "Bạn là người chọn nhạc nền cho video YouTube. Nhạc phải KHÔNG LỜI, không lấn giọng đọc.", summary)
    qs = [q.strip() for q in d.get("queries") or [] if isinstance(q, str) and q.strip()][:3] or ["cinematic background instrumental"]
    return {"mood": (d.get("mood") or "").strip(), "queries": qs}


def choose(summary: dict, mood: str, candidates: list[dict], client=None) -> dict | None:
    if not candidates:
        return None
    client = client or get_client()
    rows = [{"n": i, "title": c["title"], "info": c.get("desc", "")[:160], "seconds": round(c["duration"]),
             "from": "kho của bạn" if c["source"] == "library" else "mạng"} for i, c in enumerate(candidates)]
    d = _ask(client, PICK_TOOL,
             "Chọn 1 bài nhạc nền KHÔNG LỜI hợp nhất với video. Ưu tiên bài trong 'kho của bạn' nếu hợp ngang nhau; "
             "tránh bài có lời hát, nhạc vui tươi cho chủ đề nghiêm túc và ngược lại. Không bài nào hợp -> choice = -1.",
             {**summary, "mood_needed": mood, "tracks": rows})
    try:
        i = int(d.get("choice", -1))
    except (TypeError, ValueError):
        return None
    if not 0 <= i < len(candidates):
        return None
    try:
        vol = min(0.35, max(0.05, float(d.get("volume") or 0.15)))
    except (TypeError, ValueError):
        vol = 0.15
    return {**candidates[i], "volume": vol, "reason": (d.get("reason") or "").strip()}


def auto_pick(summary: dict, seconds: float, online: bool = True, client=None) -> dict | None:
    """summary: {title, style, script (trích), language}. Trả bài đã chọn (có 'path' trên máy) hoặc None."""
    client = client or get_client()
    b = brief(summary, client)
    lib = scan_library()
    words = {w for q in b["queries"] for w in q.lower().split() if len(w) > 2}
    # kho lớn: đưa Claude những bài có tên/mô tả gần từ khoá nhất trước
    lib.sort(key=lambda c: -sum(w in (c["title"] + " " + c["desc"]).lower() for w in words))
    cands = lib[:25]
    if online:
        seen = set()
        for q in b["queries"]:
            for it in search_online(q, seconds):
                if it["id"] not in seen:
                    seen.add(it["id"])
                    cands.append(it)
            time.sleep(0.3)                       # lịch sự với API không cần key
        cands = cands[:45]
    pick = choose(summary, b["mood"], cands, client)
    if pick is None:
        return None
    if pick["source"] != "library":
        pick["path"] = str(download_track(pick))
    return {**pick, "mood": b["mood"]}


def credit_line(pick: dict) -> str:
    if pick.get("source") == "library" and not pick.get("license"):
        return ""
    lic = (pick.get("license") or "").upper()
    if lic in ("CC0", "PDM"):
        return f"Nhạc nền: {pick.get('title')} ({lic}, không bắt buộc ghi nguồn)"
    return "Nhạc nền: " + (pick.get("attribution") or f"{pick.get('title')} — {pick.get('creator', '')} ({lic})")
