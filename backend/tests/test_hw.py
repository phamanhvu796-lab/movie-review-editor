from app.services import ffmpeg_service as ff


def _reset(monkeypatch, listing, ok):
    ff._hw_cache.clear()
    monkeypatch.setattr(ff, "find_binary", lambda n: "ffmpeg")

    def fake(cmd):
        if "-encoders" in cmd:
            return 0, listing
        name = cmd[cmd.index("-c:v") + 1]
        return (0 if name in ok else 1), ""
    monkeypatch.setattr(ff, "_run_quiet", fake)


def test_picks_first_encoder_that_really_works(monkeypatch):
    _reset(monkeypatch, "h264_nvenc h264_qsv libx264", ok={"h264_qsv"})   # có trong danh sách nhưng không có GPU NVIDIA
    assert ff.detect_hw_encoder() == "h264_qsv"


def test_no_hw_when_test_encode_fails_or_not_listed(monkeypatch):
    _reset(monkeypatch, "h264_nvenc libx264", ok=set())
    assert ff.detect_hw_encoder() is None
    _reset(monkeypatch, "libx264", ok={"h264_nvenc"})
    assert ff.detect_hw_encoder() is None


def test_detection_is_cached(monkeypatch):
    _reset(monkeypatch, "h264_nvenc", ok={"h264_nvenc"})
    assert ff.detect_hw_encoder() == "h264_nvenc"
    monkeypatch.setattr(ff, "_run_quiet", lambda c: (_ for _ in ()).throw(AssertionError("không được chạy lại")))
    assert ff.detect_hw_encoder() == "h264_nvenc"


def test_codec_args():
    a = ff.segment_codec_args(None, 16)
    assert a[a.index("-crf") + 1] == "16"
    assert "-threads" in a and int(a[a.index("-threads") + 1]) >= 1   # CPU: chừa lõi để máy không đơ
    assert "-threads" not in ff.segment_codec_args("h264_nvenc")
    assert "h264_nvenc" in ff.segment_codec_args("h264_nvenc")
    for enc in (None, "h264_nvenc", "h264_qsv", "h264_amf"):
        a = ff.final_codec_args(enc, False, 8000)
        assert a[a.index("-b:v") + 1] == "8000k" and a[a.index("-bufsize") + 1] == "16000k"
    assert ff.final_codec_args(None, True, 3000)[3] == "veryfast"
