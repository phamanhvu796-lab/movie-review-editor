"""Đếm token đã dùng cho TỪNG nguồn kết nối và ước tính chi phí (USD).

Anthropic không có API xem số dư cho API key, nên app tự cộng dồn token từ mỗi phản hồi. Kết hợp với
"hạn mức tự đặt" (budget_usd) của nguồn để hiển thị số tiền còn lại ước tính. Lưu ở <data_dir>/usage.json.
"""
import json
import threading
from datetime import datetime, timezone

from ..utils.paths import data_dir

_lock = threading.Lock()

# Giá tham khảo USD / 1 triệu token (vào, ra). Chỉ để ƯỚC TÍNH; giá relay có thể khác.
PRICES = (("haiku", 1.0, 5.0), ("opus", 5.0, 25.0), ("sonnet", 3.0, 15.0),
          ("gpt-4o-mini", 0.15, 0.6), ("gpt-4o", 2.5, 10.0), ("gpt-4.1", 2.0, 8.0))
DEFAULT_PRICE = (3.0, 15.0)


def price_of(model: str | None) -> tuple[float, float]:
    m = (model or "").lower()
    for needle, pin, pout in PRICES:
        if needle in m:
            return pin, pout
    return DEFAULT_PRICE


def _file():
    return data_dir() / "usage.json"


def _load() -> dict:
    try:
        return json.loads(_file().read_text("utf-8"))
    except Exception:
        return {}


def _g(o, k):
    v = o.get(k) if isinstance(o, dict) else getattr(o, k, None)
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def cost(model: str | None, inp: int, out: int, cache_read: int = 0, cache_write: int = 0) -> float:
    from . import deepseek
    if deepseek.is_model(model):           # giá DeepSeek riêng (cache rẻ hơn nhiều, giờ cao điểm x2)
        return deepseek.cost(model, inp, out, cache_read, cache_write)
    pin, pout = price_of(model)
    return (inp * pin + out * pout + cache_read * pin * 0.1 + cache_write * pin * 1.25) / 1_000_000


def record(conn: dict, model: str | None, usage) -> None:
    """Gọi sau mỗi phản hồi thành công. usage: resp.usage (Anthropic) hoặc NS tương đương (OpenAI-compatible)."""
    if usage is None or not conn.get("id"):
        return
    inp, out = _g(usage, "input_tokens"), _g(usage, "output_tokens")
    cr, cw = _g(usage, "cache_read_input_tokens"), _g(usage, "cache_creation_input_tokens")
    if not (inp or out or cr or cw):
        return
    usd = cost(model, inp, out, cr, cw)
    month = datetime.now(timezone.utc).strftime("%Y-%m")
    with _lock:
        d = _load()
        c = d.setdefault(conn["id"], {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0, "requests": 0, "usd": 0.0,
                                      "since": datetime.now(timezone.utc).isoformat(timespec="seconds"), "months": {}})
        c["input"] += inp
        c["output"] += out
        c["cache_read"] += cr
        c["cache_write"] += cw
        c["requests"] += 1
        c["usd"] = round(c["usd"] + usd, 6)
        c["months"][month] = round(c["months"].get(month, 0.0) + usd, 6)
        c["last_model"] = model or ""
        try:
            _file().write_text(json.dumps(d), "utf-8")
        except OSError:
            pass


def get(conn_id: str) -> dict:
    c = _load().get(conn_id) or {}
    month = datetime.now(timezone.utc).strftime("%Y-%m")
    return {"input": c.get("input", 0), "output": c.get("output", 0), "requests": c.get("requests", 0),
            "usd": round(c.get("usd", 0.0), 4), "month_usd": round((c.get("months") or {}).get(month, 0.0), 4),
            "since": c.get("since", "")}


def reset(conn_id: str) -> None:
    with _lock:
        d = _load()
        d.pop(conn_id, None)
        _file().write_text(json.dumps(d), "utf-8")
