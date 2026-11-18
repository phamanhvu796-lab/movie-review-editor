"""Tìm & tải ảnh/video minh hoạ từ kho MIỄN PHÍ BẢN QUYỀN (dùng được cho video thương mại / YouTube):

- Pexels   (ảnh + video)        — cần API key miễn phí: https://www.pexels.com/api/
- Pixabay  (ảnh, hình minh hoạ, video) — cần API key miễn phí: https://pixabay.com/api/docs/
- Wikimedia Commons (ảnh tư liệu, bản đồ, tranh lịch sử) — không cần key; nhiều ảnh yêu cầu GHI NGUỒN (app tự lập danh sách).

Không lấy ảnh từ Google/Facebook/YouTube: phần lớn có bản quyền, dễ bị gậy khi đăng video.
Ảnh tĩnh được chuyển thành clip 24 giây (nền mờ + ảnh giữa nếu khác tỉ lệ) để bộ dựng áp hiệu ứng zoom/pan.
Video được cắt ngắn (bỏ tiếng) ngay sau khi tải để tiết kiệm ổ đĩa.
"""
import hashlib
import json
import logging
import re
import urllib.parse
import urllib.request
from html import unescape
from pathlib import Path

from ..config import settings
from ..version import __version__
from .ffmpeg_service import probe, run_ffmpeg

log = logging.getLogger("mre.stock")
UA = f"MovieReviewEditor/{__version__} (video editor; contact: app user)"
MAX_DOWNLOAD = 120 * 1024 ** 2
PHOTO_CLIP_SECONDS = 24.0          # ảnh tĩnh nén gần như 0 byte; dài để lấy nhiều đoạn khác nhau


def keys() -> dict:
    d = settings._load()                                      # noqa: SLF001
    return {"pexels": (d.get("stock_pexels_key") or "").strip(), "pixabay": (d.get("stock_pixabay_key") or "").strip()}


def available_sources() -> list[str]:
    k = keys()
    return [s for s in ("pexels", "pixabay") if k[s]] + ["wikimedia"]


def _get_json(url: str, headers: dict | None = None, timeout: float = 20) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json", **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def _strip_html(s: str) -> str:
    return re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]+>", "", s or ""))).strip()


# ------------------------------------------------------------------ tìm kiếm từng nguồn
def _pick_video_file(files: list[dict], vertical: bool) -> dict | None:
    """Chọn bản gần 1920 (ngang) / 1080 (dọc) nhưng không lớn hơn -> đủ nét cho 1080p, đỡ nặng."""
    target = 1080 if vertical else 1920
    ok = [f for f in files if f.get("link") or f.get("url")]
    ok = [f for f in ok if (f.get("width") or 0) and (f.get("height") or 0)]
    if not ok:
        return None
    under = [f for f in ok if max(f["width"], f["height"]) <= max(target, 1920)]
    pool = under or ok
    return min(pool, key=lambda f: abs((f["width"] if not vertical else min(f["width"], f["height"])) - target))


def search_pexels(query: str, kind: str, vertical: bool, n: int = 6) -> list[dict]:
    key = keys()["pexels"]
    if not key:
        return []
    q = urllib.parse.urlencode({"query": query, "per_page": n, "orientation": "portrait" if vertical else "landscape"})
    if kind == "video":
        d = _get_json(f"https://api.pexels.com/videos/search?{q}", {"Authorization": key})
        out = []
        for v in d.get("videos", []):
            f = _pick_video_file(v.get("video_files", []), vertical)
            if f:
                out.append({"source": "pexels", "kind": "video", "id": f"pexels_v{v['id']}", "url": f["link"],
                            "width": f["width"], "height": f["height"], "duration": v.get("duration"),
                            "author": (v.get("user") or {}).get("name", ""), "page": v.get("url", ""),
                            "license": "Pexels License (miễn phí, không bắt buộc ghi nguồn)"})
        return out
    d = _get_json(f"https://api.pexels.com/v1/search?{q}", {"Authorization": key})
    return [{"source": "pexels", "kind": "photo", "id": f"pexels_p{p['id']}", "url": p["src"].get("large2x") or p["src"]["original"],
             "width": p.get("width"), "height": p.get("height"), "author": p.get("photographer", ""), "page": p.get("url", ""),
             "license": "Pexels License (miễn phí, không bắt buộc ghi nguồn)"} for p in d.get("photos", [])]


def search_pixabay(query: str, kind: str, vertical: bool, n: int = 6, photo_type: str = "photo") -> list[dict]:
    key = keys()["pixabay"]
    if not key:
        return []
    base = {"key": key, "q": query[:100], "per_page": max(3, n), "safesearch": "true"}
    if kind == "video":
        d = _get_json("https://pixabay.com/api/videos/?" + urllib.parse.urlencode(base))
        out = []
        for h in d.get("hits", []):
            vids = [{**v, "link": v.get("url")} for v in (h.get("videos") or {}).values() if v.get("url")]
            f = _pick_video_file(vids, vertical)
            if f:
                out.append({"source": "pixabay", "kind": "video", "id": f"pixabay_v{h['id']}", "url": f["url"],
                            "width": f["width"], "height": f["height"], "duration": h.get("duration"),
                            "author": h.get("user", ""), "page": h.get("pageURL", ""),
                            "license": "Pixabay Content License (miễn phí, không bắt buộc ghi nguồn)"})
        return out
    d = _get_json("https://pixabay.com/api/?" + urllib.parse.urlencode(
        {**base, "image_type": photo_type if photo_type in ("photo", "illustration", "vector") else "photo",
         "orientation": "vertical" if vertical else "horizontal"}))
    return [{"source": "pixabay", "kind": "photo", "id": f"pixabay_p{h['id']}", "url": h.get("largeImageURL"),
             "width": h.get("imageWidth"), "height": h.get("imageHeight"), "author": h.get("user", ""),
             "page": h.get("pageURL", ""), "license": "Pixabay Content License (miễn phí, không bắt buộc ghi nguồn)"}
            for h in d.get("hits", []) if h.get("largeImageURL")]


def search_wikimedia(query: str, n: int = 6) -> list[dict]:
    q = urllib.parse.urlencode({"action": "query", "format": "json", "generator": "search", "gsrnamespace": 6,
                                "gsrsearch": f"{query} filetype:bitmap", "gsrlimit": n, "prop": "imageinfo",
                                "iiprop": "url|size|mime|extmetadata", "iiurlwidth": 1920})
    d = _get_json(f"https://commons.wikimedia.org/w/api.php?{q}")
    out = []
    for p in sorted((d.get("query") or {}).get("pages", {}).values(), key=lambda x: x.get("index", 0)):
        ii = (p.get("imageinfo") or [{}])[0]
        if ii.get("mime") not in ("image/jpeg", "image/png") or (ii.get("width") or 0) < 640:
            continue
        meta = ii.get("extmetadata") or {}
        lic = _strip_html((meta.get("LicenseShortName") or {}).get("value", ""))
        out.append({"source": "wikimedia", "kind": "photo", "id": f"wiki_{p.get('pageid')}", "url": ii.get("thumburl") or ii.get("url"),
                    "width": ii.get("thumbwidth") or ii.get("width"), "height": ii.get("thumbheight") or ii.get("height"),
                    "author": _strip_html((meta.get("Artist") or {}).get("value", ""))[:120],
                    "page": ii.get("descriptionurl", ""), "license": f"Wikimedia Commons — {lic or 'xem trang nguồn'} (cần ghi nguồn)",
                    "title": p.get("title", "")})
    return out


def search(query: str, kind: str, vertical: bool, sources: list[str], photo_type: str = "photo", n: int = 6) -> list[dict]:
    """Tìm theo thứ tự nguồn ưu tiên; nguồn lỗi/thiếu key thì bỏ qua. kind: video | photo."""
    have = available_sources()
    out: list[dict] = []
    for s in sources:
        if s not in have:
            continue
        try:
            if s == "pexels":
                out += search_pexels(query, kind, vertical, n)
            elif s == "pixabay":
                out += search_pixabay(query, kind, vertical, n, photo_type)
            elif s == "wikimedia" and kind == "photo":
                out += search_wikimedia(query, n)
        except Exception as e:
            log.warning("Tìm '%s' trên %s lỗi: %s", query, s, e)
        if len(out) >= n:
            break
    return out


# ------------------------------------------------------------------ tải & chuẩn hoá
def cache_name(item: dict) -> str:
    return re.sub(r"[^A-Za-z0-9_]+", "_", item["id"])[:60] + "_" + hashlib.sha1(item["url"].encode()).hexdigest()[:8]


def download(url: str, dest: Path, max_bytes: int = MAX_DOWNLOAD) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_suffix(dest.suffix + ".part")
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    got = 0
    with urllib.request.urlopen(req, timeout=60) as r, open(part, "wb") as f:
        while True:
            chunk = r.read(1 << 20)
            if not chunk:
                break
            got += len(chunk)
            if got > max_bytes:
                raise RuntimeError("File quá lớn, bỏ qua")
            f.write(chunk)
    part.replace(dest)
    return dest


def photo_to_clip(img: Path, out: Path, W: int, H: int, seconds: float = PHOTO_CLIP_SECONDS) -> Path:
    """Ảnh -> clip tĩnh. Khác tỉ lệ khung nhiều: nền là chính ảnh phóng to làm mờ, ảnh gốc đặt giữa (không bị cắt mất)."""
    info = probe(img)
    iw, ih = info.get("width") or W, info.get("height") or H
    ratio_diff = abs((iw / ih) - (W / H)) / (W / H)
    if ratio_diff < 0.18:
        vf = f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},setsar=1,format=yuv420p"
        args = ["-loop", "1", "-t", f"{seconds:.2f}", "-i", str(img), "-vf", vf]
    else:
        fc = (f"[0:v]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},boxblur=24:2,eq=brightness=-0.08[bg];"
              f"[0:v]scale={W}:{H}:force_original_aspect_ratio=decrease[fg];[bg][fg]overlay=(W-w)/2:(H-h)/2,setsar=1,format=yuv420p")
        args = ["-loop", "1", "-t", f"{seconds:.2f}", "-i", str(img), "-filter_complex", fc]
    run_ffmpeg([*args, "-r", "30", "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-an", str(out)])
    return out


def trim_video(src: Path, out: Path, seconds: float) -> Path:
    """Giữ đoạn đầu đủ dùng, bỏ tiếng (không mã hoá lại -> nhanh)."""
    run_ffmpeg(["-i", str(src), "-t", f"{seconds:.2f}", "-an", "-c:v", "copy", "-movflags", "+faststart", str(out)])
    return out


def credits_text(items: list[dict]) -> str:
    lines = ["NGUỒN HÌNH ẢNH / VIDEO MINH HOẠ", ""]
    need = [i for i in items if i.get("source") == "wikimedia"]
    other = [i for i in items if i.get("source") != "wikimedia"]
    if need:
        lines.append("Bắt buộc ghi nguồn (Wikimedia Commons):")
        lines += [f"- {i.get('title') or i.get('id')} — {i.get('author') or 'không rõ tác giả'} — {i.get('license')} — {i.get('page')}"
                  for i in need]
        lines.append("")
    if other:
        lines.append("Pexels / Pixabay (không bắt buộc ghi nguồn, ghi để cảm ơn tác giả):")
        lines += [f"- {i.get('author') or 'tác giả'} — {i.get('source').title()} — {i.get('page')}" for i in other]
    return "\n".join(lines)
