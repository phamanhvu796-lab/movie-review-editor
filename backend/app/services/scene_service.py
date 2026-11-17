import logging

from .. import config as C

log = logging.getLogger("mre.scenes")


def detect_scenes(path: str, duration: float = 0.0, window: float = 6.0) -> list[dict]:
    """PySceneDetect (ContentDetector). Nếu lỗi/không cắt được thì chia cửa sổ cố định.
    Scene ngắn hơn clip tối thiểu (1s) bị loại vì không thể dùng."""
    scenes: list[dict] = []
    try:
        from scenedetect import ContentDetector, detect
        for s, e in detect(str(path), ContentDetector(threshold=27.0)):
            scenes.append({"start": round(s.get_seconds(), 3), "end": round(e.get_seconds(), 3)})
    except Exception as ex:   # thiếu thư viện hoặc codec lạ
        log.warning("Scene detection lỗi (%s), dùng cửa sổ cố định", ex)
    if not scenes and duration:
        t = 0.0
        while t < duration:
            scenes.append({"start": round(t, 3), "end": round(min(t + window, duration), 3)})
            t += window
    return [s for s in scenes if s["end"] - s["start"] >= C.VISUAL_DURATION_RANGE[0]]
