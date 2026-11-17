# -*- mode: python ; coding: utf-8 -*-
# Build thư mục (dùng cho Setup.exe):  pyinstaller --noconfirm --clean movie_review_editor.spec
# Build 1 FILE portable:               set MRE_ONEFILE=1 && pyinstaller --noconfirm --clean --distpath dist_portable movie_review_editor.spec
#   (bản 1 file: có sẵn FFmpeg; bỏ Whisper/OpenCV cho nhẹ và mở nhanh)
# Ứng dụng cửa sổ (không có cửa sổ đen console). Giao diện chạy trong Microsoft Edge WebView2 qua pywebview.
import os
from PyInstaller.utils.hooks import collect_all, collect_submodules

ONEFILE = os.environ.get("MRE_ONEFILE") == "1"
datas = [("static", "static")]
binaries = []
hiddenimports = (collect_submodules("uvicorn") + collect_submodules("app") +
                 ["sqlalchemy.dialects.sqlite", "multipart", "anyio._backends._asyncio"])
if os.path.isdir("bin"):                  # cả bản 1 file cũng có sẵn FFmpeg (không bắt người dùng cài)
    datas.append(("bin", "bin"))          # ffmpeg.exe, ffprobe.exe
if os.path.isfile("access.json"):
    datas.append(("access.json", "."))    # key bảo mật + mật khẩu cấp sẵn (scripts/make_access.py)

# pywebview + pythonnet (cửa sổ ứng dụng) — bắt buộc trên Windows
for pkg in ("webview", "clr_loader", "pythonnet"):
    try:
        d, b, h = collect_all(pkg)
        datas += d; binaries += b; hiddenimports += h
    except Exception:
        pass
hiddenimports += ["clr", "webview.platforms.winforms", "webview.platforms.edgechromium"]

AI_PKGS = ("scenedetect", "cv2", "faster_whisper", "ctranslate2", "tokenizers", "onnxruntime", "av")
for pkg in (() if ONEFILE else AI_PKGS):
    try:
        d, b, h = collect_all(pkg)
        datas += d; binaries += b; hiddenimports += h
    except Exception:
        pass                              # gói tuỳ chọn chưa cài

icon = "app.ico" if os.path.isfile("app.ico") else None
a = Analysis(["launcher.py"], pathex=["."], binaries=binaries, datas=datas, hiddenimports=hiddenimports,
             excludes=["tkinter", "pytest"] + (list(AI_PKGS) + ["huggingface_hub"] if ONEFILE else []))
pyz = PYZ(a.pure)
if ONEFILE:
    exe = EXE(pyz, a.scripts, a.binaries, a.datas, [], name="MovieReviewEditor", console=False, icon=icon,
              upx=False, runtime_tmpdir=None)
else:
    exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="MovieReviewEditor", console=False, icon=icon)
    coll = COLLECT(exe, a.binaries, a.datas, name="MovieReviewEditor")
