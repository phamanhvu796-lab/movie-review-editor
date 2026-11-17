import functools
import logging
from concurrent.futures import ThreadPoolExecutor

log = logging.getLogger("mre.jobs")
_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="job")
_render_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="render")   # hàng đợi render: 1 job/lần


def _safe(fn):
    @functools.wraps(fn)
    def wrapper(*a, **k):
        try:
            return fn(*a, **k)
        except Exception:
            log.exception("Job %s lỗi", fn.__name__)
    return wrapper


def submit(fn, *a):
    return _pool.submit(_safe(fn), *a)


def submit_render(fn, *a):
    return _render_pool.submit(_safe(fn), *a)


CANCELLED: set[str] = set()      # id render được yêu cầu huỷ
