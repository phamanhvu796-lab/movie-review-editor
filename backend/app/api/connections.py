import uuid

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..config import clean_key, key_problem, settings, url_problem
from ..services import credit_service, deepseek, tts_service
from ..services.llm_sources import default_name, mask_conn, test_connection

router = APIRouter(prefix="/api")
SERVICES = ("llm", "deepseek", "elevenlabs", "openai_tts")
PROTO = {"llm": "auto", "deepseek": "anthropic", "elevenlabs": "elevenlabs", "openai_tts": "openai"}
LLM_LIKE = ("llm", "deepseek")           # DeepSeek: kết nối y như Claude (Messages API), dùng riêng để kiểm tra kịch bản


class ConnIn(BaseModel):
    """Mọi trường tuỳ chọn: trường không gửi (hoặc key để trống) = giữ giá trị đã lưu."""
    service: str | None = None           # llm (mặc định) | elevenlabs | openai_tts
    id: str | None = None
    name: str | None = None
    protocol: str | None = None          # chỉ cho llm: auto | anthropic | openai
    base_url: str | None = None
    user_agent: str | None = None        # nâng cao: để trống = giống trình duyệt Chrome
    api_key: str | None = None
    model: str | None = None
    fast_model: str | None = None
    enabled: bool | None = None
    budget_usd: float | None = None      # hạn mức tự đặt (USD) để app tự trừ chi phí ước tính
    force: bool = False                  # True = lưu luôn, không kiểm tra
    set_active: bool | None = None       # mặc định: nguồn mới thì đặt làm đang dùng
    preset: str | None = None            # "deepseek": tự điền Base URL / kiểu API / model của DeepSeek


FIELDS = ("name", "protocol", "base_url", "user_agent", "api_key", "model", "fast_model", "enabled", "budget_usd")


def _svc(s: str | None) -> str:
    s = s or "llm"
    if s not in SERVICES:
        raise HTTPException(400, f"Dịch vụ không hợp lệ: {s}")
    return s


def _apply_preset(svc: str, lst: list[dict], body: ConnIn) -> None:
    if svc == "deepseek":
        body.preset = "deepseek"
    if body.preset != "deepseek" or svc not in LLM_LIKE:
        return
    for k, v in deepseek.preset().items():
        if getattr(body, k) in (None, ""):
            setattr(body, k, v)
    if not body.id:                      # đã có nguồn DeepSeek -> cập nhật key cho nguồn đó, không tạo trùng
        body.id = next((c["id"] for c in lst if deepseek.is_deepseek(c)), None)


def _merge(svc: str, lst: list[dict], body: ConnIn) -> tuple[dict, dict | None]:
    _apply_preset(svc, lst, body)
    cur = next((c for c in lst if c["id"] == body.id), None) if body.id else None
    merged = dict(cur) if cur else {"id": body.id or uuid.uuid4().hex[:8], "service": svc, "name": "", "protocol": PROTO[svc],
                                    "base_url": "", "user_agent": "", "api_key": "", "model": "", "fast_model": "", "enabled": True}
    for k in FIELDS:
        v = getattr(body, k)
        if v is None or (k == "api_key" and v == ""):
            continue
        merged[k] = v.strip() if isinstance(v, str) else v
    merged["api_key"] = clean_key(merged["api_key"])
    merged["service"] = svc
    if svc not in LLM_LIKE:
        merged["protocol"] = PROTO[svc]
    return merged, cur


def _listing_one(svc: str):
    active = settings.active_connection(svc)
    return {"connections": [mask_conn(c) for c in settings.connections(svc)],
            "active": settings.active_connection_id(svc) or (active or {}).get("id", "")}


def _listing():
    return {s: _listing_one(s) for s in SERVICES}


def _test(conn: dict) -> dict:
    if not conn.get("api_key"):
        return {"ok": False, "detail": "Chưa có API key", "models": []}
    return test_connection(conn) if conn["service"] in LLM_LIKE else {**tts_service.test_tts_source(conn), "models": []}


@router.get("/connections")
def list_connections():
    return _listing()


@router.post("/connections/test")
def test(body: ConnIn):
    svc = _svc(body.service)
    merged, _ = _merge(svc, [dict(c) for c in settings.connections(svc)], body)
    return _test(merged)


@router.post("/connections")
def save(body: ConnIn):
    svc = _svc(body.service)
    lst = [dict(c) for c in settings.connections(svc)]
    merged, cur = _merge(svc, lst, body)
    if not merged["api_key"]:
        raise HTTPException(400, "Cần nhập API key")
    bad = key_problem(merged["api_key"]) or url_problem(merged.get("base_url"))
    if bad:                              # chặn cả khi lưu không kiểm tra (force)
        return {"ok": False, "detail": bad, "models": []}
    if any(ord(c) < 32 or ord(c) > 126 for c in merged.get("user_agent", "")):
        return {"ok": False, "detail": "User-Agent chỉ được gồm ký tự ASCII (không dấu, không xuống dòng).", "models": []}
    res = {}
    if not body.force:
        res = _test(merged)
        if not res["ok"]:
            return {"ok": False, "detail": res["detail"], "models": [], "trace": res.get("trace", [])}
        if svc in LLM_LIKE:
            merged["protocol"] = res["protocol"]
            merged["model"] = merged.get("model") or res["model"]
    elif svc in LLM_LIKE and merged["protocol"] == "auto":
        merged["protocol"] = "anthropic"
    if not merged["name"]:
        merged["name"] = default_name(merged) if merged.get("base_url") else {"llm": "Claude", "deepseek": "DeepSeek", "elevenlabs": "ElevenLabs", "openai_tts": "OpenAI TTS"}[svc]
    if cur is not None:
        lst[lst.index(cur)] = merged
    else:
        lst.append(merged)
    active = settings.active_connection_id(svc)
    make_active = body.set_active if body.set_active is not None else cur is None
    if make_active or not any(c["id"] == active for c in lst):
        active = merged["id"]
    settings.save_connections(lst, active, svc)
    credit_service.invalidate()
    return {"ok": True, "detail": res.get("detail", "Đã lưu"), "models": res.get("models", []), **_listing()}


@router.get("/connections/presets")
def presets():
    return {"deepseek": deepseek.preset()}


@router.put("/connections/active")
def set_active(body: ConnIn):
    svc = _svc(body.service)
    if not any(c["id"] == body.id for c in settings.connections(svc)):
        raise HTTPException(404, "Không tìm thấy nguồn")
    settings.save_connections([dict(c) for c in settings.connections(svc)], body.id, svc)
    return _listing()


@router.delete("/connections/{cid}")
def delete(cid: str, service: str = "llm"):
    svc = _svc(service)
    lst = [dict(c) for c in settings.connections(svc) if c["id"] != cid]
    active = settings.active_connection_id(svc)
    if active == cid or not any(c["id"] == active for c in lst):
        active = next((c["id"] for c in lst if c.get("enabled", True)), "")
    settings.save_connections(lst, active, svc)
    return _listing()
