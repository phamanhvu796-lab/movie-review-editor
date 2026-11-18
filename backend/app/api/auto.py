"""⚡ Tự động tạo video — MỘT nút cho mọi trường hợp:
- Có footage phim  -> phân tích cảnh -> (vá kịch bản/giọng/phụ đề nếu thiếu) -> Claude lên bản dựng -> xuất video.
- Không có footage -> chế độ Kịch bản → Video: từ kịch bản / SRT / voice MP3, tự tìm ảnh-video miễn phí bản quyền,
  dựng khớp giọng đọc -> xuất video.
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from ..database import get_db
from ..workers import autofill_worker
from .deps import ensure_idle, get_project

router = APIRouter(prefix="/api")


class AutoIn(BaseModel):
    tag_scenes: bool = False
    render: bool = True
    quality: str = Field("final", pattern="^(preview|final)$")   # final = thành phẩm 1080p vào thư mục đầu ra
    allow_spoilers: bool | None = None
    style: str | None = None


@router.post("/projects/{pid}/auto", status_code=202)
def auto(pid: str, body: AutoIn = AutoIn(), db=Depends(get_db)):
    p = get_project(db, pid)
    ensure_idle(p)
    err = autofill_worker.precheck(db, p, body.render)
    if err:
        raise HTTPException(400, err)
    autofill_worker.start(db, p, {"replan": True, "full": True, "render": body.quality if body.render else None,
                                  "tag_scenes": body.tag_scenes, "allow_spoilers": body.allow_spoilers, "style": body.style})
    return {"status": "queued"}
