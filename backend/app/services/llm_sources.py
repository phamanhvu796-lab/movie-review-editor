import json
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse

from ..config import DEFAULT_UA, clean_key, key_problem, normalize_base_url, url_problem
from .api_errors import friendly
from .openai_compat import OpenAICompat, pick_model

LABEL = {"anthropic": "Anthropic", "openai": "OpenAI-compatible"}


def mask_conn(c: dict) -> dict:
    key = c.get("api_key", "")
    return {"id": c.get("id"), "service": c.get("service", "llm"), "name": c.get("name", ""), "protocol": c.get("protocol", "anthropic"),
            "base_url": c.get("base_url", ""), "user_agent": c.get("user_agent", ""), "model": c.get("model", ""), "fast_model": c.get("fast_model", ""),
            "enabled": c.get("enabled", True), "budget_usd": c.get("budget_usd"), "key_hint": ("…" + key[-4:]) if len(key) > 8 else ("••••" if key else "")}


def default_name(c: dict) -> str:
    u = (c.get("base_url") or "").strip()
    host = urlparse(u if "://" in u else "https://" + u).netloc if u else ""
    return host or "Anthropic"


def build_single_client(conn: dict):
    """Client cho đúng MỘT nguồn. Anthropic: chỉ truyền api_key (SDK không cho truyền cùng lúc api_key và auth_token),
    với Base URL tuỳ chỉnh thì thêm header Authorization: Bearer qua default_headers để tương thích relay."""
    key = conn["api_key"]
    if conn.get("protocol") == "openai":
        return OpenAICompat(normalize_base_url(conn.get("base_url", ""), "openai"), key, conn.get("user_agent"),
                            timeout=conn.get("_timeout", 300), retries=conn.get("_retries", 3))
    from anthropic import Anthropic          # import muộn: module này dùng được cả khi chưa cài SDK
    base = normalize_base_url(conn.get("base_url", ""), "anthropic") or None
    hdr = {}
    if base:        # relay: thêm Bearer + User-Agent giống trình duyệt (tránh Cloudflare error 1010)
        hdr = {"Authorization": f"Bearer {key}", "User-Agent": conn.get("user_agent") or DEFAULT_UA}
    elif conn.get("user_agent"):
        hdr = {"User-Agent": conn["user_agent"]}
    kw = {"default_headers": hdr} if hdr else {}
    if conn.get("_timeout"):
        import httpx
        timeout = httpx.Timeout(conn["_timeout"], connect=10.0)     # thử kết nối: không treo hàng phút nếu relay không trả lời
    else:
        timeout = 300.0
    return Anthropic(api_key=key, base_url=base, max_retries=conn.get("_retries", 4), timeout=timeout, **kw)


def probe_models(conn: dict) -> list[str]:
    key = conn["api_key"]
    if conn.get("protocol") == "openai":
        url = normalize_base_url(conn.get("base_url", ""), "openai") + "/models"
        headers = {"Authorization": f"Bearer {key}"}
    else:
        base = normalize_base_url(conn.get("base_url", ""), "anthropic") or "https://api.anthropic.com"
        url = base + "/v1/models?limit=100"
        headers = {"x-api-key": key, "anthropic-version": "2023-06-01", "Authorization": f"Bearer {key}"}
    headers = {"User-Agent": conn.get("user_agent") or DEFAULT_UA, "Accept": "application/json", **headers}
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=15) as r:
        data = json.loads(r.read().decode("utf-8", "replace"))
    return [m["id"] for m in data.get("data", []) if isinstance(m, dict) and "id" in m]


def _attempt(conn: dict, proto: str) -> tuple[dict, list[str]]:
    """Một lần thử với một kiểu API. Trả về (kết quả, các dòng chi tiết để hiển thị cho người dùng)."""
    c = {**conn, "protocol": proto, "_retries": 0, "_timeout": 40}
    base = normalize_base_url(conn.get("base_url", ""), "openai" if proto == "openai" else "anthropic")
    endpoint = f"{base}/chat/completions" if proto == "openai" else f"{base or 'https://api.anthropic.com'}/v1/messages"
    lines, t0 = [], time.time()
    try:
        models = probe_models(c)
        lines.append(f"{LABEL[proto]}: lấy danh sách model → {len(models)} model")
    except Exception as e:
        models = []
        lines.append(f"{LABEL[proto]}: lấy danh sách model thất bại ({friendly(e)})")
    model = conn.get("model") or pick_model(models) or "claude-sonnet-5-5"
    try:
        build_single_client(c).messages.create(model=model, max_tokens=8, messages=[{"role": "user", "content": "ping"}])
        lines.append(f"{LABEL[proto]}: POST {endpoint} (model {model}) → OK sau {time.time() - t0:.1f}s")
        return {"ok": True, "protocol": proto, "model": model, "models": models[:80]}, lines
    except Exception as e:
        err = friendly(e)
        lines.append(f"{LABEL[proto]}: POST {endpoint} (model {model}) → LỖI: {err}")
        return {"ok": False, "err": err}, lines


def test_connection(conn: dict) -> dict:
    """Thử thật một nguồn (tối đa ~50 giây). protocol='auto': thử Anthropic và OpenAI-compatible SONG SONG, giữ kiểu chạy được.
    Model để trống: lấy danh sách model của nguồn rồi tự chọn. Kết quả kèm 'trace' mô tả từng bước để dễ tìm lỗi."""
    conn = {**conn, "api_key": clean_key(conn.get("api_key"))}
    if not conn["api_key"]:
        return {"ok": False, "detail": "Chưa có API key", "models": [], "trace": []}
    bad = key_problem(conn["api_key"]) or url_problem(conn.get("base_url"))
    if bad:
        return {"ok": False, "detail": bad, "models": [], "trace": []}
    protos = ["anthropic", "openai"] if conn.get("protocol", "auto") == "auto" else [conn["protocol"]]
    with ThreadPoolExecutor(max_workers=len(protos)) as ex:
        results = list(ex.map(lambda p: _attempt(conn, p), protos))
    trace = [ln for _, lines in results for ln in lines]
    for proto, (res, _) in zip(protos, results):
        if res["ok"]:
            return {"ok": True, "protocol": proto, "model": res["model"], "models": res["models"], "trace": trace,
                    "detail": f"Kết nối OK ({LABEL[proto]}), model {res['model']}"}
    errs = [f"{LABEL[p]}: {r['err']}" for p, (r, _) in zip(protos, results)]
    msgs = [e.split(": ", 1)[-1] for e in errs]
    detail = msgs[0] if len(set(msgs)) == 1 else " | ".join(errs)      # cùng một lỗi thì chỉ nói một lần
    return {"ok": False, "detail": detail, "models": [], "trace": trace}
