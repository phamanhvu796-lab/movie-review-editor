from app.config import normalize_base_url as n


def test_defaults_when_empty():
    assert n("", "anthropic") == "" and n("  ", "elevenlabs") == "https://api.elevenlabs.io"
    assert n("", "openai") == "https://api.openai.com/v1"


def test_anthropic_and_elevenlabs_strip_trailing_v1_and_slash():
    assert n("https://proxy.example.com/", "anthropic") == "https://proxy.example.com"
    assert n("https://proxy.example.com/v1/", "anthropic") == "https://proxy.example.com"
    assert n("proxy.example.com/api", "elevenlabs") == "https://proxy.example.com/api"


def test_openai_always_ends_with_v1():
    assert n("https://x.com", "openai") == "https://x.com/v1"
    assert n("https://x.com/v1/", "openai") == "https://x.com/v1"


def test_http_is_kept_for_local_proxies():
    assert n("http://localhost:8080/", "anthropic") == "http://localhost:8080"


def test_clean_key_strips_junk():
    from app.config import clean_key
    assert clean_key('  "sk-abc123"  ') == "sk-abc123"
    assert clean_key("Bearer sk-abc123\n") == "sk-abc123"
    assert clean_key("sk-\u200babc\ufeff123\u00a0") == "sk-abc123"


def test_key_problem_names_the_bad_character_and_position():
    from app.config import key_problem, url_problem
    assert key_problem("sk-ant-abc_123.x-Y") is None
    msg = key_problem("sk-ant-\u2026key")           # dấu … là ký tự gây lỗi 'latin-1' ban đầu
    assert "\u2026" in msg and "vị trí 8" in msg
    assert "khoảng trắng" in key_problem("sk abc") and key_problem("sk-ến") is not None
    assert url_problem("https://api.example.com/v1") is None and url_problem("") is None
    assert url_problem("https://api.example.com/v1 ") is None            # khoảng trắng đầu/cuối được strip trước
    assert url_problem("https://api.exámple.com") is not None and url_problem("https://a b.com") is not None
