@echo off
rem Build dist\RimModManager.exe (single file, no console). Needs Python 3.13 (py launcher or python on PATH).
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo Creating .venv ...
    py -3.13 -m venv .venv 2>nul || python -m venv .venv
    if not exist ".venv\Scripts\python.exe" goto :fail
)
set "PY=.venv\Scripts\python.exe"

echo Installing build requirements ...
"%PY%" -m pip install --disable-pip-version-check -q pyinstaller anthropic pywebview pillow || goto :fail

echo Drawing assets\icon.ico ...
"%PY%" tools\make_icon.py || goto :fail

echo Running PyInstaller ...
rem Hidden imports, excludes and the trimmed Tcl data live in RimModManager.spec.
"%PY%" -m PyInstaller --noconfirm --clean --log-level WARN RimModManager.spec || goto :fail
"%PY%" tools\make_store_assets.py || goto :fail

echo.
echo Built dist\RimModManager.exe
echo Check it with:  start /wait dist\RimModManager.exe --selftest   (report: %%TEMP%%\rmm_selftest.txt)
exit /b 0

:fail
echo.
echo Build FAILED.
exit /b 1
