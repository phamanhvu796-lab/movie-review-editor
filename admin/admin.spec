# -*- mode: python ; coding: utf-8 -*-
# Build tool Admin thành 1 file:  pyinstaller --noconfirm --clean admin.spec   -> dist/MovieReviewEditor-Admin.exe
from PyInstaller.utils.hooks import collect_all, collect_submodules

datas = [("admin_app/static", "admin_app/static")]
binaries = []
hiddenimports = collect_submodules("uvicorn") + ["admin_app.server", "admin_app.core", "anyio._backends._asyncio"]
for pkg in ("webview", "clr_loader", "pythonnet"):
    try:
        d, b, h = collect_all(pkg)
        datas += d; binaries += b; hiddenimports += h
    except Exception:
        pass
hiddenimports += ["clr", "webview.platforms.winforms", "webview.platforms.edgechromium"]
a = Analysis(["admin_launcher.py"], pathex=["."], binaries=binaries, datas=datas, hiddenimports=hiddenimports,
             excludes=["tkinter", "pytest"])
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, a.binaries, a.datas, [], name="MovieReviewEditor-Admin", console=False, upx=False)
