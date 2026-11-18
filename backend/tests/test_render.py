from app.schemas import EditPlan
from app.schemas import Cue
from app.services.render_service import plan_segments, segment_filter, RenderOptions, subtitle_ass
from tests.helpers import make_plan


def test_segments_sum_to_total_frames():
    segs, total = plan_segments(EditPlan.model_validate(make_plan(3)), 12.0, 30)
    assert total == 360 and sum(s.frames for s in segs) == 360


def test_gap_between_cues_is_absorbed_without_drift():
    p = make_plan(3)
    p["sections"][1]["voice_start"] = 5.0           # khoảng lặng 1s sau cue 1
    segs, total = plan_segments(EditPlan.model_validate(p), 12.0, 30)
    assert segs[0].frames == 150 and sum(s.frames for s in segs) == total == 360


def test_voice_longer_than_cues_extends_last_section():
    segs, total = plan_segments(EditPlan.model_validate(make_plan(2)), 9.0, 30)
    assert total == 270


def test_segment_filter_features():
    p = EditPlan.model_validate(make_plan(1))
    v = p.sections[0].visuals[0].model_copy(update={"zoom_start": 1.0, "zoom_end": 1.3, "speed": 1.5,
                                                    "filter": "noir", "transition_in": "flash_cut"})
    vf = segment_filter(v, 1280, 720, 30, 90)
    assert "zoompan" in vf and "setpts=PTS/1.5000" in vf and "hue=s=0" in vf and "color=white" in vf
    assert "zoompan" not in segment_filter(p.sections[0].visuals[0], 1280, 720, 30, 90)


def test_subtitle_ass_uses_real_pixel_canvas_and_margins():
    o = RenderOptions(subtitle_size=54)
    cues = [Cue(index=1, start=0, end=2.5, text="Dòng 1\nDòng {2}"), Cue(index=2, start=3725.5, end=3727, text="  ")]
    h = subtitle_ass(o, 1920, 1080, cues)
    assert "PlayResX: 1920" in h and "PlayResY: 1080" in h
    assert "Style: Default,Arial,54," in h and h.split("Style: Default")[1].split("\n")[0].endswith(",60,60,70,1")
    assert "Dialogue: 0,0:00:00.00,0:00:02.50,Default,,0,0,0,,Dòng 1\\NDòng \\{2\\}" in h
    assert h.count("Dialogue:") == 1                       # câu rỗng bị bỏ
    v = subtitle_ass(o, 1080, 1920, cues)
    assert "PlayResY: 1920" in v and v.split("Style: Default")[1].split("\n")[0].endswith(",260,1")


def test_segment_key_depends_on_content_and_params(tmp_path):
    from app.services.render_service import _segment_key
    src = tmp_path / "a.mp4"
    src.write_bytes(b"x")
    v = EditPlan.model_validate(make_plan(1)).sections[0].visuals[0]
    k = _segment_key(str(src), v, 1280, 720, 30, 120, "veryfast", "23", None)
    assert k == _segment_key(str(src), v, 1280, 720, 30, 120, "veryfast", "23", None)
    assert k != _segment_key(str(src), v.model_copy(update={"zoom_end": 1.2}), 1280, 720, 30, 120, "veryfast", "23", None)
    assert k != _segment_key(str(src), v, 1920, 1080, 30, 120, "veryfast", "23", None)
    assert k != _segment_key(str(src), v, 1280, 720, 30, 120, "veryfast", "23", "lutsig")


def test_prune_cache_removes_oldest_first(tmp_path):
    import os
    from app.services.render_service import prune_cache
    for i in range(3):
        f = tmp_path / f"{i}.mp4"
        f.write_bytes(b"x" * 100)
        os.utime(f, (1000 + i, 1000 + i))
    prune_cache(tmp_path, max_bytes=150)
    assert [p.name for p in tmp_path.glob("*.mp4")] == ["2.mp4"]
