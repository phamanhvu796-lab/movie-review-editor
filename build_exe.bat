@echo off
REM ====================================================================
REM  BUILD MovieReviewEditor.exe  (Windows 10/11)
REM  Cach dung: bam doi vao file nay (hoac: build_exe.bat lite)
REM  Moi buoc deu ghi log vao build_log.txt (cung thu muc voi file nay)
REM  Ket qua : MovieReviewEditor-Portable.exe   (1 FILE - gui cho may khac, bam doi la chay)
REM            backend\dist\MovieReviewEditor\   (ban thu muc: chay build_installer.bat de tao Setup.exe)
REM  LUU Y: KHONG gui rieng file exe trong thu muc dist - no can thu muc _internal di kem.
REM ====================================================================
setlocal EnableExtensions
cd /d "%~dp0backend"
set "ROOT=%~dp0"
set "LOG=%ROOT%build_log.txt"
echo ==== Build %DATE% %TIME% ==== > "%LOG%"

set "MODE=%~1"
if /i "%MODE%"=="full" goto :modeok
if /i "%MODE%"=="lite" goto :modeok
echo.
echo Chon kieu build:
echo   1 = DAY DU : co Whisper + tu phat hien canh (nang ~1GB, build lau, de loi hon)
echo   2 = NHE    : khong Whisper (de build nhat - NEN THU KIEU NAY TRUOC)
choice /c 12 /n /m "Nhap 1 hoac 2: "
if errorlevel 2 (set "MODE=lite") else (set "MODE=full")
:modeok
echo Kieu build: %MODE%  >> "%LOG%"

echo.
echo [1/7] Tim Python 3.10 - 3.12 ...
set "PY="
for %%V in (3.12 3.11 3.10) do if not defined PY (py -%%V -c "import sys" >nul 2>nul && set "PY=py -%%V")
if not defined PY (python -c "import sys; sys.exit(0 if (3,10)<=sys.version_info[:2]<=(3,12) else 1)" >nul 2>nul && set "PY=python")
if not defined PY goto :nopython
echo     Dung: %PY%
echo Python: %PY% >> "%LOG%"

echo [2/7] Tao moi truong ao ...
if not exist .venv\Scripts\python.exe (
  %PY% -m venv .venv >> "%LOG%" 2>&1
  if errorlevel 1 goto :fail
)
call .venv\Scripts\activate.bat

echo [3/7] Cai thu vien (lan dau mat 3-15 phut, cu cho) ...
python -m pip install --upgrade pip >> "%LOG%" 2>&1
pip install -r requirements.txt "pyinstaller>=6.6" "pyinstaller-hooks-contrib>=2024.6" >> "%LOG%" 2>&1
if errorlevel 1 goto :fail
if /i "%MODE%"=="full" (
  pip install -r requirements-ai.txt >> "%LOG%" 2>&1
  if errorlevel 1 (
    echo     ! Khong cai duoc Whisper / phat hien canh. Chuyen sang kieu NHE de van co file exe.
    echo Khong cai duoc requirements-ai.txt, chuyen sang lite >> "%LOG%"
    set "MODE=lite"
  )
)

echo [4/7] Chuan bi FFmpeg ...
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\get_ffmpeg.ps1 >> "%LOG%" 2>&1
if errorlevel 1 (
  echo     ! Khong tai duoc FFmpeg. File exe van duoc build; khi chay app se co nut "Tai FFmpeg tu dong".
)

echo [5/7] Dong goi exe (5-20 phut) ...
REM Nguon cap nhat online: set MRE_UPDATE_REPO=chu-repo/ten-repo truoc khi chay (bo trong = chinh trong Cai dat)
REM Giay phep theo ma may: set MRE_LICENSE_SERVER=http://IP-may-admin:8800  va  set MRE_LICENSE_PUBKEY=<khoa cong khai trong tool Admin>
python -c "import os;e=os.environ.get;open('app/build_info.py','w',encoding='utf-8').write('UPDATE_REPO = %%r\nDIST = %%r\nLICENSE_SERVER = %%r\nLICENSE_PUBKEY = %%r\n' %% (e('MRE_UPDATE_REPO',''),'setup',e('MRE_LICENSE_SERVER',''),e('MRE_LICENSE_PUBKEY','')))" >> "%LOG%" 2>&1
pyinstaller --noconfirm --clean movie_review_editor.spec >> "%LOG%" 2>&1
if errorlevel 1 goto :fail
if not exist dist\MovieReviewEditor\MovieReviewEditor.exe goto :fail

echo [6/7] Tu kiem tra ban exe ...
REM exe la ung dung cua so (khong console): chay va CHO xong, ket qua ghi ra file
if exist "%ROOT%selftest_result.txt" del "%ROOT%selftest_result.txt"
start "" /wait dist\MovieReviewEditor\MovieReviewEditor.exe --selftest "%ROOT%selftest_result.txt"
set "ST=%errorlevel%"
if exist "%ROOT%selftest_result.txt" (type "%ROOT%selftest_result.txt" & type "%ROOT%selftest_result.txt" >> "%LOG%")
if not "%ST%"=="0" (
  echo     ! Tu kiem tra BAO LOI - xem build_log.txt. Ban exe co the chay khong duoc.
) else (
  echo     Tu kiem tra DAT: may chu, giao dien, co so du lieu deu chay.
)

echo [7/7] Tao ban 1 FILE de gui may khac (them 3-10 phut) ...
python -c "import os;e=os.environ.get;open('app/build_info.py','w',encoding='utf-8').write('UPDATE_REPO = %%r\nDIST = %%r\nLICENSE_SERVER = %%r\nLICENSE_PUBKEY = %%r\n' %% (e('MRE_UPDATE_REPO',''),'portable',e('MRE_LICENSE_SERVER',''),e('MRE_LICENSE_PUBKEY','')))" >> "%LOG%" 2>&1
set "MRE_ONEFILE=1"
pyinstaller --noconfirm --clean --distpath dist_portable --workpath build_portable movie_review_editor.spec >> "%LOG%" 2>&1
set "MRE_ONEFILE="
python -c "import os;e=os.environ.get;open('app/build_info.py','w',encoding='utf-8').write('UPDATE_REPO = %%r\nDIST = %%r\nLICENSE_SERVER = %%r\nLICENSE_PUBKEY = %%r\n' %% (e('MRE_UPDATE_REPO',''),'source',e('MRE_LICENSE_SERVER',''),e('MRE_LICENSE_PUBKEY','')))" >> "%LOG%" 2>&1
if exist dist_portable\MovieReviewEditor.exe (copy /y dist_portable\MovieReviewEditor.exe "%ROOT%MovieReviewEditor-Portable.exe" >nul) else (echo     ! Khong tao duoc ban 1 file - xem build_log.txt)

echo.
echo ================= XONG =================
echo GUI CHO MAY KHAC (chon 1):
echo   * %ROOT%MovieReviewEditor-Portable.exe   ^<- 1 file, bam doi la chay
echo   * Setup.exe: chay build_installer.bat     ^<- cai 1 cham, co icon Desktop, tu cap nhat
echo KHONG gui rieng file trong dist\MovieReviewEditor\ (thieu thu muc _internal se bao loi python312.dll).
echo Bam doi file exe: ung dung mo trong cua so rieng (khong can trinh duyet, khong co cua so den).
echo Lan dau mo: tao key bao mat + mat khau. Sau do nhap API key trong Cai dat - tu ket noi.
echo.
if not defined NOPAUSE pause
exit /b 0

:nopython
echo.
echo KHONG TIM THAY PYTHON 3.10 - 3.12 (ban Microsoft Store hoac Python 3.13+ khong dung duoc).
echo Cach 1: mo CMD, go lenh:  winget install Python.Python.3.12
echo Cach 2: tai tai https://www.python.org/downloads/release/python-3128/  roi tick "Add python.exe to PATH"
echo Sau do chay lai file nay. >> "%LOG%"
if not defined NOPAUSE pause
exit /b 1

:fail
echo.
echo ===== BUILD LOI. 30 dong cuoi cua build_log.txt: =====
powershell -NoProfile -Command "Get-Content -Tail 30 '%LOG%'"
echo.
echo Toan bo log: %LOG%
echo Gui file build_log.txt cho nguoi ho tro de duoc sua nhanh.
if not defined NOPAUSE pause
exit /b 1
