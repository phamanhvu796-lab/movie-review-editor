from app.services.validator_service import validate_plan
from tests.helpers import FILTERS, make_ctx, make_plan


def codes(plan, ctx):
    return {i.code for i in validate_plan(plan, ctx)[1]}


def test_valid():
    plan, issues = validate_plan(make_plan(), make_ctx())
    assert plan is not None and issues == []


def test_out_of_scene():
    p = make_plan()
    p["sections"][0]["visuals"][0].update(start=8.0, end=12.0)   # vắt qua 2 scene
    assert "OUT_OF_SCENE" in codes(p, make_ctx())


def test_unknown_footage():
    p = make_plan()
    p["sections"][0]["visuals"][0]["footage"] = "zzz.mp4"
    assert "UNKNOWN_FOOTAGE" in codes(p, make_ctx())


def test_scene_reused_three_times():
    p = make_plan()
    for s in p["sections"]:
        s["visuals"][0].update(start=0.0, end=4.0)
    assert "SCENE_REUSED" in codes(p, make_ctx())


def test_too_many_filters():
    p = make_plan(4)
    for s, f in zip(p["sections"], FILTERS):
        s["visuals"][0]["filter"] = f
    assert "TOO_MANY_FILTERS" in codes(p, make_ctx(4))


def test_missing_and_duplicate_cues():
    p = make_plan()
    p["sections"].pop()
    assert "MISSING_CUES" in codes(p, make_ctx())
    p2 = make_plan()
    p2["sections"].append(p2["sections"][0])
    assert "DUPLICATE_CUES" in codes(p2, make_ctx())


def test_section_duration_mismatch_and_speed_compensation():
    p = make_plan()
    p["sections"][0]["visuals"][0]["end"] = 3.0                   # 2s vs cue 4s
    assert "SECTION_DURATION" in codes(p, make_ctx())
    p = make_plan()
    p["sections"][0]["visuals"][0].update(start=1.0, end=5.0, speed=2.0)  # 2s timeline
    assert "SECTION_DURATION" in codes(p, make_ctx())
    p = make_plan()
    p["sections"][0]["visuals"][0].update(start=1.0, end=9.0, speed=2.0)  # 8s src / 2 = 4s
    assert codes(p, make_ctx()) == set()


def test_schema_range_errors():
    p = make_plan()
    p["sections"][0]["visuals"][0]["zoom_end"] = 2.0
    plan, issues = validate_plan(p, make_ctx())
    assert plan is None and issues[0].code == "SCHEMA"


def test_spoiler_blocked():
    ctx = make_ctx()
    ctx.available_footage[0].scenes[0].spoiler = True
    assert "SPOILER" in codes(make_plan(), ctx)
    ctx.allow_spoilers = True
    assert "SPOILER" not in codes(make_plan(), ctx)
