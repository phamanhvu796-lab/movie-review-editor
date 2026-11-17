from starlette.requests import Request

from app.utils.rangefile import ranged_file


def req(rng=None):
    headers = [(b"range", rng.encode())] if rng else []
    return Request({"type": "http", "headers": headers})


def test_full_and_partial_ranges(tmp_path):
    f = tmp_path / "a.bin"
    f.write_bytes(bytes(range(100)))
    r = ranged_file(req(), str(f), "video/mp4")
    assert r.status_code == 200 and r.headers["content-length"] == "100"
    r = ranged_file(req("bytes=10-19"), str(f), "video/mp4")
    assert r.status_code == 206 and r.headers["content-range"] == "bytes 10-19/100" and r.headers["content-length"] == "10"
    r = ranged_file(req("bytes=90-"), str(f), "video/mp4")
    assert r.headers["content-range"] == "bytes 90-99/100"
    r = ranged_file(req("bytes=-5"), str(f), "video/mp4")
    assert r.headers["content-range"] == "bytes 95-99/100"


def test_invalid_range_returns_416(tmp_path):
    f = tmp_path / "a.bin"
    f.write_bytes(b"x" * 10)
    assert ranged_file(req("bytes=50-60"), str(f), "video/mp4").status_code == 416
    assert ranged_file(req("bytes=abc-"), str(f), "video/mp4").status_code == 416
