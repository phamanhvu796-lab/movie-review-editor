@echo off
REM ====================================================================
REM  Tao bo cai MovieReviewEditor-Setup-<ban>.exe  (can Windows + Python + Inno Setup)
REM  Cach dung: build_installer.bat          (hoi chon kieu build)
REM             build_installer.bat lite     (ban nhe, khong Whisper)
REM  Ket qua : installer_output\MovieReviewEditor-Setup-1.0.0.exe
REM ====================================================================
setlocal EnableExtensions
set "ROOT=%~dp0"
set "NOPAUSE=1"
call "%ROOT%build_exe.bat" %1
if not exist "%ROOT%backend\dist\MovieReviewEditor\MovieReviewEditor.exe" (
  echo Build exe that bai nen khong tao duoc bo cai. Xem build_log.txt
  pause
  exit /b 1
)

set "ISCC=%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
if not exist "%ISCC%" set "ISCC=%ProgramFiles%\Inno Setup 6\ISCC.exe"
if not exist "%ISCC%" set "ISCC=%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe"
if not exist "%ISCC%" (
  echo Chua cai Inno Setup. Mo CMD, go lenh:  winget install JRSoftware.InnoSetup   roi chay lai file nay.
  pause
  exit /b 1
)

if not exist "%ROOT%backend\vc_redist.x64.exe" powershell -NoProfile -Command "$ProgressPreference='SilentlyContinue'; try { Invoke-WebRequest https://aka.ms/vs/17/release/vc_redist.x64.exe -OutFile '%ROOT%backend\vc_redist.x64.exe' -UseBasicParsing } catch { Write-Host 'Khong tai duoc VC++ Redistributable, bo qua' }"

if not exist "%ROOT%backend\MicrosoftEdgeWebview2Setup.exe" powershell -NoProfile -Command "$ProgressPreference='SilentlyContinue'; try { Invoke-WebRequest 'https://go.microsoft.com/fwlink/p/?LinkId=2124703' -OutFile '%ROOT%backend\MicrosoftEdgeWebview2Setup.exe' -UseBasicParsing } catch { Write-Host 'Khong tai duoc WebView2 bootstrapper, bo qua' }"

"%ISCC%" "%ROOT%installer\MovieReviewEditor.iss"
if errorlevel 1 (
  echo Tao bo cai LOI.
  pause
  exit /b 1
)
echo.
echo ============ XONG ============
echo Bo cai: %ROOT%installer_output\
echo Gui file Setup.exe nay cho cac may khac. Nguoi nhan chi can bam doi va bam Next.
pause
