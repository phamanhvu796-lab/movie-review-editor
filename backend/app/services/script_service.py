"""AI viết kịch bản voice-over khi dự án chưa có (để vá chỗ thiếu trước khi dựng)."""
from ..config import settings
from .claude_client import get_client

TOOL = {
    "name": "write_review_script",
    "description": "Trả về kịch bản voice-over cho video review phim.",
    "input_schema": {"type": "object", "properties": {
        "script": {"type": "string", "description": "Toàn bộ lời đọc, chia đoạn bằng dòng trống. Không ghi chú đạo diễn, không tiêu đề."},
    }, "required": ["script"]},
}

# Tốc độ đọc trung bình (ký tự/giây) để ước lượng độ dài kịch bản theo thời lượng mục tiêu.
CHARS_PER_SEC = {"vi": 14.0, "en": 15.0, "es": 15.0, "ja": 7.0, "zh": 5.0, "ko": 7.0}


def write_script(movie_title: str, genre: str, language: str, target_seconds: int, scene_notes: list[str],
                 style: str = "", allow_spoilers: bool = False, client=None) -> str:
    client = client or get_client()
    chars = int(CHARS_PER_SEC.get(language, 14.0) * max(30, target_seconds))
    system = ("Bạn là biên kịch kênh YouTube review phim. Viết lời đọc tự nhiên, cuốn hút, câu ngắn dễ đọc to, "
              f"bằng ngôn ngữ mã '{language}'. Cấu trúc: hook mạnh 1-2 câu -> bối cảnh và nhân vật -> diễn biến chính -> "
              "diễn xuất / hình ảnh / âm nhạc -> đánh giá -> kêu gọi đăng ký. "
              + ("" if allow_spoilers else "KHÔNG tiết lộ kết phim hay twist. ")
              + "Chỉ nói điều phù hợp với phim; không chắc chi tiết nào thì nói chung chung, không bịa tên riêng. "
                "Trả kết quả bằng tool write_review_script.")
    notes = "\n".join(scene_notes[:120])
    user = (f"Phim: {movie_title}\nThể loại: {genre or 'chưa rõ'}\nPhong cách: {style or 'kể chuyện cuốn hút'}\n"
            f"Độ dài mục tiêu: khoảng {chars} ký tự (~{target_seconds // 60} phút đọc).\n"
            + (f"\nCác cảnh có trong footage (để lời đọc khớp hình):\n{notes}" if notes else ""))
    resp = client.messages.create(model=settings.claude_model, max_tokens=min(16000, chars // 2 + 2000), system=system,
                                  tools=[TOOL], tool_choice={"type": "tool", "name": TOOL["name"]},
                                  messages=[{"role": "user", "content": user}])
    block = next((b for b in resp.content if b.type == "tool_use"), None)
    text = ((block.input or {}).get("script") if block else "") or "".join(getattr(b, "text", "") for b in resp.content)
    text = (text or "").strip()
    if len(text) < 50:
        raise RuntimeError("AI không viết được kịch bản (nội dung trả về quá ngắn)")
    return text


def estimate_cues(script: str, total_seconds: float):
    """Không có Whisper: chia thời gian voice-over theo độ dài từng câu (ước lượng, đủ để lên bản dựng)."""
    from ..schemas import Cue
    from .tts_service import split_sentences
    lines = []
    for s in split_sentences(script):
        s = s.strip()
        while len(s) > 84:                          # câu dài -> tách cho vừa phụ đề
            cut = s.rfind(" ", 0, 84)
            cut = cut if cut > 20 else 84
            lines.append(s[:cut].strip())
            s = s[cut:].strip()
        if s:
            lines.append(s)
    total_chars = sum(len(x) for x in lines) or 1
    t, out = 0.0, []
    for i, line in enumerate(lines, 1):
        d = total_seconds * len(line) / total_chars
        out.append(Cue(index=i, start=round(t, 3), end=round(t + d, 3), text=line))
        t += d
    return out
