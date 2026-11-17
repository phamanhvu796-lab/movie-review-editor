import base64
import logging
from concurrent.futures import ThreadPoolExecutor
import shutil
import tempfile
from pathlib import Path

from anthropic import Anthropic

from ..config import settings
from .claude_client import get_client
from .ffmpeg_service import extract_frame

log = logging.getLogger("mre.tagging")

TAG_TOOL = {
    "name": "tag_scenes",
    "description": "Gắn nhãn cho từng scene dựa trên ảnh keyframe.",
    "input_schema": {"type": "object", "properties": {"tags": {"type": "array", "items": {
        "type": "object",
        "properties": {
            "index": {"type": "integer"},
            "shot_type": {"type": "string", "enum": ["close_up", "medium", "wide", "insert", "other"]},
            "camera_motion": {"type": "string"},
            "description": {"type": "string", "description": "Tối đa 15 từ: ai/đang làm gì/cảm xúc"},
            "spoiler_risk": {"type": "boolean", "description": "true nếu cảnh lộ cái kết hoặc twist"},
        },
        "required": ["index", "shot_type", "description"]}}}, "required": ["tags"]},
}


def tag_scenes(path: str, scenes: list[dict], client: Anthropic | None = None,
               batch: int = 16, max_scenes: int = 200, workers: int = 3) -> list[dict]:
    """Gắn shot_type/description/spoiler bằng Claude vision (1 keyframe giữa mỗi scene).
    Dùng model nhanh/rẻ (claude_fast_model), trích frame và gọi API song song; lỗi một batch chỉ mất nhãn batch đó."""
    if client is None:
        if not settings.anthropic_api_key:
            return scenes
        client = get_client()
    idxs = list(range(len(scenes)))
    if len(idxs) > max_scenes:
        step = len(idxs) / max_scenes
        idxs = [int(i * step) for i in range(max_scenes)]

    tmp = Path(tempfile.mkdtemp(prefix="mre_tag_"))
    try:
        def grab(i):
            s = scenes[i]
            out = tmp / f"{i}.jpg"
            try:
                extract_frame(path, (s["start"] + s["end"]) / 2, out)
                return i, out
            except RuntimeError as ex:
                log.warning("Không trích được frame scene %s: %s", i, ex)
                return i, None

        with ThreadPoolExecutor(max_workers=4) as ex:
            frames = {i: o for i, o in ex.map(grab, idxs) if o}
        keys = sorted(frames)
        batches = [keys[b:b + batch] for b in range(0, len(keys), batch)]

        def tag_batch(part):
            content = []
            for i in part:
                data = base64.b64encode(frames[i].read_bytes()).decode()
                content += [{"type": "text", "text": f"Scene {i}:"},
                            {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": data}}]
            content.append({"type": "text", "text": "Gắn nhãn từng scene bằng tool tag_scenes (dùng đúng số scene)."})
            try:
                resp = client.messages.create(
                    model=settings.claude_fast_model, max_tokens=4000, tools=[TAG_TOOL],
                    tool_choice={"type": "tool", "name": "tag_scenes"}, messages=[{"role": "user", "content": content}])
                return next(x for x in resp.content if x.type == "tool_use").input.get("tags", [])
            except Exception as ex:
                log.warning("Tagging batch lỗi: %s", ex)
                return []

        with ThreadPoolExecutor(max_workers=workers) as ex:
            for tags in ex.map(tag_batch, batches):
                for t in tags:
                    i = t.get("index")
                    if i in frames:
                        scenes[i].update(shot_type=t.get("shot_type"), camera_motion=t.get("camera_motion"),
                                         description=t.get("description"), spoiler=bool(t.get("spoiler_risk")))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return scenes
