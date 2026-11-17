import importlib.util
import uuid
from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel

from ..config import clean_key, settings
from ..version import __version__
from ..services import tts_service
from ..services.api_errors import friendly
from ..services.llm_sources import default_name, mask_conn, test_connection
from ..services.claude_client import get_client
from ..services.ffmpeg_service import detect_hw_encoder
from ..utils.paths import find_binary

router = APIRouter(prefix="/api")


class SettingsIn(BaseModel):
    anthropic_api_key: str | None = None
    claude_model: str | None = None
    whisper_model: str | None = None
    elevenlabs_api_key: str | None = None
    openai_api_key: str | None = None
    openai_tts_model: str | None = None
    anthropic_base_url: str | None = None
    elevenlabs_base_url: str | None = None
    openai_base_url: str | None = None


def _status():
    key = settings.anthropic_api_key
    return {
        "anthropic_base_url": settings.base_url_raw("anthropic"), "elevenlabs_base_url": settings.base_url_raw("elevenlabs"),
        "openai_base_url": settings.base_url_raw("openai"),
        "connections": [mask_conn(c) for c in settings.connections()],
        "active_connection": (settings.active_connection() or {}).get("id", ""),
        "has_api_key": bool(key), "api_key_hint": f"…{key[-4:]}" if len(key) > 8 else "",
        "claude_model": settings.claude_model, "whisper_model": settings.whisper_model,
        "has_elevenlabs_key": bool(settings.elevenlabs_api_key), "has_openai_key": bool(settings.openai_api_key),
        "openai_tts_model": settings.openai_tts_model,
        "hw_encoder": detect_hw_encoder(),
        "ffmpeg": bool(find_binary("ffmpeg")), "ffprobe": bool(find_binary("ffprobe")),
        "whisper_available": importlib.util.find_spec("faster_whisper") is not None,
        "scenedetect_available": importlib.util.find_spec("scenedetect") is not None,
    }


@router.get("/settings")
def get_settings():
    return _status()


@router.put("/settings")
def put_settings(body: SettingsIn):
    data = {k: (v.strip() if isinstance(v, str) else v) for k, v in body.model_dump().items() if v is not None}
    settings.update(**{k: v for k, v in data.items() if v or k.endswith("_base_url")})   # Base URL rỗng = về mặc định
    return _status()


class TestIn(BaseModel):
    service: Literal["anthropic", "elevenlabs", "openai"]


@router.post("/settings/test")
def test_service(body: TestIn):
    """Kiểm tra key thật sự dùng được (1 request rất nhỏ) và cho biết quota còn lại."""
    try:
        if body.service == "anthropic":
            if not settings.anthropic_api_key:
                return {"ok": False, "detail": "Chưa nhập key"}
            get_client().messages.create(model=settings.claude_model, max_tokens=8,
                                         messages=[{"role": "user", "content": "ping"}])
            return {"ok": True, "detail": "Kết nối Claude hoạt động"}
        if body.service == "elevenlabs":
            q = tts_service.get_quota()
            return {"ok": True, "detail": f"Key hợp lệ, còn {q['remaining']:,} / {q['limit']:,} ký tự"}
        tts_service.check_openai()
        return {"ok": True, "detail": "Key hợp lệ"}
    except Exception as e:
        return {"ok": False, "detail": friendly(e)}


class ConnectIn(BaseModel):
    protocol: Literal["auto", "anthropic", "openai"] = "auto"
    base_url: str = ""
    api_key: str | None = None
    model: str = ""
    name: str = ""
    elevenlabs_api_key: str | None = None
    elevenlabs_base_url: str | None = None
    openai_api_key: str | None = None
    openai_base_url: str | None = None


@router.post("/connect")
def connect(body: ConnectIn):
    """Kết nối lần đầu: thử nguồn Claude (tự nhận diện kiểu API, tự chọn model), lưu nếu chạy được;
    ElevenLabs/OpenAI (giọng đọc) là tuỳ chọn."""
    results = {}
    if body.api_key and body.api_key.strip():
        conn = {"id": uuid.uuid4().hex[:8], "protocol": body.protocol, "base_url": body.base_url.strip(),
                "api_key": clean_key(body.api_key), "model": body.model.strip(), "fast_model": "", "enabled": True}
        conn["name"] = body.name.strip() or default_name(conn)
        res = test_connection(conn)
        results["anthropic"] = res
        if res["ok"]:
            conn["protocol"], conn["model"] = res["protocol"], conn["model"] or res["model"]
            settings.save_connections([dict(c) for c in settings.connections()] + [conn], conn["id"])
    elif settings.anthropic_api_key:
        results["anthropic"] = test_service(TestIn(service="anthropic"))
    else:
        results["anthropic"] = {"ok": False, "detail": "Chưa nhập API key"}
    for svc, label, key, base, nice in (
            ("elevenlabs", "elevenlabs", body.elevenlabs_api_key, body.elevenlabs_base_url, "ElevenLabs"),
            ("openai_tts", "openai", body.openai_api_key, body.openai_base_url, "OpenAI TTS")):
        if key and key.strip():
            conn = {"id": uuid.uuid4().hex[:8], "service": svc, "protocol": label, "base_url": (base or "").strip(),
                    "api_key": clean_key(key), "model": "", "fast_model": "", "enabled": True}
            conn["name"] = default_name(conn) if conn["base_url"] else nice
            res = tts_service.test_tts_source(conn)
            results[label] = res
            if res["ok"]:
                cur = [dict(c) for c in settings.connections(svc)]
                settings.save_connections(cur + [conn], settings.active_connection_id(svc) or conn["id"], svc)
    return {"ok": results["anthropic"]["ok"], "results": results, "status": _status(),
            "models": results["anthropic"].get("models", [])}


@router.get("/health")
def health():
    return {"ok": True, "app": "MovieReviewEditor", "version": __version__}
