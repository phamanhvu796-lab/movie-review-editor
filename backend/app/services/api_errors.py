import re

_CF = {
    "1010": "Relay (Cloudflare) chặn vì 'chữ ký trình duyệt' (error 1010). App đã gửi User-Agent giống trình duyệt; nếu vẫn bị chặn, "
            "thử đổi User-Agent ở mục Sửa nguồn, mở Base URL bằng trình duyệt xem có vào được không, hoặc nhờ nhà cung cấp mở chặn.",
    "1020": "Relay (Cloudflare) chặn truy cập bằng quy tắc tường lửa (error 1020). Nhờ nhà cung cấp mở chặn hoặc đổi User-Agent ở mục Sửa nguồn.",
    "1015": "Relay (Cloudflare) đang giới hạn tốc độ (error 1015). Đợi vài phút rồi thử lại.",
    "1006": "IP của bạn bị relay chặn (Cloudflare 1006/1007/1008). Thử đổi mạng/VPN hoặc liên hệ nhà cung cấp.",
}
_CF["1007"] = _CF["1008"] = _CF["1006"]


def _cloudflare(msg: str) -> str | None:
    m = re.search(r"error code:?\s*(1\d{3})", msg, re.I)
    if m and m.group(1) in _CF:
        return _CF[m.group(1)]
    if "cloudflare" in msg.lower() or "attention required" in msg.lower():
        return "Relay (Cloudflare) từ chối kết nối. Thử đổi User-Agent ở mục Sửa nguồn, kiểm tra Base URL bằng trình duyệt hoặc liên hệ nhà cung cấp."
    return None


def friendly(e: Exception) -> str:
    """Đổi lỗi kỹ thuật của SDK Anthropic thành thông báo tiếng Việt dễ hành động."""
    if isinstance(e, UnicodeError):
        return "API key hoặc Base URL chứa ký tự không hợp lệ (chữ có dấu, dấu …, khoảng trắng). Hãy dán lại đúng key/URL gốc."
    cf = _cloudflare(str(e))
    if cf:
        return cf
    try:
        import anthropic as A
    except ImportError:
        return str(e)
    names = lambda n: getattr(A, n, ())
    msg = str(e)
    if isinstance(e, RuntimeError):
        return msg
    if isinstance(e, names("AuthenticationError")):
        return "Anthropic API key không hợp lệ hoặc đã bị thu hồi. Kiểm tra lại trong Cài đặt."
    if isinstance(e, names("PermissionDeniedError")):
        return "Bị từ chối (403): key không có quyền với model này hoặc relay chặn truy cập. Kiểm tra tên model và nhóm quyền của key trên relay."
    if isinstance(e, names("NotFoundError")):
        return "Không tìm thấy model hoặc đường dẫn API. Kiểm tra tên model và Base URL trong Cài đặt."
    if isinstance(e, names("RateLimitError")):
        return "Vượt giới hạn tốc độ của Anthropic (đã tự thử lại nhiều lần). Đợi vài phút rồi chạy lại."
    if isinstance(e, names("APIConnectionError")):
        return "Không kết nối được tới máy chủ API. Kiểm tra Base URL, mạng, VPN hoặc proxy."
    if "credit balance" in msg.lower():
        return "Tài khoản Anthropic hết credit. Nạp thêm tại console.anthropic.com."
    if getattr(e, "status_code", None) in (500, 502, 503, 529):
        return "Dịch vụ Anthropic đang quá tải hoặc lỗi tạm thời (đã tự thử lại). Thử lại sau ít phút."
    return msg
