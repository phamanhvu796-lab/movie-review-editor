from fastapi import APIRouter, HTTPException

from ..services import ffmpeg_install

router = APIRouter(prefix="/api/system")


@router.get("/ffmpeg")
def ffmpeg_state():
    return ffmpeg_install.state()


@router.post("/ffmpeg/install", status_code=202)
def ffmpeg_install_start():
    s = ffmpeg_install.state()
    if s["installed"]:
        return s
    if not s["supported"]:
        raise HTTPException(400, "Tự cài chỉ hỗ trợ Windows. Linux: sudo apt install ffmpeg · macOS: brew install ffmpeg")
    ffmpeg_install.start()
    return ffmpeg_install.state()


# ------------------------------------------------------------------ cập nhật phiên bản online
from pydantic import BaseModel  # noqa: E402

from ..config import settings  # noqa: E402
from ..services import update_service  # noqa: E402


@router.get("/update")
def update_check(force: bool = False):
    return {**update_service.check(force), "download": update_service.state()}


@router.get("/update/status")
def update_status():
    return update_service.state()


@router.post("/update/download")
def update_download():
    try:
        return update_service.start_download()
    except RuntimeError as e:
        raise HTTPException(400, str(e))


@router.post("/update/apply")
def update_apply():
    from ..database import SessionLocal
    from ..models import Project
    with SessionLocal() as db:
        busy = db.query(Project).filter(Project.status.in_(["analyzing", "rendering"])).count()
    if busy:
        raise HTTPException(409, "Đang có dự án phân tích / xuất video. Đợi xong rồi cập nhật để không mất tiến độ.")
    try:
        return update_service.apply()
    except RuntimeError as e:
        raise HTTPException(400, str(e))


class UpdateCfg(BaseModel):
    source: str | None = None
    auto_check: bool | None = None


@router.put("/update/config")
def update_config(body: UpdateCfg):
    kw = {}
    if body.source is not None:
        kw["update_source"] = body.source.strip()
    if body.auto_check is not None:
        kw["update_auto"] = body.auto_check
    settings.update(**kw)
    return update_service.check(force=True)


# ------------------------------------------------------------------ thư mục đầu vào / đầu ra
from ..services import folders, sysinfo  # noqa: E402


class FoldersIn(BaseModel):
    input_dir: str | None = None
    output_dir: str | None = None


@router.get("/folders")
def folders_get():
    return folders.info()


@router.put("/folders")
def folders_put(body: FoldersIn):
    try:
        return folders.set_dirs(body.input_dir, body.output_dir)
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.get("/folders/inputs")
def folders_inputs():
    return {"dir": str(folders.input_dir()), "files": folders.list_inputs()}


class OpenIn(BaseModel):
    which: str = "output"            # input | output | file
    path: str | None = None


@router.post("/folders/open")
def folders_open(body: OpenIn):
    try:
        target = {"input": folders.input_dir(), "output": folders.output_dir()}.get(body.which) or body.path
        if body.which == "file":
            from ..database import SessionLocal
            from ..models import Render
            with SessionLocal() as db:      # chỉ mở file video do app xuất ra (không mở đường dẫn tuỳ ý)
                if not db.query(Render).filter(Render.output_path == body.path).count():
                    raise ValueError("Không phải file do ứng dụng xuất")
        folders.open_in_explorer(target)
        return {"ok": True}
    except ValueError as e:
        raise HTTPException(400, str(e))


# ------------------------------------------------------------------ cấu hình máy & hiệu năng
@router.get("/hardware")
def hardware(force: bool = False):
    return {**sysinfo.scan(force), "mode": sysinfo.perf_mode(), "parallel": sysinfo.parallel_segments(sysinfo.scan().get("encoder")),
            "cpu_threads": sysinfo.cpu_threads()}


@router.get("/monitor")
def monitor():
    return sysinfo.live([str(folders.output_dir()), str(folders.input_dir())])


class PerfIn(BaseModel):
    mode: str


@router.put("/perf")
def perf(body: PerfIn):
    if body.mode not in ("gpu", "max", "eco"):
        raise HTTPException(400, "Chế độ: gpu | max | eco")
    settings.update(perf_mode=body.mode)
    return hardware()
