"""Chạy THẬT bằng FFmpeg hai luồng "⚡ Tự động" mà người dùng hay làm:
A) Kịch bản → Video: SRT + voice MP3 (không footage) -> tìm hình (giả lập kho) -> lắp bản dựng -> xuất 1080p có phụ đề.
B) Review phim: footage + voice MP3 + SRT -> phát hiện cảnh -> Claude (giả lập) lên bản dựng -> xuất 1080p có logo + nhạc.
Phần AI/Internet được giả lập; phần xử lý video, phụ đề, âm thanh chạy thật."""
import shutil
import subprocess
import time
from types import SimpleNamespace as NS

import pytest

from app.schemas import Footage, PlanContext, Scene
from app.services import broll_service, stock_service, styles
from app.services.ffmpeg_service import probe
from app.services.render_service import PRESETS, RenderOptions, render_video
from app.services.repair_service import repair_plan
from app.services.srt_service import parse_srt
from app.services.validator_service import validate_plan

pytestmark = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="cần ffmpeg")

SRT = """1
00:00:00,000 --> 00:00:03,200
Lạm phát là khi giá cả tăng đều theo thời gian.

2
00:00:03,200 --> 00:00:07,900
Cùng một số tiền, bạn mua được ít hàng hơn trước.

3
00:00:07,900 --> 00:00:11,500
Ngân hàng trung ương dùng lãi suất để kìm lạm phát.

4
00:00:11,500 --> 00:00:16,800
Lãi suất cao khiến vay tiền đắt hơn, chi tiêu chậm lại.

5
00:00:16,800 --> 00:00:20,000
Vì vậy, hãy giữ tiền tiết kiệm ở nơi sinh lời tốt hơn.
"""
VOICE_SECONDS = 20.0


def _ff(*args):
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", *args], check=True)


def _mp3(path, seconds):
    _ff("-f", "lavfi", "-i", f"sine=f=220:d={seconds}", "-c:a", "libmp3lame", "-b:a", "128k", str(path))
    return path


def _frame_luma(video, t, tmp):
    """Độ sáng trung bình 1/5 dưới khung hình (vùng phụ đề) ở giây t."""
    from PIL import Image, ImageStat
    png = tmp / f"f{t}.png"
    _ff("-ss", str(t), "-i", str(video), "-frames:v", "1", str(png))
    im = Image.open(png).convert("L")
    w, h = im.size
    return ImageStat.Stat(im.crop((0, int(h * 0.8), w, h))).stddev[0], im.size


def test_script_to_video_srt_plus_mp3_to_final(tmp_path, monkeypatch):
    monkeypatch.setenv("MRE_DATA_DIR", str(tmp_path / "data"))
    from PIL import Image
    res = parse_srt(SRT)
    cues = res.cues
    assert len(cues) == 5 and not [i for i in res.issues if getattr(i, "severity", "") == "error"]
    voice = _mp3(tmp_path / "giong_doc.mp3", VOICE_SECONDS)
    assert abs(probe(voice)["duration"] - VOICE_SECONDS) < 0.2

    # "Kho ảnh/video" giả lập: ảnh dọc + ngang (khác tỉ lệ -> nền mờ) và 1 video stock có tiếng (phải bị bỏ tiếng)
    clips = {}
    for i, size in enumerate([(900, 1400), (1600, 900), (1200, 1200)]):
        img = tmp_path / f"anh{i}.jpg"
        Image.new("RGB", size, (60 + 60 * i, 120, 200 - 50 * i)).save(img)
        clips[f"photo{i}.mp4"] = stock_service.photo_to_clip(img, tmp_path / f"photo{i}.mp4", 1920, 1080, seconds=24)
    raw = tmp_path / "stock_raw.mp4"
    _ff("-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=30:duration=12", "-f", "lavfi", "-i", "sine=f=500:d=12",
        "-shortest", "-c:v", "libx264", "-preset", "ultrafast", str(raw))
    clips["stock.mp4"] = stock_service.trim_video(raw, tmp_path / "stock.mp4", 10)
    dur = {k: probe(v)["duration"] for k, v in clips.items()}
    assert not probe(clips["stock.mp4"])["has_audio"]

    names = list(clips)
    media = {c.index: [{"name": names[k % 4], "duration": dur[names[k % 4]], "kind": "video" if names[k % 4] == "stock.mp4" else "photo"},
                       {"name": names[(k + 1) % 4], "duration": dur[names[(k + 1) % 4]], "kind": "photo"}]
             for k, c in enumerate(cues)}
    st = styles.get("fin_faceless")
    plan, scenes = broll_service.build_plan(cues, media, st, "vi", None)
    ctx = PlanContext(movie_title="Lạm phát", voice_duration=VOICE_SECONDS, srt_cues=cues, language="vi",
                      available_footage=[Footage(file=n, duration=dur[n], scenes=[Scene(**s) for s in scenes.get(n, [])])
                                         for n in names if scenes.get(n)],
                      available_filters=list(PRESETS))
    plan, _ = repair_plan(plan, ctx)
    parsed, issues = validate_plan(plan, ctx)
    assert not issues, issues

    out = tmp_path / "out" / "thanh_pham.mp4"
    out.parent.mkdir()
    t0 = time.time()
    render_video(parsed, cues, {n: str(p) for n, p in clips.items()}, str(voice), VOICE_SECONDS, out,
                 RenderOptions(quality="final", aspect_ratio="16:9", burn_subtitles=True, subtitle_size=st["subtitle_size"],
                               hw_accel="auto"), tmp_path / "work", cache_dir=tmp_path / "cache")
    took = time.time() - t0
    o = probe(out)
    assert o["has_video"] and o["has_audio"] and (o["width"], o["height"]) == (1920, 1080)
    assert abs(o["duration"] - VOICE_SECONDS) < 0.5
    sub_var, _ = _frame_luma(out, 1.5, tmp_path)           # có chữ phụ đề -> vùng dưới có độ tương phản
    print(f"\n[A] Kịch bản→Video: {len(cues)} câu, {o['duration']:.1f}s 1080p, dựng mất {took:.1f}s, độ tương phản vùng phụ đề {sub_var:.1f}")
    assert sub_var > 8


def _plan_from_scenes(cues, scenes, footage_name):
    """Giả lập Claude: mỗi câu dùng 1-2 cảnh khác nhau, chưa cảnh nào dùng quá 2 lần."""
    secs, k = [], 0
    for c in cues:
        d = c.end - c.start
        parts = 2 if d > 4 else 1
        vis = []
        for j in range(parts):
            s = scenes[k % len(scenes)]
            k += 1
            ln = d / parts
            vis.append({"footage": footage_name, "start": round(s.start + 0.2, 2), "end": round(s.start + 0.2 + ln, 2),
                        "filter": "cinematic_teal_orange"})
        secs.append({"srt_index": c.index, "voice_start": c.start, "voice_end": c.end, "visuals": vis, "purpose": "plot"})
    return {"creative_direction": "nhịp vừa, màu điện ảnh", "voice_language": "vi", "target_duration_seconds": int(cues[-1].end),
            "sections": secs}


def test_movie_review_footage_mp3_srt_to_final(tmp_path, monkeypatch):
    monkeypatch.setenv("MRE_DATA_DIR", str(tmp_path / "data"))
    from PIL import Image
    from app.services.claude_service import TOOL_NAME, generate_edit_plan
    from app.services.scene_service import detect_scenes
    # Footage 60s gồm 6 cảnh màu khác hẳn nhau (để dò cảnh tìm được)
    parts = []
    for i, col in enumerate(["red", "green", "blue", "yellow", "purple", "white"]):
        f = tmp_path / f"s{i}.mp4"
        _ff("-f", "lavfi", "-i", f"color=c={col}:size=1280x720:rate=30:d=10", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
            "-shortest", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", str(f))
        parts.append(f)
    lst = tmp_path / "l.txt"
    lst.write_text("".join(f"file '{p}'\n" for p in parts))
    film = tmp_path / "phim.mp4"
    _ff("-f", "concat", "-safe", "0", "-i", str(lst), "-c", "copy", str(film))
    fdur = probe(film)["duration"]
    raw_scenes = detect_scenes(str(film), fdur)
    assert len(raw_scenes) >= 5, raw_scenes
    scenes = [Scene(start=s["start"], end=s["end"]) for s in raw_scenes]

    cues = parse_srt(SRT).cues
    voice = _mp3(tmp_path / "voice.mp3", VOICE_SECONDS)
    music = _mp3(tmp_path / "nhac_nen.mp3", 30)
    logo = tmp_path / "logo.png"
    Image.new("RGBA", (300, 120), (255, 255, 255, 200)).save(logo)

    ctx = PlanContext(movie_title="Phim thử", voice_duration=VOICE_SECONDS, srt_cues=cues, language="vi",
                      available_footage=[Footage(file="phim.mp4", duration=fdur, scenes=scenes)], available_filters=list(PRESETS))
    plan = _plan_from_scenes(cues, scenes, "phim.mp4")
    client = NS(messages=NS(create=lambda **kw: NS(stop_reason="tool_use",
                                                   content=[NS(type="tool_use", name=TOOL_NAME, id="t1", input=plan)])))
    res = generate_edit_plan(ctx, client=client)
    assert res.status == "ok", res.issues

    out = tmp_path / "review.mp4"
    t0 = time.time()
    render_video(res.plan, cues, {"phim.mp4": str(film)}, str(voice), VOICE_SECONDS, out,
                 RenderOptions(quality="final", aspect_ratio="16:9", burn_subtitles=True, music_volume=0.2, music_ducking=True,
                               hw_accel="auto"), tmp_path / "work", music_path=str(music), logo_path=str(logo),
                 cache_dir=tmp_path / "cache")
    took = time.time() - t0
    o = probe(out)
    assert o["has_video"] and o["has_audio"] and (o["width"], o["height"]) == (1920, 1080)
    assert abs(o["duration"] - VOICE_SECONDS) < 0.5
    print(f"\n[B] Review phim: {len(raw_scenes)} cảnh dò được, {o['duration']:.1f}s 1080p + logo + nhạc, dựng mất {took:.1f}s")

    # Bản thứ hai (giống "sửa ngầm" sau chat): dùng lại cache đoạn -> nhanh hơn
    t1 = time.time()
    render_video(res.plan, cues, {"phim.mp4": str(film)}, str(voice), VOICE_SECONDS, tmp_path / "review2.mp4",
                 RenderOptions(quality="final", aspect_ratio="16:9", burn_subtitles=True, hw_accel="auto"),
                 tmp_path / "work2", music_path=str(music), logo_path=str(logo), cache_dir=tmp_path / "cache")
    print(f"[B] Dựng lại (có cache): {time.time() - t1:.1f}s")
