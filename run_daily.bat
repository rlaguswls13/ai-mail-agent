@echo off
REM 스케줄러가 호출: dry-run 조회 -> 대시보드 HTML 재생성.
REM 사전에 dev-install.bat 를 한 번 실행했으면 PYTHONPATH 없이도 동작한다.
setlocal
set "REPO=%~dp0"
set "PYTHONPATH=%REPO%packages\mail-core;%REPO%packages\mail-app;%REPO%packages\admin-ui;%PYTHONPATH%"
"python" -m mail_app.fetch_mail
"python" -m mail_app.generate_html
endlocal
