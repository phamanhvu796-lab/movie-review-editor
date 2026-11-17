"""Tín dụng / số dư còn lại của từng nguồn API.

- Relay OpenAI-compatible kiểu one-api / new-api / phần lớn relay Việt: /dashboard/billing/subscription + /usage (USD).
- new-api đời mới: /api/usage/token (đơn vị quota, 500 000 = 1 USD).
- OpenRouter: /api/v1/credits.
- Anthropic chính thức: KHÔNG có API xem số dư -> dùng hạn mức tự đặt (budget_usd) trừ đi chi phí app tự đếm.
- ElevenLabs: số ký tự còn lại của gói (cộng dồn các key).
Kết quả được nhớ 2 phút để không gọi liên tục.
"""
import json
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from urllib.parse import urlparse

from ..config import DEFAULT_UA, normalize_base_url, settings
from . import tts_service, usage_service

TTL = 120
UNLIMITED = 1e7               # new-api trả hard_limit cực lớn khi token "không giới hạn"
_cache: dict = {}
_lock = threading.Lock()


def _get(url: str, key: str, ua: str | None, timeout: float = 8) -> dict:
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {key}", "User-Agent": ua or DEFAULT_UA,
                                               "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def _roots(conn: dict) -> list[str]:
    """Các gốc URL để thử: https://host và https://host/v1 (relay đặt API ở /v1)."""
    raw = (conn.get("base_url") or "").strip()
    if not raw:
        return []
    u = normalize_base_url(raw, "openai")             # luôn kết thúc bằng /v1
    p = urlparse(u)
    host = f"{p.scheme}://{p.netloc}"
    out = [u, host]
    return list(dict.fromkeys(out))


def _billing(conn: dict) -> dict | None:
    key, ua = conn["api_key"], conn.get("user_agent")
    for root in _roots(conn):
        try:
            sub = _get(f"{root}/dashboard/billing/subscription", key, ua)
            limit = float(sub.get("hard_limit_usd") or sub.get("system_hard_limit_usd") or 0)
            start = (date.today() - timedelta(days=99)).isoformat()
            end = (date.today() + timedelta(days=1)).isoformat()
            try:
                used = float(_get(f"{root}/dashboard/billing/usage?start_date={start}&end_date={end}", key, ua)
                             .get("total_usage") or 0) / 100
            except Exception:
                used = None
            if limit <= 0 and used is None:
                continue
            if limit >= UNLIMITED:
                return {"unlimited": True, "used_usd": used, "source": "relay"}
            rem = max(limit - (used or 0), 0) if used is not None else None
            return {"remaining_usd": rem, "limit_usd": limit, "used_usd": used, "source": "relay"}
        except Exception:
            continue
    host = urlparse(_roots(conn)[-1]).netloc if _roots(conn) else ""
    if host:
        try:
            d = _get(f"https://{host}/api/usage/token", key, ua).get("data") or {}
            if d.get("unlimited_quota"):
                return {"unlimited": True, "used_usd": float(d.get("total_used") or 0) / 500000, "source": "relay"}
            if "total_available" in d:
                return {"remaining_usd": float(d["total_available"]) / 500000,
                        "limit_usd": float(d.get("total_granted") or 0) / 500000 or None,
                        "used_usd": float(d.get("total_used") or 0) / 500000, "source": "relay", "approx": True}
        except Exception:
            pass
    return None


def _openrouter(conn: dict) -> dict | None:
    try:
        d = _get("https://openrouter.ai/api/v1/credits", conn["api_key"], conn.get("user_agent")).get("data") or {}
        total, used = float(d.get("total_credits") or 0), float(d.get("total_usage") or 0)
        return {"remaining_usd": max(total - used, 0), "limit_usd": total, "used_usd": used, "source": "openrouter"}
    except Exception:
        return None


def llm_credit(conn: dict) -> dict:
    """Số dư của một nguồn Claude/LLM + mức đã dùng do app tự đếm + hạn mức tự đặt."""
    usage = usage_service.get(conn.get("id", ""))
    out = {"id": conn.get("id"), "name": conn.get("name", ""), "usage": usage, "balance": None, "note": ""}
    host = urlparse((conn.get("base_url") or "").strip() if "://" in (conn.get("base_url") or "")
                    else "https://" + (conn.get("base_url") or "").strip()).netloc.lower()
    if not conn.get("api_key"):
        out["note"] = "Chưa có key"
    elif not host or host.endswith("anthropic.com"):
        out["note"] = "Anthropic không cho xem số dư qua API — xem ở console.anthropic.com, hoặc đặt hạn mức để app tự trừ."
    elif "openrouter.ai" in host:
        out["balance"] = _openrouter(conn)
    else:
        out["balance"] = _billing(conn)
        if out["balance"] is None:
            out["note"] = "Relay này không công bố số dư qua API — có thể đặt hạn mức để app tự trừ."
    budget = conn.get("budget_usd")
    try:
        budget = float(budget) if budget not in (None, "") else None
    except (TypeError, ValueError):
        budget = None
    if budget:
        out["budget"] = {"limit_usd": budget, "remaining_usd": round(max(budget - usage["usd"], 0), 4)}
    return out


def _tts_eleven(conn: dict) -> dict:
    out = {"id": conn.get("id"), "name": conn.get("name", ""), "chars": None, "note": ""}
    try:
        out["chars"] = tts_service._quota_of(conn)                 # noqa: SLF001 (dùng lại hàm sẵn có)
    except Exception as e:
        out["note"] = str(e)[:160]
    return out


def _tts_openai(conn: dict) -> dict:
    bal = _billing(conn) if (conn.get("base_url") or "").strip() and "api.openai.com" not in conn.get("base_url", "") else None
    return {"id": conn.get("id"), "name": conn.get("name", ""), "balance": bal,
            "note": "" if bal else "Không đọc được số dư qua API (OpenAI chính thức không hỗ trợ) — xem ở trang quản lý của nhà cung cấp."}


def summary(refresh: bool = False) -> dict:
    with _lock:
        hit = _cache.get("all")
        if hit and not refresh and time.time() - hit[0] < TTL:
            return hit[1]
    llm = [c for c in settings.connections("llm") if c.get("enabled", True)]
    el = [c for c in settings.connections("elevenlabs") if c.get("enabled", True) and c.get("api_key")]
    oa = [c for c in settings.connections("openai_tts") if c.get("enabled", True) and c.get("api_key")]
    jobs = [(llm_credit, c) for c in llm] + [(_tts_eleven, c) for c in el] + [(_tts_openai, c) for c in oa]
    with ThreadPoolExecutor(max_workers=max(1, min(6, len(jobs)))) as ex:
        res = list(ex.map(lambda j: j[0](j[1]), jobs))
    out = {"llm": res[:len(llm)], "elevenlabs": res[len(llm):len(llm) + len(el)], "openai_tts": res[len(llm) + len(el):],
           "active_llm": settings.active_connection_id("llm") or (settings.active_connection("llm") or {}).get("id", ""),
           "checked_at": time.time()}
    chars = [x["chars"] for x in out["elevenlabs"] if x.get("chars")]
    if chars:
        out["elevenlabs_total"] = {"remaining": sum(c["remaining"] for c in chars), "limit": sum(c["limit"] for c in chars)}
    with _lock:
        _cache["all"] = (time.time(), out)
    return out


def invalidate():
    with _lock:
        _cache.pop("all", None)
