@echo off
REM 로컬 관리 웹 UI (http://127.0.0.1:5000). 사전에 dev-install.bat 를 한 번 실행했으면
REM PYTHONPATH 없이도 동작한다.
setlocal
set "REPO=%~dp0"
set "PYTHONPATH=%REPO%packages\mail-core;%REPO%packages\mail-app;%REPO%packages\admin-ui;%PYTHONPATH%"
"python" -m admin_ui
endlocal
