from app.services.repair_service import repair_plan
from app.services.validator_service import validate_plan
from tests.helpers import make_ctx, make_plan


def fixed(plan, ctx=None):
    ctx = ctx or make_ctx()
    repaired, notes = repair_plan(plan, ctx)
    return repaired, notes, validate_plan(repaired, ctx)[1]


def test_clamps_numbers_and_enums():
    p = make_plan()
    v = p["sections"][0]["visuals"][0]
    v.update(zoom_end=1.9, speed=3.0, transition_in="spin", filter="no_such_filter")
    r, notes, issues = fixed(p)
    v2 = r["sections"][0]["visuals"][0]
    assert v2["zoom_end"] == 1.5 and v2["transition_in"] == "none" and v2["filter"] is None and notes
    assert not [i for i in issues if i.code in ("SCHEMA", "UNKNOWN_FILTER")]


def test_clip_crossing_scene_boundary_is_pulled_into_scene():
    p = make_plan()
    p["sections"][0]["visuals"][0].update(start=8.0, end=12.0)
    _, _, issues = fixed(p)
    assert issues == []


def test_section_duration_is_stretched_to_cue():
    p = make_plan()
    p["sections"][0]["visuals"][0]["end"] = 3.0          # 2s so với cue 4s
    r, _, issues = fixed(p)
    assert issues == [] and r["sections"][0]["visuals"][0]["end"] == 5.0


def test_short_scene_falls_back_to_slow_motion():
    p = make_plan()
    ctx = make_ctx()
    ctx.available_footage[0].scenes[0].end = 3.0         # scene chỉ còn 3s: không kéo dài được
    p["sections"][0]["visuals"][0].update(start=0.0, end=3.0)
    r, _, issues = fixed(p, ctx)
    assert issues == [] and r["sections"][0]["visuals"][0]["speed"] == 0.75


def test_voice_times_and_subtitle_are_snapped_to_cue():
    p = make_plan()
    p["sections"][1].update(voice_start=4.3, voice_end=7.6, subtitle="sai chữ", purpose="???")
    r, _, issues = fixed(p)
    s = r["sections"][1]
    assert (s["voice_start"], s["voice_end"], s["subtitle"], s["purpose"]) == (4.0, 8.0, "c2", "plot") and issues == []


def test_duplicate_and_unordered_sections():
    p = make_plan()
    p["sections"] = [p["sections"][2], p["sections"][0], p["sections"][1], p["sections"][1]]
    r, _, issues = fixed(p)
    assert [s["srt_index"] for s in r["sections"]] == [1, 2, 3] and issues == []


def test_unfixable_errors_are_left_for_claude():
    p = make_plan()
    p["sections"].pop()
    p["sections"][0]["visuals"][0]["footage"] = "zzz.mp4"
    _, _, issues = fixed(p)
    assert {"MISSING_CUES", "UNKNOWN_FOOTAGE"} <= {i.code for i in issues}


def test_malformed_plan_is_returned_untouched():
    assert repair_plan({"foo": 1}, make_ctx()) == ({"foo": 1}, [])
