@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONUTF8=1
if defined UPLOAD_PYTHON goto run_python
python -c "import sys; assert sys.version_info >= (3, 10)" >nul 2>nul
if not errorlevel 1 (
    set "UPLOAD_PYTHON=python"
    goto run_python
)
py -3 -c "import sys; assert sys.version_info >= (3, 10)" >nul 2>nul
if not errorlevel 1 goto run_launcher
if exist "E:\anaconda\python.exe" (
    set "UPLOAD_PYTHON=E:\anaconda\python.exe"
    goto run_python
)
echo Python 3.10 or newer is required. Install Python and enable Add Python to PATH.
pause
exit /b 1
:run_launcher
py -3 "%~dp0scripts\github_upload.py" %*
goto finished
:run_python
"%UPLOAD_PYTHON%" "%~dp0scripts\github_upload.py" %*
:finished
set "UPLOAD_RESULT=%ERRORLEVEL%"
echo.
if not "%UPLOAD_RESULT%"=="0" echo Upload failed. See the message above.
pause
exit /b %UPLOAD_RESULT%
