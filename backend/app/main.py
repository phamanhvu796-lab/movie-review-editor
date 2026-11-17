import logging
import shutil
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from .api import auto, connections, edit_plans, projects, renders, system, voice
from .api import assist
from .api import auth as auth_api
from .auth import COOKIE, auth
from .api import settings as settings_api
from .database import SessionLocal, engine
from .models import Base, Project, Render
from .services import license_service, storage_service, update_service
from .utils.paths import data_dir, resource_dir

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


@asynccontextmanager
async def lifespan(_):
    Base.metadata.create_all(engine)
    shutil.rmtree(data_dir() / "work", ignore_errors=True)       # thư mục tạm của lần chạy trước
    storage_service.prune_all()
    license_service.start_background()                            # đăng ký / gia hạn với máy chủ Admin (nền)
    try:
        update_service.cleanup()
    except Exception:
        pass                                   # giữ cache dưới giới hạn (mặc định 2 GB)
    # Job dang dở khi app bị đóng sẽ không bao giờ chạy tiếp: đánh dấu lỗi để UI không kẹt.
    with SessionLocal() as db:
        for r in db.query(Render).filter(Render.status.in_(["queued", "running"])):
            r.status, r.error_message = "failed", "Ứng dụng bị đóng giữa chừng"
        for p in db.query(Project).filter(Project.status.in_(["analyzing", "rendering"])):
            p.status, p.error_message = "draft", "Tác vụ trước bị gián đoạn, hãy chạy lại"
        db.commit()
    yield


app = FastAPI(title="AI Movie Review Video Editor", lifespan=lifespan)


@app.exception_handler(Exception)
async def on_unhandled(request, exc):
    logging.getLogger("mre").exception("Lỗi chưa xử lý ở %s", request.url.path)
    return JSONResponse({"detail": f"Lỗi máy chủ: {type(exc).__name__}: {exc}"}, status_code=500)


@app.middleware("http")
async def require_login(request: Request, call_next):
    """Mọi API (trừ đăng nhập/health) cần phiên đăng nhập hợp lệ; giao diện tĩnh thì tải tự do (không chứa dữ liệu)."""
    path = request.url.path
    if path.startswith("/api/"):
        if path not in auth_api.PUBLIC_PATHS and not auth.check(request.cookies.get(COOKIE)):
            return JSONResponse({"detail": "Cần đăng nhập bằng mật khẩu", "login_required": True}, status_code=401)
        if path not in auth_api.LICENSE_FREE and not license_service.is_valid():
            st = license_service.state()
            return JSONResponse({"detail": st["message"] or "Máy chưa được cấp phép", "license_required": True},
                                status_code=402)
    return await call_next(request)


for r in (auth_api.router, assist.router, projects.router, edit_plans.router, renders.router, voice.router, auto.router, connections.router, system.router, settings_api.router):
    app.include_router(r)
app.mount("/", StaticFiles(directory=str(resource_dir() / "static"), html=True), name="static")
