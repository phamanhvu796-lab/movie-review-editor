"""Phong cách dựng cho chế độ KỊCH BẢN -> VIDEO (tự tìm ảnh/video minh hoạ trên kho miễn phí bản quyền).

Mỗi phong cách quy định: khung hình, nhịp cắt (giây/cảnh), ưu tiên ảnh hay video, kiểu chuyển cảnh, màu, cỡ phụ đề,
nguồn ảnh ưu tiên, gợi ý tìm kiếm và giọng văn khi AI viết kịch bản.

`needs_human`: phong cách gốc cần người thật trên hình (người dẫn, phỏng vấn, diễn viên). App không tạo được người thật:
nó dựng phần hình minh hoạ (B-roll) + giọng đọc + phụ đề; nếu bạn tải lên video quay người dẫn (loại "Footage"),
app sẽ xen kẽ cảnh người dẫn với hình minh hoạ.
"""

GROUPS = {"news": "📰 Tin tức", "history": "📜 Lịch sử", "finance": "💰 Tài chính"}

# pace = số giây trung bình mỗi cảnh; media = video | photo | mix (mặc định, người dùng đổi được)
_BASE = {"aspect": "16:9", "pace": 4.0, "media": "mix", "transition": "none", "filter": None, "subtitle_size": 54,
         "sources": ["pexels", "pixabay", "wikimedia"], "hint": "", "photo_type": "photo", "tone": "", "max_seconds": 0,
         "needs_human": False, "note": "", "disabled": False, "zoom": 0.12}

STYLES: dict[str, dict] = {
    # ------------------------------------------------------------------ TIN TỨC
    "news_anchor": {"group": "news", "label": "Người dẫn đối diện ống kính (talking head)", "pace": 4.0, "media": "video",
                    "needs_human": True, "transition": "none", "hint": "news, city, people",
                    "tone": "giọng người dẫn bản tin: rõ ràng, khách quan, có 1-2 câu bình luận ngắn ở cuối mỗi tin"},
    "news_short": {"group": "news", "label": "Tin ngắn dọc (vertical short) – dưới 1 phút", "aspect": "9:16", "pace": 2.2,
                   "media": "video", "transition": "flash_cut", "subtitle_size": 78, "max_seconds": 58,
                   "tone": "câu cực ngắn, mở đầu bằng thông tin gây chú ý nhất, tổng dưới 1 phút"},
    "news_field": {"group": "news", "label": "Phóng sự hiện trường", "pace": 4.5, "media": "video", "needs_human": True,
                   "hint": "street, crowd, documentary, handheld",
                   "tone": "kể như phóng viên tại hiện trường: mô tả cảnh, thời gian, địa điểm, dẫn lời nhân chứng (không bịa tên)"},
    "news_reel": {"group": "news", "label": "Tổng hợp tin (newsreel / compilation)", "pace": 3.0, "media": "mix",
                  "transition": "whip_pan", "tone": "nhiều tin ngắn liên tiếp, mỗi tin 2-4 câu, có câu chuyển giữa các tin"},
    "news_explainer": {"group": "news", "label": "Đồ hoạ giải thích (explainer)", "pace": 4.0, "media": "photo",
                       "photo_type": "illustration", "hint": "infographic, map, chart, diagram",
                       "tone": "giải thích vấn đề từng bước, có số liệu cụ thể và so sánh dễ hiểu"},
    "news_live": {"group": "news", "label": "Livestream – đưa tin trực tiếp", "disabled": True,
                  "note": "Livestream là phát trực tiếp, không phải dựng video. Hãy chọn “Tin ngắn” hoặc “Tổng hợp tin”."},
    # ------------------------------------------------------------------ LỊCH SỬ
    "hist_doc": {"group": "history", "label": "Phim tài liệu (documentary)", "pace": 5.5, "media": "mix",
                 "transition": "cross_dissolve", "filter": "vintage_sepia", "sources": ["wikimedia", "pexels", "pixabay"],
                 "hint": "historical, archive, old", "tone": "giọng đọc phim tài liệu: trầm, chậm, giàu hình ảnh, mốc thời gian rõ ràng"},
    "hist_reenact": {"group": "history", "label": "Tái hiện đóng vai (re-enactment)", "pace": 4.5, "media": "video",
                     "needs_human": True, "transition": "fade", "filter": "cinematic_teal_orange", "hint": "costume, medieval, ancient, reenactment",
                     "tone": "kể như đang diễn ra trước mắt, có hành động và cảm xúc nhân vật"},
    "hist_anim": {"group": "history", "label": "Hoạt hình minh hoạ sự kiện, nhân vật", "pace": 4.0, "media": "photo",
                  "photo_type": "illustration", "transition": "fade", "hint": "illustration, drawing, painting",
                  "tone": "kể chuyện sinh động, dễ hình dung, hợp với hình vẽ minh hoạ"},
    "hist_qa": {"group": "history", "label": "Giảng giải / đặt câu hỏi – “Bạn có biết…”", "pace": 3.5, "media": "mix",
                "transition": "flash_cut", "sources": ["wikimedia", "pexels", "pixabay"],
                "tone": "mở đầu bằng câu hỏi “Bạn có biết…?”, trả lời bằng sự thật bất ngờ, có số liệu"},
    "hist_archive": {"group": "history", "label": "Tổng hợp tư liệu cũ (archive montage)", "pace": 4.0, "media": "photo",
                     "filter": "vintage_sepia", "transition": "cross_dissolve", "sources": ["wikimedia", "pixabay", "pexels"],
                     "hint": "archive photo, black and white, historical", "zoom": 0.15,
                     "tone": "ngắn gọn, nhấn vào mốc thời gian và hình ảnh tư liệu"},
    "hist_story": {"group": "history", "label": "Kể chuyện (storytelling)", "pace": 5.0, "media": "mix", "transition": "fade",
                   "filter": "cinematic_teal_orange", "sources": ["wikimedia", "pexels", "pixabay"],
                   "tone": "kể theo nhân vật/sự kiện có mở – thắt – mở nút, tạo tò mò"},
    # ------------------------------------------------------------------ TÀI CHÍNH
    "fin_talk": {"group": "finance", "label": "Đối thoại / giảng giải (talking head + minh hoạ)", "pace": 4.0, "media": "mix",
                 "needs_human": True, "hint": "business, finance, office, chart",
                 "tone": "giải thích khái niệm tài chính bằng ví dụ đời thường, có con số minh hoạ"},
    "fin_faceless": {"group": "finance", "label": "Không lộ mặt (faceless essay)", "pace": 3.5, "media": "video",
                     "transition": "cross_dissolve", "filter": "cinematic_teal_orange", "hint": "money, city, business, technology",
                     "tone": "bài luận cuốn hút, có luận điểm rõ, hook mạnh ở đầu"},
    "fin_anim": {"group": "finance", "label": "Hoạt hình giải thích đầu tư, chứng khoán", "pace": 3.5, "media": "photo",
                 "photo_type": "illustration", "transition": "whip_pan", "hint": "illustration, chart, coin, graph",
                 "tone": "giải thích đơn giản cho người mới, ví dụ bằng số tiền cụ thể"},
    "fin_variety": {"group": "finance", "label": "Tạp kỹ / phỏng vấn (giải trí hoá tài chính)", "pace": 2.8, "media": "video",
                    "needs_human": True, "transition": "flash_cut", "hint": "people, reaction, office, fun",
                    "tone": "vui, nhanh, hỏi – đáp, có câu đùa nhẹ nhưng thông tin chính xác"},
    "fin_short": {"group": "finance", "label": "Tin ngắn tài chính – điểm tin dưới 1 phút", "aspect": "9:16", "pace": 2.3,
                  "media": "video", "transition": "flash_cut", "subtitle_size": 78, "max_seconds": 58,
                  "hint": "stock market, trading, chart",
                  "tone": "điểm tin thị trường: chỉ số, tăng/giảm, nguyên nhân, 1 nhận định — tổng dưới 1 phút"},
    "fin_tutorial": {"group": "finance", "label": "Hướng dẫn thực hành (tutorial)", "pace": 5.0, "media": "mix",
                     "hint": "laptop, spreadsheet, document, calculator",
                     "tone": "hướng dẫn từng bước đánh số (Bước 1, Bước 2…), câu mệnh lệnh rõ ràng"},
    "fin_mv": {"group": "finance", "label": "MV / clip âm nhạc tài chính", "pace": 2.0, "media": "video",
               "transition": "flash_cut", "filter": "vibrant_vlog", "hint": "city night, neon, money, celebration",
               "tone": "lời ngắn có vần, nhịp như bài hát / slogan",
               "note": "Nhạc nền: tải bài hát của bạn ở mục Tài nguyên (loại Nhạc nền)."},
}
for _k, _v in STYLES.items():
    STYLES[_k] = {**_BASE, **_v, "id": _k}
    if _v.get("needs_human") and not _v.get("note"):
        STYLES[_k]["note"] = ("Phong cách gốc có người thật trên hình. App dựng phần hình minh hoạ + giọng đọc + phụ đề; "
                              "muốn có người dẫn: tải video bạn tự quay (loại Footage), app sẽ xen kẽ.")

MEDIA_LABELS = {"photo": "Ảnh", "video": "Video", "mix": "Kết hợp ảnh + video"}


def get(style_id: str) -> dict:
    s = STYLES.get(style_id)
    if not s:
        raise ValueError(f"Không có phong cách '{style_id}'")
    if s["disabled"]:
        raise ValueError(s["note"] or "Phong cách này chưa hỗ trợ")
    return s


def listing() -> list[dict]:
    return [{k: v[k] for k in ("id", "group", "label", "aspect", "media", "needs_human", "note", "disabled", "max_seconds")}
            | {"group_label": GROUPS[v["group"]]} for v in STYLES.values()]
