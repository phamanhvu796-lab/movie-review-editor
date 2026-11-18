from datetime import datetime, timezone
from types import SimpleNamespace as NS

import pytest

from app.services import deepseek, factcheck_service, usage_service


def test_detect_and_preset():
    assert deepseek.is_deepseek({"base_url": "https://api.deepseek.com/anthropic"})
    assert deepseek.is_deepseek({"base_url": "api.deepseek.com"})
    assert not deepseek.is_deepseek({"base_url": "https://evil-deepseek.com.example.org"})
    assert not deepseek.is_deepseek({"base_url": ""})
    p = deepseek.preset()
    assert p["protocol"] == "anthropic" and p["base_url"].endswith("/anthropic") and p["fast_model"] == "deepseek-flash"


def test_adapt_disables_thinking_and_routes_images_to_flash():
    kw = {"model": "deepseek-v4-pro", "messages": [{"role": "user", "content": "hi"}], "tool_choice": {"type": "tool", "name": "x"}}
    out = deepseek.adapt(kw)
    assert out["thinking"] == {"type": "disabled"} and out["model"] == "deepseek-v4-pro" and "thinking" not in kw
    img = {"model": "deepseek-v4-pro", "messages": [{"role": "user", "content": [{"type": "image", "source": {}}]}]}
    assert deepseek.adapt(img)["model"] == "deepseek-flash"


def test_wrapped_client_passes_adapted_kwargs():
    seen = {}
    inner = NS(messages=NS(create=lambda **kw: seen.update(kw) or "ok"), other=1)
    w = deepseek.Wrapped(inner, "my-flash")
    assert w.messages.create(model="deepseek-v4-pro", messages=[]) == "ok"
    assert seen["thinking"] == {"type": "disabled"} and w.other == 1


def test_cost_offpeak_peak_and_usage_hook():
    off = datetime(2026, 11, 21, 3, tzinfo=timezone.utc)        # thứ Bảy -> không cao điểm
    peak = datetime(2026, 11, 18, 7, tzinfo=timezone.utc)       # thứ Tư 07h UTC -> cao điểm
    assert abs(deepseek.cost("deepseek-flash", 1_000_000, 1_000_000, now=off) - 0.75) < 1e-9
    assert abs(deepseek.cost("deepseek-v4-pro", 1_000_000, 0, cache_read=1_000_000, now=peak) - 2 * 0.682) < 1e-9
    assert usage_service.cost("deepseek-flash", 1_000_000, 0) in (0.15, 0.3)


def test_parse_balance():
    d = {"is_available": True, "balance_infos": [{"currency": "CNY", "total_balance": "70.00"},
                                                 {"currency": "USD", "total_balance": "10.5"}]}
    assert deepseek.parse_balance(d) == {"currency": "USD", "remaining": 10.5, "available": True,
                                         "source": "deepseek", "remaining_usd": 10.5}
    cny = deepseek.parse_balance({"is_available": False, "balance_infos": [{"currency": "CNY", "total_balance": "3"}]})
    assert cny["currency"] == "CNY" and "remaining_usd" not in cny and cny["available"] is False
    assert deepseek.parse_balance({"balance_infos": []}) is None


SCRIPT = "Năm 1945, Việt Nam tuyên bố độc lập. Tháp Eiffel cao 500 mét. Lạm phát năm nay có thể tăng."


def test_factcheck_parses_and_sanitises(monkeypatch):
    data = {"summary": "Có 1 lỗi", "score": 140,
            "claims": [{"quote": "Năm 1945, Việt Nam tuyên bố độc lập.", "verdict": "dung", "reason": "Đúng"},
                       {"quote": "Tháp Eiffel cao 500 mét.", "verdict": "sai", "reason": "Khoảng 330 m",
                        "fix": "Tháp Eiffel cao khoảng 330 mét."},
                       {"quote": "Lạm phát", "verdict": "la", "reason": "?"}, {"quote": "  "}],
            "issues": [{"problem": "Câu 3 mơ hồ", "fix": "Nêu nguồn"}, {"quote": "x"}],
            "corrected_script": SCRIPT.replace("500", "khoảng 330")}
    calls = []

    def fake_call(**kw):
        calls.append(kw)
        return NS(content=[NS(type="tool_use", input=data)])
    monkeypatch.setattr(factcheck_service, "_call", fake_call)
    r = factcheck_service.check_script(SCRIPT, "vi", "history", "Lịch sử")
    assert r["score"] == 100 and len(r["claims"]) == 3 and r["claims"][2]["verdict"] == "chua_chac"
    assert r["claims"][1]["found"] and r["counts"] == {"dung": 1, "sai": 1, "chua_chac": 1, "can_nguon": 0}
    assert len(r["issues"]) == 1 and "330" in r["corrected_script"]
    assert calls[0]["tool_choice"]["name"] == "report_check" and "lịch sử" in calls[0]["system"]


def test_factcheck_drops_truncated_rewrite_and_rejects_short(monkeypatch):
    monkeypatch.setattr(factcheck_service, "_call", lambda **kw: NS(content=[NS(type="tool_use", input={
        "summary": "", "score": 80, "claims": [], "corrected_script": "ngắn"})]))
    assert factcheck_service.check_script(SCRIPT)["corrected_script"] == ""
    with pytest.raises(ValueError):
        factcheck_service.check_script("ngắn")


def test_factcheck_without_key_explains(monkeypatch):
    monkeypatch.setattr(factcheck_service.settings, "usable_connections", lambda svc="llm": [])
    assert factcheck_service.available() is False
    with pytest.raises(RuntimeError, match="DeepSeek"):
        factcheck_service._call(model="x")
