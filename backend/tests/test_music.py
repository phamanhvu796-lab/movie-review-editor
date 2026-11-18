import json
import shutil
import subprocess
from types import SimpleNamespace as NS

import pytest

from app.services import folders, music_service

pytestmark = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="cần ffmpeg")


def _mp3(path, seconds):
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", f"sine=f=330:d={seconds}",
                    "-c:a", "libmp3lame", str(path)], check=True)
    return path


class FakeClaude:
    """Lần 1 trả music_brief, lần 2 chọn bài có tên chứa 'suspense'."""
    def __init__(self):
        self.calls = []
        self.messages = NS(create=self.create)

    def create(self, **kw):
        self.calls.append(kw)
        name = kw["tool_choice"]["name"]
        if name == "music_brief":
            inp = {"mood": "hồi hộp, chậm", "queries": ["suspense cinematic", "dark ambient"]}
        else:
            tracks = json.loads(kw["messages"][0]["content"])["tracks"]
            n = next((t["n"] for t in tracks if "suspense" in (t["title"] + t["info"]).lower()), -1)
            inp = {"choice": n, "volume": 0.9, "reason": "hợp không khí hồi hộp"}
        return NS(content=[NS(type="tool_use", input=inp)])


@pytest.fixture
def lib(tmp_path, monkeypatch):
    monkeypatch.setattr(folders, "input_dir", lambda: tmp_path / "input")
    d = folders.music_dir()
    _mp3(d / "Vui tuoi - ukulele.mp3", 25)
    _mp3(d / "Hoi hop - suspense drums.mp3", 30)
    _mp3(d / "Ngan qua.mp3", 5)                       # quá ngắn -> bỏ
    (d / "ghi chu.txt").write_text("x")
    return d


def test_scan_library_durations_cache_and_sidecar(lib):
    _mp3(lib / "AI" / "Calm piano - Ann.mp3", 22)
    (lib / "AI" / "Calm piano - Ann.json").write_text(json.dumps({"desc": "piano, calm", "license": "by",
                                                                   "attribution": "Calm piano by Ann, CC BY 4.0"}))
    items = music_service.scan_library()
    titles = {i["title"] for i in items}
    assert titles == {"Vui tuoi - ukulele", "Hoi hop - suspense drums", "Calm piano - Ann"}
    ai = next(i for i in items if i["title"].startswith("Calm"))
    assert ai["license"] == "by" and ai["desc"] == "piano, calm" and 21 < ai["duration"] < 23
    assert (lib / ".mre_music_index.json").exists()
    assert music_service.scan_library() == items        # lần 2 dùng bộ đệm, cùng kết quả


def test_search_online_filters_licenses_and_short(monkeypatch):
    seen = {}

    def fake_get(url, headers=None, timeout=20):
        seen["url"] = url
        return {"results": [
            {"id": 1, "title": "Dark Suspense", "creator": "A", "license": "by", "license_version": "4.0", "url": "https://x/1.mp3",
             "duration": 120000, "genres": ["cinematic"], "tags": [{"name": "tension"}], "attribution": "Dark Suspense by A", "filetype": "mp3"},
            {"id": 2, "title": "NC song", "license": "by-nc", "url": "https://x/2.mp3", "duration": 120000},
            {"id": 3, "title": "Tiny", "license": "cc0", "url": "https://x/3.mp3", "duration": 8000},
            {"id": 4, "title": "No url", "license": "cc0", "url": None, "duration": 90000}]}
    monkeypatch.setattr(music_service.stock_service, "_get_json", fake_get)
    out = music_service.search_online("suspense", 60)
    assert [o["title"] for o in out] == ["Dark Suspense"] and out[0]["duration"] == 120
    assert "category=music" in seen["url"] and "by-nc" not in seen["url"] and "cc0" in seen["url"]


def test_auto_pick_prefers_library_and_clamps_volume(lib, monkeypatch):
    monkeypatch.setattr(music_service, "search_online", lambda q, s=0, n=12: [])
    fc = FakeClaude()
    pick = music_service.auto_pick({"title": "Phim kinh dị", "script_excerpt": "..."}, 60, client=fc)
    assert pick["title"] == "Hoi hop - suspense drums" and pick["source"] == "library"
    assert pick["volume"] == 0.35 and pick["mood"] == "hồi hộp, chậm" and pick["path"].endswith(".mp3")
    assert music_service.credit_line(pick) == ""            # nhạc của bạn -> không cần ghi nguồn


def test_auto_pick_downloads_online_track_with_license(lib, monkeypatch, tmp_path):
    for f in list(lib.glob("*.mp3")):
        f.unlink()
    src = _mp3(tmp_path / "src.mp3", 40)
    monkeypatch.setattr(music_service, "search_online", lambda q, s=0, n=12: [
        {"id": "ov:9", "url": "https://x/9.mp3", "title": "Night Suspense", "creator": "Bob", "desc": "suspense",
         "duration": 40, "source": "openverse:jamendo", "license": "by", "license_version": "4.0",
         "landing": "https://j/9", "attribution": "Night Suspense by Bob (CC BY 4.0)", "ext": "mp3"}])
    monkeypatch.setattr(music_service.stock_service, "download", lambda url, dest, max_bytes=0: shutil.copy(src, dest) and dest)
    monkeypatch.setattr(music_service.time, "sleep", lambda s: None)
    pick = music_service.auto_pick({"title": "x"}, 30, client=FakeClaude())
    p = folders.music_dir() / "AI" / "Night Suspense - Bob.mp3"
    assert pick["path"] == str(p) and p.exists()
    assert json.loads(p.with_suffix(".json").read_text())["license"] == "by"
    assert "CC BY" in music_service.credit_line(pick)
    # lần sau bài này đã nằm trong kho, có giấy phép
    again = {i["title"]: i for i in music_service.scan_library()}
    assert again["Night Suspense - Bob"]["license"] == "by"


def test_no_match_returns_none(lib, monkeypatch):
    monkeypatch.setattr(music_service, "search_online", lambda q, s=0, n=12: [])
    fc = FakeClaude()
    fc.create = lambda **kw: NS(content=[NS(type="tool_use", input={"mood": "x", "queries": ["x"]} if
                                             kw["tool_choice"]["name"] == "music_brief" else {"choice": -1, "volume": 0.1, "reason": ""})])
    fc.messages = NS(create=fc.create)
    assert music_service.auto_pick({"title": "x"}, 30, client=fc) is None
