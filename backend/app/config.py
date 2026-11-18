import json
import os
import re

from .utils.paths import data_dir

# Giới hạn theo đặc tả (mục 8.5). Chỉnh ở một chỗ duy nhất.
ZOOM_RANGE = (1.0, 1.5)
SPEED_RANGE = (0.5, 2.0)
BRIGHTNESS_RANGE = (0.7, 1.4)
SATURATION_RANGE = (0.0, 1.8)
CONTRAST_RANGE = (0.7, 1.5)
VISUAL_DURATION_RANGE = (1.0, 8.0)   # độ dài clip nguồn (giây)
MAX_SCENE_REUSE = 2
MAX_FILTERS = 3
CUE_TIME_TOLERANCE = 0.05            # voice_start/end phải khớp cue SRT
SECTION_DURATION_TOLERANCE = 0.5     # tổng visual của section vs độ dài cue
TOTAL_DURATION_TOLERANCE = 1.0
SCENE_EPS = 0.05                     # sai số khi so sánh với ranh giới scene
MIN_CUE_SECONDS = 1.2                # cue ngắn hơn sẽ được gộp khi tạo từ Whisper


DEFAULT_BASES = {"anthropic": "", "elevenlabs": "https://api.elevenlabs.io", "openai": "https://api.openai.com/v1"}


def normalize_base_url(url: str, kind: str) -> str:
    """Chuẩn hoá Base URL người dùng nhập. Rỗng = địa chỉ mặc định.
    anthropic/elevenlabs: bỏ '/' và '/v1' cuối (SDK tự thêm /v1/...). openai: bảo đảm kết thúc bằng '/v1'."""
    u = (url or "").strip().rstrip("/")
    if not u:
        return DEFAULT_BASES[kind]
    if not re.match(r"^https?://", u, re.I):
        u = "https://" + u
    if kind == "openai":
        if not u.endswith("/v1"):
            u += "/v1"
    elif u.endswith("/v1"):
        u = u[:-3]
    return u.rstrip("/")


# Nhiều relay đặt sau Cloudflare chặn (error 1010) các User-Agent kiểu chương trình như "Python-urllib" / "Anthropic/Python".
DEFAULT_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/126.0.0.0 Safari/537.36")

_INVISIBLE = dict.fromkeys(map(ord, "\u200b\u200c\u200d\u2060\ufeff\u00a0"), None)


def clean_key(k: str | None) -> str:
    """Bỏ khoảng trắng, ký tự vô hình, dấu nháy bao quanh và tiền tố 'Bearer ' thường bị dán kèm."""
    k = (k or "").translate(_INVISIBLE).strip().strip("\"'`\u201c\u201d\u2018\u2019").strip()
    return k[7:].strip() if k.lower().startswith("bearer ") else k


def key_problem(k: str) -> str | None:
    """Header HTTP chỉ nhận ASCII in được. Trả về thông báo cụ thể nếu key có ký tự lạ, nếu không trả None."""
    for i, c in enumerate(k or ""):
        if not 33 <= ord(c) <= 126:
            shown = c if c.strip() else "khoảng trắng"
            return (f"API key chứa ký tự không hợp lệ \"{shown}\" ở vị trí {i + 1}. Key chỉ gồm chữ không dấu, số và - _ . "
                    "Có thể bạn dán thừa chữ, dấu … hoặc xuống dòng. Hãy dán lại đúng key gốc.")
    return None


def url_problem(u: str | None) -> str | None:
    u = (u or "").strip()
    if u and any(ord(c) > 126 or c.isspace() for c in u):
        return "Base URL có khoảng trắng hoặc ký tự lạ (chữ có dấu, dấu …). Hãy dán lại đúng địa chỉ gốc, ví dụ https://api.example.com/v1"
    return None


class Settings:
    """Cài đặt người dùng lưu ở <data_dir>/settings.json, đọc lại mỗi lần để đổi key không cần restart."""

    def _file(self):
        return data_dir() / "settings.json"

    def _load(self) -> dict:
        try:
            return json.loads(self._file().read_text("utf-8"))
        except Exception:
            return {}

    def update(self, **kw):
        d = self._load()
        d.update({k: v for k, v in kw.items() if v is not None})
        self._file().write_text(json.dumps(d, ensure_ascii=False), "utf-8")

    def base_url_raw(self, kind: str) -> str:
        return self._load().get(f"{kind}_base_url") or os.getenv(f"{kind.upper()}_BASE_URL", "")

    def base_url(self, kind: str) -> str:
        return normalize_base_url(self.base_url_raw(kind), kind)

    # ---- Nguồn kết nối (nhiều Base URL + API key) cho từng dịch vụ: llm | elevenlabs | openai_tts ----
    _KEYFIELD = {"llm": "anthropic_api_key", "elevenlabs": "elevenlabs_api_key", "openai_tts": "openai_api_key",
                 "deepseek": "deepseek_api_key"}

    def _legacy_key(self) -> str:
        return self._load().get("anthropic_api_key") or os.getenv("ANTHROPIC_API_KEY", "")

    def _all_connections(self) -> list[dict]:
        lst = self._load().get("connections")
        return lst if isinstance(lst, list) else []

    def _legacy(self, service: str) -> dict | None:
        """Key đơn của bản cũ (hoặc biến môi trường) được coi như một nguồn mặc định."""
        d = self._load()
        if service == "llm":
            key, kind, name = self._legacy_key(), "anthropic", "Claude (mặc định)"
        elif service == "elevenlabs":
            key, kind, name = d.get("elevenlabs_api_key") or os.getenv("ELEVENLABS_API_KEY", ""), "elevenlabs", "ElevenLabs (mặc định)"
        elif service == "deepseek":
            key = d.get("deepseek_api_key") or os.getenv("DEEPSEEK_API_KEY", "")
            if not key:
                return None
            from .services import deepseek
            return {"id": "default", "service": service, **deepseek.preset(), "api_key": key, "enabled": True}
        else:
            key, kind, name = d.get("openai_api_key") or os.getenv("OPENAI_API_KEY", ""), "openai", "OpenAI TTS (mặc định)"
        if not key:
            return None
        return {"id": "default", "service": service, "name": name, "protocol": kind, "base_url": self.base_url_raw(kind),
                "api_key": key, "model": "", "fast_model": "", "enabled": True}

    def connections(self, service: str = "llm") -> list[dict]:
        own = [c for c in self._all_connections() if c.get("service", "llm") == service]
        if own:
            return own
        legacy = self._legacy(service)
        return [legacy] if legacy else []

    def active_connection_id(self, service: str = "llm") -> str:
        d = self._load()
        return (d.get("active_connections") or {}).get(service) or (d.get("active_connection", "") if service == "llm" else "")

    def usable_connections(self, service: str = "llm") -> list[dict]:
        cs = [c for c in self.connections(service) if c.get("enabled", True) and c.get("api_key")]
        aid = self.active_connection_id(service)
        return sorted(cs, key=lambda c: c.get("id") != aid)        # sorted ổn định: nguồn đang chọn đứng đầu

    def active_connection(self, service: str = "llm") -> dict | None:
        cs = self.usable_connections(service)
        return cs[0] if cs else None

    def save_connections(self, conns: list[dict], active_id: str, service: str = "llm"):
        d = self._load()
        others = [c for c in self._all_connections() if c.get("service", "llm") != service]
        mine = [{**c, "service": service} for c in conns]
        d["connections"] = others + mine
        act = dict(d.get("active_connections") or {})
        act[service] = active_id
        d["active_connections"] = act
        if service == "llm":
            d["active_connection"] = active_id
        if not mine:
            d[self._KEYFIELD[service]] = ""         # xoá hết nguồn thì không để key cũ "sống lại"
        self._file().write_text(json.dumps(d, ensure_ascii=False), "utf-8")

    @property
    def anthropic_api_key(self) -> str:
        """Key của nguồn Claude đang dùng (để các nơi kiểm tra 'đã kết nối chưa')."""
        return (self.active_connection("llm") or {}).get("api_key", "")

    @property
    def claude_model(self) -> str:
        return self._load().get("claude_model") or os.getenv("CLAUDE_MODEL", "claude-sonnet-5-5")

    @property
    def whisper_model(self) -> str:
        return self._load().get("whisper_model") or os.getenv("WHISPER_MODEL", "small")

    @property
    def plan_max_retries(self) -> int:
        return int(os.getenv("PLAN_MAX_RETRIES", "3"))

    @property
    def elevenlabs_api_key(self) -> str:
        return (self.active_connection("elevenlabs") or {}).get("api_key", "")

    @property
    def openai_api_key(self) -> str:
        return (self.active_connection("openai_tts") or {}).get("api_key", "")

    @property
    def openai_tts_model(self) -> str:
        return self._load().get("openai_tts_model") or os.getenv("OPENAI_TTS_MODEL", "gpt-4o-mini-tts")

    @property
    def claude_fast_model(self) -> str:
        """Model rẻ/nhanh cho việc phân loại (gắn nhãn cảnh bằng ảnh)."""
        return self._load().get("claude_fast_model") or os.getenv("CLAUDE_FAST_MODEL", "claude-haiku-5-5")

    @property
    def tts_concurrency(self) -> int:
        """Số request ElevenLabs chạy đồng thời (gói Free thường chỉ cho 2)."""
        return int(self._load().get("tts_concurrency") or os.getenv("TTS_CONCURRENCY", "2"))


settings = Settings()
