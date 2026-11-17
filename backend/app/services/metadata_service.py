from anthropic import Anthropic

from ..config import settings
from .claude_client import get_client

TOOL = {
    "name": "create_video_metadata",
    "description": "Tiêu đề, mô tả, hashtag và chữ thumbnail cho video review phim.",
    "input_schema": {"type": "object", "properties": {
        "titles": {"type": "array", "items": {"type": "string"}, "description": "5 tiêu đề, tối đa 70 ký tự"},
        "description": {"type": "string", "description": "Mô tả YouTube: 2 câu hook, tóm tắt, kêu gọi đăng ký"},
        "hashtags": {"type": "array", "items": {"type": "string"}, "description": "5-8 hashtag, có dấu #"},
        "thumbnail_text": {"type": "array", "items": {"type": "string"}, "description": "3 phương án, mỗi cái tối đa 4 từ"},
    }, "required": ["titles", "description", "hashtags", "thumbnail_text"]},
}


def generate_metadata(movie_title: str, genre: str, language: str, direction: str, script: str,
                      allow_spoilers: bool = False, client: Anthropic | None = None) -> dict:
    client = client or get_client()
    system = ("Bạn là chuyên gia YouTube SEO cho kênh review phim. Viết bằng ngôn ngữ mã '" + language + "'. "
              "Tiêu đề gây tò mò nhưng không đánh lừa. "
              + ("" if allow_spoilers else "Không tiết lộ kết phim hay twist. ")
              + "Chỉ trả kết quả bằng cách gọi tool create_video_metadata.")
    user = (f"Phim: {movie_title}\nThể loại: {genre}\nHướng sáng tạo: {direction}\n\nKịch bản voice-over:\n{script[:8000]}")
    resp = client.messages.create(model=settings.claude_model, max_tokens=2000, system=system, tools=[TOOL],
                                  tool_choice={"type": "tool", "name": TOOL["name"]},
                                  messages=[{"role": "user", "content": user}])
    block = next((b for b in resp.content if b.type == "tool_use"), None)
    if block is None:
        raise RuntimeError("Claude không trả về metadata")
    return block.input
