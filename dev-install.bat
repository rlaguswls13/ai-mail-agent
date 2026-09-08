@echo off
REM 개발 설치 — 3개 패키지를 editable 로 설치한다 (mail-core -> mail-app -> admin-ui 순서).
REM 이후 어디서든 `python -m mail_app.fetch_mail` / `python -m admin_ui` 가 동작한다.
REM
REM 런타임 의존성: mail-core=없음, mail-app=없음(형제 mail-core 필요), admin-ui=flask.
REM hatchling(빌드 백엔드)은 dev 도구다 — 없으면 먼저: python -m pip install hatchling
setlocal
set "REPO=%~dp0"
set "PY=python"
"%PY%" -m pip install --no-build-isolation -e "%REPO%packages\mail-core" -e "%REPO%packages\mail-app" -e "%REPO%packages\admin-ui"
endlocal
