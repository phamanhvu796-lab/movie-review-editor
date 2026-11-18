import json
import shutil
from types import SimpleNamespace as NS

import pytest

from app.schemas import Cue, Footage, PlanContext, Scene
from app.services import broll_service, chat_service, stock_service, styles
from app.services.ffmpeg_service import probe
from app.services.render_service import PRESETS
from app.services.repair_service import repair_plan
from app.services.validator_service import validate_plan

CUES = [Cue(index=1, start=0, end=4.2, text="Năm 1288, quân Nguyên tiến vào sông Bạch Đằng."),
        Cue(index=2, start=4.2, end=9.8, text="Trần Hưng Đạo cho cắm cọc gỗ dưới lòng sông."),
        Cue(index=3, start=9.8, end=14.0, text="Khi nước rút, thuyền giặc mắc cạn và bị đánh tan.")]


def test_media_requests_merges_claude_output_and_falls_back():
    seen = []

    def create(**kw):
        seen.append(kw)
        return NS(content=[NS(type="tool_use", input={"intro": "Mình cần bạn vẽ 2 cảnh.", "items": [
            {"cue": 1, "what": "Đoàn thuyền chiến tiến vào cửa sông", "keywords": ["Bach Dang battle", "thuyền chiến"],
             "prompt": "Flat 2D illustration of Mongol war junks entering a river estuary, dawn mist --ar 16:9"}]})])
    items = [{"cue": c.index, "start": c.start, "end": c.end, "text": c.text, "need": "draw", "q": ["river battle"]} for c in CUES[:2]]
    st = {**styles.get("hist_anim"), "id": "hist_anim"}
    d = chat_service.media_requests(items, st, "Trận Bạch Đằng", client=NS(messages=NS(create=create)))
    assert d["intro"] == "Mình cần bạn vẽ 2 cảnh."
    assert d["items"][0]["prompt"].endswith("--ar 16:9") and d["items"][0]["keywords"][0] == "Bach Dang battle"
    assert d["items"][1]["what"] == CUES[1].text and d["items"][1]["keywords"] == ["river battle"] and d["items"][1]["prompt"] == ""
    assert "--ar 16:9" in seen[0]["system"] and "illustration" in seen[0]["system"]


def test_chat_op_updates_media_prompt():
    _, _, acts, notes = chat_service.apply_operations({"sections": []}, [
        {"op": "update_media_request", "srt_index": 2, "prompt": "Pixar-style 3D render of wooden stakes --ar 16:9"},
        {"op": "update_media_request", "srt_index": 3}], {})
    assert acts["media"] == [{"cue": 2, "prompt": "Pixar-style 3D render of wooden stakes --ar 16:9", "what": None}]
    assert notes == ["Cập nhật hình cần cho câu 2"]


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="cần ffmpeg")
def test_user_image_replaces_one_cue_and_plan_stays_valid(tmp_path):
    from PIL import Image
    st = styles.get("hist_anim")
    clips = {}
    for i, col in enumerate([(200, 80, 40), (40, 90, 200)]):
        img = tmp_path / f"s{i}.jpg"
        Image.new("RGB", (1600, 900), col).save(img)
        clips[f"stock{i}.mp4"] = stock_service.photo_to_clip(img, tmp_path / f"stock{i}.mp4", 1920, 1080, seconds=24)
    media = {c.index: [{"name": f"stock{c.index % 2}.mp4", "duration": 24.0, "kind": "photo"}] for c in CUES}
    plan, scenes = broll_service.build_plan(CUES, media, st, "vi")
    # người dùng vẽ ảnh dọc cho câu 2 -> chuẩn hoá 16:9 (nền mờ) -> thay riêng câu 2
    drawn = tmp_path / "ve_cau2.png"
    Image.new("RGB", (900, 1400), (230, 200, 60)).save(drawn)
    out = stock_service.photo_to_clip(drawn, tmp_path / "cau002.mp4", 1920, 1080)
    assert (probe(out)["width"], probe(out)["height"]) == (1920, 1080)
    one, sc2 = broll_service.build_plan([CUES[1]], {2: [{"name": "cau002.mp4", "duration": 24.0, "kind": "photo"}]}, st)
    new = broll_service.replace_cue_visuals(plan, 2, one["sections"][0]["visuals"])
    assert all(v["footage"] == "cau002.mp4" for v in new["sections"][1]["visuals"])
    assert new["sections"][0] == plan["sections"][0] and new["sections"][2] == plan["sections"][2]
    assert new["sections"][1]["visuals"][0].get("transition_in") == plan["sections"][1]["visuals"][0].get("transition_in")
    foot = [Footage(file=n, duration=24.0, scenes=[Scene(**x) for x in scenes[n]]) for n in scenes]
    foot.append(Footage(file="cau002.mp4", duration=24.0, scenes=[Scene(**x) for x in sc2["cau002.mp4"]]))
    ctx = PlanContext(movie_title="Bạch Đằng", voice_duration=14.0, srt_cues=CUES, language="vi", available_footage=foot,
                      available_filters=list(PRESETS))
    fixed, _ = repair_plan(new, ctx)
    _, issues = validate_plan(fixed, ctx)
    assert not issues, issues
    with pytest.raises(ValueError):
        broll_service.replace_cue_visuals(plan, 99, [])
    json.dumps(new)
