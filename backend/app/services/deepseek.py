"""DeepSeek: kết nối giống Claude (dán API key là chạy) qua cổng tương thích Anthropic của DeepSeek.

- Base URL: https://api.deepseek.com/anthropic (dùng thẳng Anthropic SDK).
- Model chính deepseek-v4-pro (không nhận ảnh), model nhanh deepseek-flash (nhận ảnh).
- tool_choice bắt buộc gọi công cụ chỉ chạy ở chế độ không suy nghĩ -> luôn gửi thinking=disabled.
- Số dư: GET https://api.deepseek.com/user/balance (USD hoặc CNY).
"""
import json
import urllib.request
from datetime import datetime, timezone
from urllib.parse import urlparse

BASE_URL = "https://api.deepseek.com/anthropic"
MODEL = "deepseek-v4-pro"
FAST_MODEL = "deepseek-flash"
NAME = "DeepSeek"
BALANCE_URL = "https://api.deepseek.com/user/balance"
NO_IMAGE = ("v4-pro", "reasoner")

# USD / 1 triệu token, giờ thấp điểm: (vào chưa cache, vào trúng cache, ra). Giờ cao điểm x2.
PRICES = {"flash": (0.15, 0.003, 0.60), "pro": (0.66, 0.022, 1.98)}
PEAK_HOURS_UTC = set(range(1, 4)) | set(range(6, 10))


def preset() -> dict:
    return {"name": NAME, "protocol": "anthropic", "base_url": BASE_URL, "model": MODEL, "fast_model": FAST_MODEL}


def is_deepseek(conn: dict | None) -> bool:
    raw = ((conn or {}).get("base_url") or "").strip()
    if not raw:
        return False
    host = urlparse(raw if "://" in raw else "https://" + raw).netloc.lower()
    return host == "deepseek.com" or host.endswith(".deepseek.com")


def is_model(model: str | None) -> bool:
    return "deepseek" in (model or "").lower()


def _has_image(messages) -> bool:
    for m in messages or []:
        c = m.get("content") if isinstance(m, dict) else None
        if isinstance(c, list) and any(isinstance(b, dict) and b.get("type") == "image" for b in c):
            return True
    return False


def adapt(kw: dict, fast_model: str | None = None) -> dict:
    """Chỉnh tham số trước khi gửi: tắt suy nghĩ (để tool_choice chạy được), ảnh -> model nhận ảnh."""
    out = {**kw, "thinking": {"type": "disabled"}}
    model = (out.get("model") or "").lower()
    if _has_image(out.get("messages")) and any(n in model for n in NO_IMAGE):
        out["model"] = fast_model or FAST_MODEL
    return out


class _Msgs:
    def __init__(self, inner, fast):
        self._inner, self._fast = inner, fast

    def create(self, **kw):
        return self._inner.create(**adapt(kw, self._fast))


class Wrapped:
    """Bọc Anthropic client: mọi lời gọi messages.create đều đi qua adapt()."""

    def __init__(self, client, fast_model: str | None = None):
        self._client = client
        self.messages = _Msgs(client.messages, fast_model)

    def __getattr__(self, name):
        return getattr(self._client, name)


def is_peak(now: datetime | None = None) -> bool:
    now = now or datetime.now(timezone.utc)
    return now.weekday() < 5 and now.hour in PEAK_HOURS_UTC


def cost(model: str | None, inp: int, out: int, cache_read: int = 0, cache_write: int = 0,
         now: datetime | None = None) -> float:
    pin, phit, pout = PRICES["flash" if "flash" in (model or "").lower() else "pro"]
    usd = ((inp + cache_write) * pin + cache_read * phit + out * pout) / 1_000_000
    return usd * (2 if is_peak(now) else 1)


def balance(conn: dict, timeout: float = 8) -> dict | None:
    req = urllib.request.Request(BALANCE_URL, headers={"Authorization": f"Bearer {conn['api_key']}",
                                                       "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            d = json.loads(r.read().decode("utf-8", "replace"))
    except Exception:
        return None
    return parse_balance(d)


def parse_balance(d: dict) -> dict | None:
    infos = d.get("balance_infos") or []
    if not infos:
        return None
    pick = next((i for i in infos if i.get("currency") == "USD"), infos[0])
    try:
        total = float(pick.get("total_balance") or 0)
    except (TypeError, ValueError):
        return None
    cur = pick.get("currency") or "USD"
    res = {"currency": cur, "remaining": total, "available": bool(d.get("is_available", True)), "source": "deepseek"}
    if cur == "USD":
        res["remaining_usd"] = total
    return res
