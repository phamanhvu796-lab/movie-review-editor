"""Chế độ Kịch bản -> Video: phong cách, đọc kết quả kho ảnh, lắp bản dựng, dựng video thật bằng FFmpeg."""
import shutil
import subprocess
import tempfile
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from app.schemas import Cue, EditPlan, Footage, PlanContext, Scene
from app.services import broll_service, stock_service, styles
from app.services.render_service import PRESETS
from app.services.validator_service import validate_plan

HAS_FFMPEG = shutil.which("ffmpeg") is not None


def test_all_styles_are_consistent():
    trans = {"none", "flash_cut", "fade", "cross_dissolve", "whip_pan", "shake"}
    ids = [s["id"] for s in styles.listing()]
    assert len(ids) == 19 and {s["group"] for s in styles.listing()} == {"news", "history", "finance"}
    for sid in ids:
        st = styles.STYLES[sid]
        if st["disabled"]:
            with pytest.raises(ValueError):
                styles.get(sid)
            continue
        assert st["filter"] is None or st["filter"] in PRESETS, sid
        assert st["transition"] in trans and st["aspect"] in ("16:9", "9:16") and 1.5 <= st["pace"] <= 7.5
        assert set(st["sources"]) <= {"pexels", "pixabay", "wikimedia"}


def test_source_parsers(monkeypatch):
    monkeypatch.setattr(stock_service, "keys", lambda: {"pexels": "k1", "pixabay": "k2"})
    calls = []

    def fake(url, headers=None, timeout=20):
        calls.append((url, headers))
        if "pexels.com/videos" in url:
            return {"videos": [{"id": 7, "duration": 14, "url": "https://pexels.com/v/7", "user": {"name": "An"},
                                "video_files": [{"link": "https://v/4k.mp4", "width": 3840, "height": 2160},
                                                {"link": "https://v/hd.mp4", "width": 1920, "height": 1080},
                                                {"link": "https://v/sd.mp4", "width": 640, "height": 360}]}]}
        if "pexels.com/v1" in url:
            return {"photos": [{"id": 9, "width": 4000, "height": 3000, "photographer": "Bình", "url": "https://pexels.com/p/9",
                                "src": {"large2x": "https://p/9.jpg", "original": "https://p/9o.jpg"}}]}
        if "pixabay.com/api/videos" in url:
            return {"hits": [{"id": 3, "duration": 20, "user": "c", "pageURL": "https://pixabay.com/v/3",
                              "videos": {"large": {"url": "https://x/l.mp4", "width": 1920, "height": 1080},
                                         "small": {"url": "https://x/s.mp4", "width": 960, "height": 540}}}]}
        if "pixabay.com/api" in url:
            assert "image_type=illustration" in url
            return {"hits": [{"id": 5, "largeImageURL": "https://x/5.jpg", "imageWidth": 1280, "imageHeight": 720,
                              "user": "d", "pageURL": "https://pixabay.com/p/5"}]}
        if "wikimedia" in url:
            return {"query": {"pages": {"1": {"pageid": 11, "index": 1, "title": "File:Map 1858.jpg", "imageinfo": [{
                "mime": "image/jpeg", "width": 3000, "height": 2000, "thumburl": "https://w/t.jpg", "thumbwidth": 1920,
                "thumbheight": 1280, "descriptionurl": "https://commons.wikimedia.org/wiki/File:Map",
                "extmetadata": {"Artist": {"value": "<a href='x'>Nguyễn &amp; Co</a>"}, "LicenseShortName": {"value": "CC BY-SA 4.0"}}}]}}}}
        raise AssertionError(url)
    monkeypatch.setattr(stock_service, "_get_json", fake)
    v = stock_service.search_pexels("city", "video", False)
    assert v[0]["url"] == "https://v/hd.mp4" and v[0]["author"] == "An"          # chọn bản 1920, không lấy 4K
    assert calls[-1][1] == {"Authorization": "k1"}
    assert stock_service.search_pexels("city", "photo", True)[0]["url"] == "https://p/9.jpg"
    assert "orientation=portrait" in calls[-1][0]
    assert stock_service.search_pixabay("coin", "video", False)[0]["url"] == "https://x/l.mp4"
    assert stock_service.search_pixabay("coin", "photo", False, photo_type="illustration")[0]["id"] == "pixabay_p5"
    w = stock_service.search_wikimedia("map")[0]
    assert w["author"] == "Nguyễn & Co" and "CC BY-SA 4.0" in w["license"] and "ghi nguồn" in w["license"]
    # nguồn lỗi -> bỏ qua, dùng nguồn khác
    monkeypatch.setattr(stock_service, "search_pexels", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("net")))
    got = stock_service.search("map", "photo", False, ["pexels", "wikimedia"])
    assert got and got[0]["source"] == "wikimedia"
    txt = stock_service.credits_text([w, v[0]])
    assert "Bắt buộc ghi nguồn" in txt and "Map 1858" in txt and "Pexels" in txt


def _cues(durs):
    t, out = 0.0, []
    for i, d in enumerate(durs, 1):
        out.append(Cue(index=i, start=round(t, 3), end=round(t + d, 3), text=f"Câu {i}"))
        t += d
    return out


def _ctx(cues, scenes, durations):
    return PlanContext(movie_title="t", voice_duration=cues[-1].end, srt_cues=cues, available_filters=list(PRESETS),
                       available_footage=[Footage(file=n, duration=durations[n], scenes=[Scene(**s) for s in sc])
                                          for n, sc in scenes.items()])


@pytest.mark.parametrize("style_id", ["hist_archive", "news_short", "fin_faceless", "news_explainer"])
def test_build_plan_is_valid(style_id):
    st = styles.get(style_id)
    cues = _cues([2.4, 9.7, 4.1, 0.8, 15.2, 3.3, 6.0])
    pools = {
        "a.mp4": {"name": "a.mp4", "duration": 24.0, "kind": "photo"},
        "b.mp4": {"name": "b.mp4", "duration": 14.0, "kind": "video"},
        "c.mp4": {"name": "c.mp4", "duration": 3.2, "kind": "video"},        # clip ngắn
        "d.mp4": {"name": "d.mp4", "duration": 24.0, "kind": "photo"},
    }
    media = {1: [pools["a.mp4"]], 2: [pools["b.mp4"], pools["c.mp4"]], 3: [pools["d.mp4"]], 4: [pools["a.mp4"]],
             5: [pools["b.mp4"], pools["d.mp4"]], 6: [pools["a.mp4"]], 7: [pools["c.mp4"]]}
    plan, scenes = broll_service.build_plan(cues, media, st)
    durations = {n: v["duration"] for n, v in pools.items()}
    parsed, issues = validate_plan(plan, _ctx(cues, scenes, durations))
    assert not issues, [str(i) for i in issues]
    for sec in plan["sections"]:
        assert all(v.get("filter") == st["filter"] for v in sec["visuals"]) if st["filter"] else True
    photo_vis = [v for s in plan["sections"] for v in s["visuals"] if v["footage"] in ("a.mp4", "d.mp4")]
    assert any(v.get("zoom_end", 1) > 1 or v.get("zoom_start", 1) > 1 for v in photo_vis)   # Ken Burns


def test_presenter_interleave_is_in_sync():
    st = styles.get("news_anchor")
    cues = _cues([3.0, 4.0, 5.0, 3.5, 4.0, 2.5, 9.0])
    pool = {"name": "b.mp4", "duration": 20.0, "kind": "video"}
    media = {c.index: [pool] for c in cues}
    plan, scenes = broll_service.build_plan(cues, media, st, presenter={"name": "me.mp4", "duration": 40.0})
    pres = [s for s in plan["sections"] if s["visuals"][0]["footage"] == "me.mp4"]
    assert [s["srt_index"] for s in pres] == [1, 4, 7]
    for s in pres:                                     # hình người dẫn trùng đúng thời điểm lời nói
        assert abs(s["visuals"][0]["start"] - s["voice_start"]) < 0.01
    _, issues = validate_plan(plan, _ctx(cues, scenes, {"b.mp4": 20.0, "me.mp4": 40.0}))
    assert not issues, [str(i) for i in issues]


def test_plan_queries_fills_gaps_and_forces_media():
    st = styles.get("hist_doc")
    cues = _cues([3, 3, 3])
    resp = NS(content=[NS(type="tool_use", input={"shots": [
        {"i": 1, "q": ["Hue citadel", "old palace"], "kind": "photo"}, {"i": 3, "q": ["battle painting"], "kind": "video"}]})])
    client = NS(messages=NS(create=lambda **kw: resp))
    out = broll_service.plan_queries(cues, st, "photo", "Nhà Nguyễn", client=client)
    assert out[2]["q"] == ["Hue citadel", "old palace"] and out[2]["same_as_prev"]
    assert all(v["kind"] == "photo" for v in out.values())


@pytest.mark.skipif(not HAS_FFMPEG, reason="cần ffmpeg")
def test_real_render_end_to_end(tmp_path, monkeypatch):
    """Ảnh dọc -> clip 16:9 nền mờ; video thử; giọng thử -> lắp bản dựng -> dựng MP4 thật (preview)."""
    monkeypatch.setenv("MRE_DATA_DIR", str(tmp_path / "data"))
    from PIL import Image
    from app.services.ffmpeg_service import probe
    from app.services.render_service import RenderOptions, render_video
    img = tmp_path / "p.jpg"
    Image.new("RGB", (900, 1400), (200, 120, 40)).save(img)
    clip = stock_service.photo_to_clip(img, tmp_path / "photo.mp4", 1280, 720, seconds=24)
    info = probe(clip)
    assert info["width"] == 1280 and info["height"] == 720 and abs(info["duration"] - 24) < 0.2
    vid_raw = tmp_path / "raw.mp4"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=30:duration=15",
                    "-f", "lavfi", "-i", "sine=f=300:d=15", "-shortest", "-c:v", "libx264", "-preset", "ultrafast", str(vid_raw)], check=True)
    vid = stock_service.trim_video(vid_raw, tmp_path / "video.mp4", 10)
    assert probe(vid)["has_audio"] is False and probe(vid)["duration"] <= 10.2
    voice = tmp_path / "voice.wav"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "sine=f=220:d=11", str(voice)], check=True)
    cues = _cues([3.0, 5.0, 3.0])
    media = {1: [{"name": "photo.mp4", "duration": 24.0, "kind": "photo"}],
             2: [{"name": "video.mp4", "duration": probe(vid)["duration"], "kind": "video"}],
             3: [{"name": "photo.mp4", "duration": 24.0, "kind": "photo"}]}
    st = styles.get("hist_archive")
    plan, scenes = broll_service.build_plan(cues, media, st)
    parsed, issues = validate_plan(plan, _ctx(cues, scenes, {"photo.mp4": 24.0, "video.mp4": probe(vid)["duration"]}))
    assert not issues
    out = tmp_path / "out.mp4"
    render_video(parsed, cues, {"photo.mp4": str(clip), "video.mp4": str(vid)}, str(voice), 11.0, out,
                 RenderOptions(quality="preview", aspect_ratio="16:9", burn_subtitles=False, hw_accel="off"), tmp_path / "work")
    o = probe(out)
    assert o["has_video"] and o["has_audio"] and abs(o["duration"] - 11.0) < 0.4
