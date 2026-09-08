@echo off
REM Local admin web UI (http://127.0.0.1:5000). Local-only, no auth: do not expose it.
REM If dev-install.bat was run once, PYTHONPATH is not needed.
REM To use a Python that is not on PATH, set PY before running, e.g. set PY=C:\Python312\python.exe
setlocal
set "REPO=%~dp0"
if not defined PY set "PY=python"
set "PYTHONPATH=%REPO%packages\mail-core;%REPO%packages\mail-app;%REPO%packages\admin-ui;%PYTHONPATH%"
"%PY%" -m admin_ui
endlocal
