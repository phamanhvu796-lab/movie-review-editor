from app.services.srt_service import format_timestamp, parse_srt, to_srt

BASIC = "1\n00:00:00,000 --> 00:00:04,200\nXin chào\n\n2\n00:00:04,200 --> 00:00:08,000\nDòng 1\nDòng 2\n"


def test_basic():
    r = parse_srt(BASIC)
    assert r.ok and len(r.cues) == 2
    assert r.cues[0].end == 4.2
    assert r.cues[1].text == "Dòng 1\nDòng 2"


def test_bom_crlf_and_dot_separator():
    raw = "\ufeff" + BASIC.replace("\n", "\r\n").replace(",", ".")
    r = parse_srt(raw)
    assert r.ok and r.cues[0].end == 4.2


def test_invalid_timestamp_reports_issue():
    r = parse_srt("1\n00:00:xx --> 00:00:02,000\nHi\n")
    assert not r.ok and not r.cues


def test_end_before_start():
    r = parse_srt("1\n00:00:05,000 --> 00:00:02,000\nHi\n")
    assert not r.ok


def test_overlap_flagged_but_kept():
    r = parse_srt("1\n00:00:00,000 --> 00:00:05,000\nA\n\n2\n00:00:04,000 --> 00:00:06,000\nB\n")
    assert len(r.cues) == 2 and not r.ok


def test_empty():
    assert not parse_srt("").ok


def test_roundtrip_and_timestamp():
    assert format_timestamp(3661.5) == "01:01:01,500"
    again = parse_srt(to_srt(parse_srt(BASIC).cues))
    assert [c.text for c in again.cues] == ["Xin chào", "Dòng 1\nDòng 2"]


def test_vtt_header_and_short_timestamps():
    r = parse_srt("WEBVTT\n\ncue1\n00:01.000 --> 00:03.500 align:start\nHello\n")
    assert r.ok and r.cues[0].start == 1.0 and r.cues[0].end == 3.5


def test_merge_short_cues():
    from app.schemas import Cue
    from app.services.srt_service import merge_short_cues
    cues = [Cue(index=1, start=0, end=3, text="a"), Cue(index=2, start=3, end=3.4, text="b"),
            Cue(index=3, start=3.4, end=7, text="c")]
    out = merge_short_cues(cues)
    assert [c.text for c in out] == ["a b", "c"] and out[0].end == 3.4 and [c.index for c in out] == [1, 2]
