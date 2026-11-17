import logging
import re
import threading

from ..config import settings
from . import usage_service
from .llm_sources import build_single_client

log = logging.getLogger("mre.llm")
_lock = threading.Lock()
_cache: dict = {}


def _http_status(e: Exception) -> int | None:
    s = getattr(e, "status_code", None)
    if s:
        return s
    m = re.search(r"HTTP (\d{3})", str(e))
    return int(m.group(1)) if m else None


def _model_for(requested: str | None, conn: dict) -> str | None:
    """Mỗi nguồn có tên model riêng: model nhanh -> fast_model (hoặc model chính), còn lại -> model chính."""
    fast = requested is not None and requested == settings.claude_fast_model
    chosen = (conn.get("fast_model") or conn.get("model")) if fast else conn.get("model")
    return chosen or requested


def _client_for(conn: dict, retries: int):
    sig = (conn.get("id"), conn.get("protocol"), conn.get("base_url"), conn.get("api_key"), retries)
    with _lock:
        c = _cache.get(sig)
        if c is None:
            if len(_cache) > 8:
                _cache.clear()
            c = _cache[sig] = build_single_client({**conn, "_retries": retries})
        return c


class _Messages:
    def create(self, **kw):
        conns = settings.usable_connections()      # nguồn đang chọn trước, rồi các nguồn bật còn lại
        if not conns:
            raise RuntimeError("Chưa có nguồn kết nối nào. Vào Cài đặt để thêm Base URL và API key.")
        retries = 2 if len(conns) > 1 else 4        # có nguồn dự phòng thì đừng chờ retry lâu
        for i, c in enumerate(conns):
            try:
                model = _model_for(kw.get("model"), c)
                resp = _client_for(c, retries).messages.create(**{**kw, "model": model})
                try:
                    usage_service.record(c, model, getattr(resp, "usage", None))     # đếm token -> chi phí ước tính
                except Exception:
                    log.debug("Không ghi được usage", exc_info=True)
                return resp
            except Exception as e:
                if _http_status(e) == 400 or i == len(conns) - 1:   # 400 = lỗi yêu cầu của ta, nguồn khác cũng sẽ lỗi
                    raise
                log.warning("Nguồn '%s' lỗi (%s), chuyển sang nguồn kế tiếp", c.get("name"), str(e)[:150])


class FailoverClient:
    def __init__(self):
        self.messages = _Messages()


_CLIENT = FailoverClient()


def get_client() -> FailoverClient:
    """Client dùng chung, tự chọn nguồn theo cấu hình hiện tại và tự chuyển nguồn khi lỗi."""
    return _CLIENT
