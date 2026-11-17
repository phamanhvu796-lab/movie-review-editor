import tempfile

from app.services import folders, sysinfo


def test_scan_and_live(monkeypatch):
    monkeypatch.setenv("MRE_DATA_DIR", tempfile.mkdtemp())
    hw = sysinfo.scan(force=True)
    assert hw["threads"] >= 1 and hw["cpu"]
    lv = sysinfo.live([tempfile.gettempdir()])
    assert "cpu" in lv and lv["disks"] and lv["mode"] == "gpu"


def test_perf_policy(monkeypatch):
    monkeypatch.setenv("MRE_DATA_DIR", tempfile.mkdtemp())
    monkeypatch.setattr(sysinfo.os, "cpu_count", lambda: 12)
    assert sysinfo.cpu_threads() == 7                    # chừa lõi cho máy, không chiếm 100% CPU
    assert sysinfo.parallel_segments("h264_nvenc") >= sysinfo.parallel_segments(None)
    sysinfo.settings.update(perf_mode="eco")
    assert sysinfo.parallel_segments("h264_nvenc") == 1 and sysinfo.cpu_threads() == 3
    sysinfo.settings.update(perf_mode="max")
    assert sysinfo.cpu_threads() == 0 and sysinfo.priority_flags() == 0


def test_folders(monkeypatch, tmp_path):
    monkeypatch.setenv("MRE_DATA_DIR", tempfile.mkdtemp())
    (tmp_path / "in" / "phim").mkdir(parents=True)
    (tmp_path / "in" / "phim" / "a.mp4").write_bytes(b"x")
    (tmp_path / "in" / "voice.mp3").write_bytes(b"x")
    (tmp_path / "in" / "note.txt").write_bytes(b"x")
    folders.set_dirs(str(tmp_path / "in"), str(tmp_path / "out"))
    names = sorted(f["name"] for f in folders.list_inputs())
    assert names == ["a.mp4", "voice.mp3"]
    f = folders.output_file('Review: "Inception"?', "16:9", "1920x1080")
    assert f.parent == (tmp_path / "out").resolve() and ":" not in f.name and '"' not in f.name
