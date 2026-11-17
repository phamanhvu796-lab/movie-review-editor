import re
from dataclasses import dataclass, field

from ..schemas import Cue

_TS = r"(?:(\d{1,2}):)?(\d{2}):(\d{2})[,.](\d{1,3})"
_TIME_LINE = re.compile(rf"^\s*{_TS}\s*-->\s*{_TS}")


@dataclass
class SrtIssue:
    line: int
    message: str


@dataclass
class SrtParseResult:
    cues: list[Cue] = field(default_factory=list)
    issues: list[SrtIssue] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.issues


def _seconds(h, m, s, ms) -> float:
    return int(h or 0) * 3600 + int(m) * 60 + int(s) + int(ms.ljust(3, "0")) / 1000


def format_timestamp(sec: float) -> str:
    ms = round(sec * 1000)
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02}:{m:02}:{s:02},{ms:03}"


def parse_srt(raw: str) -> SrtParseResult:
    """Parse SRT; không ném lỗi, gom mọi vấn đề vào result.issues."""
    text = raw.lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n")
    if text.lstrip().startswith("WEBVTT"):
        text = text.split("\n\n", 1)[1] if "\n\n" in text else ""
    result = SrtParseResult()
    line_no = 1
    for block in re.split(r"\n{2,}", text.strip()):
        lines = block.split("\n")
        first_line = line_no
        line_no += len(lines) + 1
        if not block.strip():
            continue

        # Dòng index là tuỳ chọn với một số file lỗi; nhận cả hai.
        t_idx = 1 if len(lines) > 1 and not _TIME_LINE.match(lines[0]) else 0
        m = _TIME_LINE.match(lines[t_idx]) if t_idx < len(lines) else None
        if not m:
            result.issues.append(SrtIssue(first_line, "Không tìm thấy dòng thời gian hợp lệ (HH:MM:SS,mmm --> HH:MM:SS,mmm)"))
            continue

        start = _seconds(*m.group(1, 2, 3, 4))
        end = _seconds(*m.group(5, 6, 7, 8))
        body = "\n".join(l.strip() for l in lines[t_idx + 1:]).strip()
        idx_text = lines[0].strip() if t_idx else ""
        index = int(idx_text) if idx_text.isdigit() else len(result.cues) + 1

        if end <= start:
            result.issues.append(SrtIssue(first_line, f"Cue {index}: end ({end}) phải lớn hơn start ({start})"))
            continue
        if not body:
            result.issues.append(SrtIssue(first_line, f"Cue {index}: nội dung trống"))
            continue
        if result.cues and start < result.cues[-1].end - 0.001:
            result.issues.append(SrtIssue(first_line, f"Cue {index}: chồng lấn với cue trước"))
        result.cues.append(Cue(index=index, start=start, end=end, text=body))

    if not result.cues and not result.issues:
        result.issues.append(SrtIssue(1, "File SRT rỗng"))
    return result


def to_srt(cues: list[Cue]) -> str:
    out = []
    for i, c in enumerate(cues, 1):
        out.append(f"{i}\n{format_timestamp(c.start)} --> {format_timestamp(c.end)}\n{c.text}\n")
    return "\n".join(out)


def merge_short_cues(cues: list[Cue], min_len: float = 1.2) -> list[Cue]:
    """Gộp cue ngắn hơn min_len vào cue kề để mỗi cue đủ dài cho một clip hình."""
    out: list[Cue] = []
    for c in cues:
        if out and (c.end - c.start) < min_len:
            prev = out[-1]
            out[-1] = Cue(index=prev.index, start=prev.start, end=c.end, text=f"{prev.text} {c.text}".strip())
        else:
            out.append(c)
    if len(out) > 1 and (out[0].end - out[0].start) < min_len:
        a, b = out[0], out[1]
        out[:2] = [Cue(index=a.index, start=a.start, end=b.end, text=f"{a.text} {b.text}".strip())]
    return [Cue(index=i, start=c.start, end=c.end, text=c.text) for i, c in enumerate(out, 1)]
