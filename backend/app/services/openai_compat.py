"""Bộ chuyển đổi: cho phép dùng nguồn OpenAI-compatible (/chat/completions) với cùng giao diện
`client.messages.create(...)` của SDK Anthropic mà phần còn lại của app đang dùng (tool use, ảnh)."""
import json
import random
import time
import urllib.error
import urllib.request
from types import SimpleNamespace as NS

from ..config import DEFAULT_UA


def _g(o, k, d=None):
    return o.get(k, d) if isinstance(o, dict) else getattr(o, k, d)


def convert_messages(system, messages) -> list[dict]:
    out: list[dict] = []
    if system:
        out.append({"role": "system", "content": system if isinstance(system, str)
                    else "\n".join(_g(b, "text", "") for b in system)})
    for m in messages:
        role, content = _g(m, "role"), _g(m, "content")
        if isinstance(content, str):
            out.append({"role": role, "content": content})
            continue
        blocks = list(content or [])
        if role == "assistant":
            text = "".join(_g(b, "text", "") for b in blocks if _g(b, "type") == "text")
            calls = [{"id": _g(b, "id"), "type": "function",
                      "function": {"name": _g(b, "name"), "arguments": json.dumps(_g(b, "input") or {}, ensure_ascii=False)}}
                     for b in blocks if _g(b, "type") == "tool_use"]
            msg = {"role": "assistant", "content": text or None}
            if calls:
                msg["tool_calls"] = calls
            out.append(msg)
            continue
        parts = []
        for b in blocks:
            t = _g(b, "type")
            if t == "tool_result":      # kết quả tool -> message role "tool" (phải đứng ngay sau assistant tool_calls)
                c = _g(b, "content")
                if not isinstance(c, str):
                    c = "".join(_g(x, "text", "") for x in (c or []))
                out.append({"role": "tool", "tool_call_id": _g(b, "tool_use_id"), "content": c})
            elif t == "text":
                parts.append({"type": "text", "text": _g(b, "text", "")})
            elif t == "image":
                s = _g(b, "source")
                parts.append({"type": "image_url",
                              "image_url": {"url": f"data:{_g(s, 'media_type')};base64,{_g(s, 'data')}"}})
        if parts:
            if all(p["type"] == "text" for p in parts):      # nhiều relay chỉ nhận content dạng chuỗi
                out.append({"role": "user", "content": "\n\n".join(p["text"] for p in parts)})
            else:
                out.append({"role": "user", "content": parts})
    return out


def _extract_json(text: str):
    a, b = text.find("{"), text.rfind("}")
    if a < 0 or b <= a:
        return None
    try:
        return json.loads(text[a:b + 1])
    except ValueError:
        return None


def convert_response(data: dict, tools=None, tool_choice=None):
    choice = (data.get("choices") or [{}])[0]
    msg = choice.get("message") or {}
    blocks = []
    text = msg.get("content")
    if isinstance(text, list):
        text = "".join(p.get("text", "") for p in text if isinstance(p, dict))
    if text:
        blocks.append(NS(type="text", text=text))
    for tc in msg.get("tool_calls") or []:
        args = (tc.get("function") or {}).get("arguments")
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except ValueError:
                args = _extract_json(args) or {}
        blocks.append(NS(type="tool_use", id=tc.get("id") or "call_0",
                         name=(tc.get("function") or {}).get("name"), input=args or {}))
    if tools and text and not any(b.type == "tool_use" for b in blocks):
        # Một số model bỏ qua tool_choice và trả JSON trong nội dung: vẫn dùng được.
        obj = _extract_json(text)
        if obj is not None:
            name = (tool_choice or {}).get("name") or tools[0]["name"]
            blocks.append(NS(type="tool_use", id="call_text", name=name, input=obj))
    stop = "max_tokens" if choice.get("finish_reason") == "length" else (
        "tool_use" if any(b.type == "tool_use" for b in blocks) else "end_turn")
    u = data.get("usage") or {}
    usage = NS(input_tokens=int(u.get("prompt_tokens") or 0), output_tokens=int(u.get("completion_tokens") or 0),
               cache_read_input_tokens=int((u.get("prompt_tokens_details") or {}).get("cached_tokens") or 0),
               cache_creation_input_tokens=0)
    return NS(content=blocks, stop_reason=stop, usage=usage)


def pick_model(models: list[str]) -> str | None:
    """Chọn model hợp lý nhất từ danh sách của relay: ưu tiên claude + sonnet (bản mới nhất), rồi claude, rồi cái đầu."""
    if not models:
        return None
    for needle in (("claude", "sonnet"), ("claude",)):
        c = sorted(m for m in models if all(n in m.lower() for n in needle))
        if c:
            return c[-1]
    return models[0]


def _explain(code: int, body: str) -> str:
    msg = None
    try:
        j = json.loads(body)
        e = j.get("error", j.get("detail", j))
        msg = e.get("message") if isinstance(e, dict) else (e if isinstance(e, str) else None)
    except (ValueError, AttributeError):
        pass
    if code == 401:
        return f"HTTP 401: API key không hợp lệ hoặc bị từ chối. {msg or ''}".strip()
    if code == 404:
        return f"HTTP 404: không tìm thấy đường dẫn hoặc model ({msg or 'kiểm tra Base URL và tên model'})"
    return f"HTTP {code}: {msg or body[:200]}"


def _post(url: str, headers: dict, payload: dict, timeout: float = 300, retries: int = 3) -> dict:
    data = json.dumps(payload).encode()
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8", "replace"))
        except UnicodeError:
            raise RuntimeError("API key hoặc Base URL chứa ký tự không hợp lệ (chữ có dấu, dấu …, khoảng trắng). Hãy dán lại đúng key/URL gốc.")
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")[:600]
            if e.code in (429, 500, 502, 503, 504, 529) and attempt < retries:
                try:
                    delay = float((e.headers.get("Retry-After") if e.headers else "") or "")
                except ValueError:
                    delay = min(2 ** attempt * 2, 20)
                time.sleep(min(delay, 60) + random.uniform(0, 0.5))
                continue
            raise RuntimeError(_explain(e.code, body))
        except urllib.error.URLError as e:
            if attempt < min(retries, 2):
                time.sleep(2 ** attempt)
                continue
            raise RuntimeError(f"Không kết nối được máy chủ API: {e.reason}")
    raise RuntimeError("Gọi API thất bại")


class _Messages:
    def __init__(self, owner):
        self._o = owner

    def create(self, **kw):
        return self._o.create(**kw)


class OpenAICompat:
    def __init__(self, base_url: str, api_key: str, user_agent: str | None = None, timeout: float = 300, retries: int = 3):
        self.base, self.key, self.ua = base_url.rstrip("/"), api_key, user_agent or DEFAULT_UA
        self.timeout, self.retries = timeout, retries
        self.messages = _Messages(self)

    def create(self, model, max_tokens=1024, system=None, messages=(), tools=None, tool_choice=None, **_ignored):
        payload = {"model": model, "messages": convert_messages(system, messages), "max_tokens": max_tokens}
        if tools:
            payload["tools"] = [{"type": "function", "function": {"name": t["name"], "description": t.get("description", ""),
                                                                   "parameters": t["input_schema"]}} for t in tools]
            if tool_choice and tool_choice.get("type") == "tool":
                payload["tool_choice"] = {"type": "function", "function": {"name": tool_choice["name"]}}
        headers = {"Authorization": f"Bearer {self.key}", "Content-Type": "application/json",
                   "Accept": "application/json", "User-Agent": self.ua}
        try:
            data = _post(f"{self.base}/chat/completions", headers, payload, self.timeout, self.retries)
        except RuntimeError as e:      # model mới của OpenAI đổi tên tham số
            if "max_completion_tokens" not in str(e):
                raise
            payload["max_completion_tokens"] = payload.pop("max_tokens")
            data = _post(f"{self.base}/chat/completions", headers, payload, self.timeout, self.retries)
        return convert_response(data, tools, tool_choice)
