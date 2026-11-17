@echo off
REM ====================================================================
REM  BUILD tool Admin: admin\dist\MovieReviewEditor-Admin.exe (1 file)
REM  Can Python 3.10 - 3.12. Dung chung moi truong ao voi build_exe.bat neu co.
REM ====================================================================
setlocal
cd /d "%~dp0admin"
if exist "%~dp0backend\.venv\Scripts\activate.bat" call "%~dp0backend\.venv\Scripts\activate.bat"
python -m pip install -r requirements.txt "pyinstaller>=6.6" || goto :fail
pyinstaller --noconfirm --clean admin.spec || goto :fail
echo.
echo XONG: %CD%\dist\MovieReviewEditor-Admin.exe
echo Mo tool Admin -> sao chep "Khoa cong khai" va "Dia chi may chu" -> build lai app khach (xem HUONG_DAN_ADMIN.md)
pause
exit /b 0
:fail
echo BUILD LOI
pause
exit /b 1
