@echo off
REM Launches the actual Electron desktop app (tray + window showing live admin_ui),
REM instead of the static HTML report from run_daily.bat / generate_html.
REM Dev mode: uses desktop/config.json's saved pythonPath (already set to a real
REM Python, not the PATH "python" WindowsApps stub) and this checkout's packages/*.
setlocal
cd /d "%~dp0desktop"
npm start
endlocal
