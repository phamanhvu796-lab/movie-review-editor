from app.schemas import Cue, Footage, PlanContext, Scene

FILTERS = ["cinematic_teal_orange", "warm_film", "cold_thriller", "vibrant_vlog", "noir"]


def make_ctx(n=3, allow_spoilers=False) -> PlanContext:
    return PlanContext(
        movie_title="X", voice_duration=4.0 * n,
        srt_cues=[Cue(index=i + 1, start=4.0 * i, end=4.0 * (i + 1), text=f"c{i+1}") for i in range(n)],
        available_footage=[Footage(file="a.mp4", duration=10.0 * n,
                                   scenes=[Scene(start=10.0 * i, end=10.0 * (i + 1)) for i in range(n)])],
        available_filters=FILTERS, allow_spoilers=allow_spoilers,
    )


def make_plan(n=3) -> dict:
    return {
        "creative_direction": "test", "voice_language": "vi", "target_duration_seconds": 4 * n,
        "sections": [{
            "srt_index": i + 1, "voice_start": 4.0 * i, "voice_end": 4.0 * (i + 1),
            "visuals": [{"footage": "a.mp4", "start": 10.0 * i + 1, "end": 10.0 * i + 5}],
            "subtitle": f"c{i+1}", "purpose": "plot",
        } for i in range(n)],
    }
