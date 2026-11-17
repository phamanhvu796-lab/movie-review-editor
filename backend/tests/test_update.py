import hashlib
import io
import tempfile
import time

import pytest

from app.services import update_service as U

REL = {"tag_name": "v9.1.0", "body": "Sửa lỗi", "html_url": "https://github.com/a/b/releases/v9.1.0", "assets": [
    {"name": "MovieReviewEditor-Setup-9.1.0.exe", "browser_download_url": "https://x/setup.exe", "size": 5,
     "digest": "sha256:" + hashlib.sha256(b"hello").hexdigest()},
    {"name": "MovieReviewEditor-Portable-9.1.0.exe", "browser_download_url": "https://x/p.exe", "size": 3}]}


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("MRE_DATA_DIR", tempfile.mkdtemp())
    U._last.clear()
    U._state.update(phase="idle", progress=0.0, error="", file="", kind="")


def test_version_compare():
    assert U.is_newer("1.10.0", "1.9.9") and U.is_newer("v2.0", "1.99.99")
    assert not U.is_newer("1.2.0", "1.2.0") and not U.is_newer("1.1.9", "1.2.0")


def test_check_github_release(monkeypatch):
    monkeypatch.setattr(U, "source", lambda: "owner/repo")
    monkeypatch.setattr(U, "_get_json", lambda url, timeout=15: REL)
    r = U.check(force=True)
    assert r["available"] and r["latest"] == "9.1.0"
    assert r["assets"]["setup_url"].endswith("setup.exe") and r["assets"]["portable_size"] == 3
    assert U._which_asset(r) == "setup"                                   # bản cài -> dùng Setup
    monkeypatch.setattr(U, "dist_kind", lambda: "portable")
    assert U._which_asset(r) == "portable"                                # bản 1 file -> dùng Portable


def test_no_source_and_bad_source(monkeypatch):
    monkeypatch.setattr(U, "source", lambda: "")
    assert not U.check(force=True)["available"]
    monkeypatch.setattr(U, "source", lambda: "khong hop le")
    assert "phải dạng" in U.check(force=True)["error"]


def _download(monkeypatch, payload):
    monkeypatch.setattr(U, "source", lambda: "owner/repo")
    monkeypatch.setattr(U, "_get_json", lambda url, timeout=15: REL)

    class R(io.BytesIO):
        headers = {"Content-Length": str(len(payload))}

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False
    monkeypatch.setattr(U.urllib.request, "urlopen", lambda req, timeout=60: R(payload))
    U.start_download()
    for _ in range(100):
        if U.state()["phase"] in ("ready", "error"):
            break
        time.sleep(0.02)
    return U.state()


def test_download_verifies_checksum(monkeypatch):
    st = _download(monkeypatch, b"hello")
    assert st["phase"] == "ready" and st["file"].endswith("Setup-9.1.0.exe")


def test_download_rejects_tampered_file(monkeypatch):
    st = _download(monkeypatch, b"hellX")
    assert st["phase"] == "error" and "mã kiểm tra" in st["error"]
