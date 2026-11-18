"""Kiểm tra kịch bản & kiểm chứng thông tin bằng DeepSeek (nguồn riêng, gắn tay trong Cài đặt).

DeepSeek chỉ đọc kịch bản rồi đánh giá từng nhận định (sự kiện, con số, tên, ngày tháng) theo hiểu biết của model —
không tự lên mạng tra cứu, nên kết quả là "cảnh báo để người dùng kiểm tra lại", không phải xác nhận tuyệt đối.
"""
import logging

from ..config import settings
from . import usage_service
from .llm_sources import build_single_client

log = logging.getLogger("mre.factcheck")

VERDICTS = ("dung", "sai", "chua_chac", "can_nguon")
TOOL = {
    "name": "report_check",
    "description": "Báo cáo kiểm tra kịch bản và kiểm chứng thông tin.",
    "input_schema": {
        "type": "object",
        "properties": {
            "summary": {"type": "string", "description": "Nhận xét chung 1-3 câu."},
            "score": {"type": "integer", "minimum": 0, "maximum": 100,
                      "description": "Độ tin cậy về thông tin của toàn kịch bản (100 = không thấy sai)."},
            "claims": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "quote": {"type": "string", "description": "Trích NGUYÊN VĂN đoạn chứa nhận định trong kịch bản."},
                        "verdict": {"type": "string", "enum": list(VERDICTS)},
                        "reason": {"type": "string", "description": "Vì sao đúng/sai/chưa chắc; thông tin đúng nếu biết."},
                        "fix": {"type": "string", "description": "Câu thay thế đề xuất (để trống nếu không cần sửa)."},
                    },
                    "required": ["quote", "verdict", "reason"],
                },
            },
            "issues": {
                "type": "array",
                "description": "Vấn đề khác: chính tả, câu khó đọc, lặp ý, giật tít, thiếu nguồn, nội dung nhạy cảm.",
                "items": {"type": "object", "properties": {"quote": {"type": "string"}, "problem": {"type": "string"},
                                                           "fix": {"type": "string"}},
                          "required": ["problem"]},
            },
            "corrected_script": {"type": "string",
                                 "description": "Toàn bộ kịch bản đã sửa các lỗi trên (giữ nguyên giọng văn, độ dài)."},
        },
        "required": ["summary", "score", "claims", "corrected_script"],
    },
}

SYSTEM = (
    "Bạn là biên tập viên kiểm chứng thông tin (fact-checker) cho kênh video {kind}. Đọc kịch bản lời đọc, liệt kê MỌI nhận định "
    "có thể kiểm chứng: sự kiện, ngày tháng, con số, tên người/tổ chức/địa danh, trích dẫn, quan hệ nhân quả. Với mỗi nhận định: "
    "dung = chắc chắn đúng theo hiểu biết của bạn; sai = chắc chắn sai (ghi thông tin đúng); chua_chac = có thể đã thay đổi, "
    "mơ hồ, hoặc bạn không đủ chắc; can_nguon = đúng hướng nhưng nên nêu nguồn/diễn đạt thận trọng hơn. Tin thời sự sau thời điểm "
    "dữ liệu của bạn: luôn chua_chac. KHÔNG bịa nguồn. Ngoài ra ghi các vấn đề chính tả, câu khó đọc to, giật tít, thiếu cân bằng. "
    "corrected_script: viết lại toàn bộ kịch bản đã sửa lỗi sai, làm mềm câu chua_chac, giữ ngôn ngữ '{lang}', giọng văn và độ dài. "
    "Trả lời bằng tool report_check, viết bằng tiếng Việt (trừ corrected_script theo ngôn ngữ kịch bản)."
)
KINDS = {"news": "tin tức", "history": "lịch sử", "finance": "tài chính"}


def available() -> bool:
    return bool(settings.usable_connections("deepseek"))


def _call(**kw):
    conns = settings.usable_connections("deepseek")
    if not conns:
        raise RuntimeError("Chưa gắn API DeepSeek: vào ⚙ Cài đặt → mục DeepSeek và dán API key.")
    err = None
    for c in conns:
        model = c.get("model") or "deepseek-v4-pro"
        try:
            resp = build_single_client({**c, "_retries": 2}).messages.create(**{**kw, "model": model})
            try:
                usage_service.record(c, model, getattr(resp, "usage", None))
            except Exception:
                log.debug("usage", exc_info=True)
            return resp
        except Exception as e:      # thử nguồn DeepSeek kế tiếp (nếu có nhiều key)
            err = e
            log.warning("DeepSeek '%s' lỗi: %s", c.get("name"), str(e)[:150])
    raise err


def _clean(data: dict, script: str) -> dict:
    claims = []
    for c in data.get("claims") or []:
        if not isinstance(c, dict) or not (c.get("quote") or "").strip():
            continue
        v = c.get("verdict") if c.get("verdict") in VERDICTS else "chua_chac"
        q = c["quote"].strip()
        claims.append({"quote": q, "verdict": v, "reason": (c.get("reason") or "").strip(),
                       "fix": (c.get("fix") or "").strip(), "found": q in script})
    issues = [{"quote": (i.get("quote") or "").strip(), "problem": (i.get("problem") or "").strip(),
               "fix": (i.get("fix") or "").strip()} for i in data.get("issues") or [] if isinstance(i, dict) and i.get("problem")]
    try:
        score = max(0, min(100, int(data.get("score", 0))))
    except (TypeError, ValueError):
        score = 0
    fixed = (data.get("corrected_script") or "").strip()
    if len(fixed) < len(script) * 0.5:          # model rút gọn quá tay -> không đề xuất thay cả bài
        fixed = ""
    counts = {v: sum(1 for c in claims if c["verdict"] == v) for v in VERDICTS}
    return {"summary": (data.get("summary") or "").strip(), "score": score, "claims": claims, "issues": issues,
            "corrected_script": fixed, "counts": counts}


def check_script(script: str, language: str = "vi", group: str | None = None, topic: str | None = None) -> dict:
    script = (script or "").strip()
    if len(script) < 20:
        raise ValueError("Kịch bản quá ngắn để kiểm tra")
    if len(script) > 60000:
        raise ValueError("Kịch bản quá dài (tối đa ~60 000 ký tự mỗi lần kiểm tra)")
    system = SYSTEM.format(kind=KINDS.get(group or "", "thông tin"), lang=language)
    user = (f"Chủ đề: {topic}\n\n" if topic else "") + "KỊCH BẢN:\n" + script
    resp = _call(max_tokens=min(16000, len(script) // 2 + 4000), system=system, tools=[TOOL],
                 tool_choice={"type": "tool", "name": TOOL["name"]}, messages=[{"role": "user", "content": user}])
    block = next((b for b in resp.content if getattr(b, "type", "") == "tool_use"), None)
    if block is None:
        raise RuntimeError("DeepSeek không trả về kết quả kiểm tra")
    return _clean(block.input or {}, script)
