@echo off
REM Chay thu KHONG can build exe. Tu cai thu vien va tai FFmpeg neu thieu.
setlocal EnableExtensions
cd /d "%~dp0backend"
set "PY="
for %%V in (3.12 3.11 3.10) do if not defined PY (py -%%V -c "import sys" >nul 2>nul && set "PY=py -%%V")
if not defined PY (python -c "import sys; sys.exit(0 if (3,10)<=sys.version_info[:2]<=(3,12) else 1)" >nul 2>nul && set "PY=python")
if not defined PY (
  echo Can Python 3.10 - 3.12. Mo CMD, go lenh: winget install Python.Python.3.12  roi chay lai.
  pause
  exit /b 1
)
if not exist .venv\Scripts\python.exe %PY% -m venv .venv
call .venv\Scripts\activate.bat
pip install -r requirements.txt
if errorlevel 1 ( echo Cai thu vien loi & pause & exit /b 1 )
pip install -r requirements-ai.txt || echo (Bo qua Whisper / phat hien canh - van dung duoc)
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\get_ffmpeg.ps1
python launcher.py
pause
