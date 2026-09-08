@echo off
REM Editable install of the 3 packages (order: mail-core -> mail-app -> admin-ui).
REM After this, "python -m mail_app.fetch_mail" / "python -m admin_ui" work from anywhere.
REM Runtime deps: mail-core=none, mail-app=none (needs sibling mail-core), admin-ui=flask.
REM hatchling is a build-time dev tool: if missing, run "python -m pip install hatchling" first.
REM To use a Python that is not on PATH, set PY before running, e.g. set PY=C:\Python312\python.exe
setlocal
set "REPO=%~dp0"
if not defined PY set "PY=python"
"%PY%" -m pip install --no-build-isolation -e "%REPO%packages\mail-core" -e "%REPO%packages\mail-app" -e "%REPO%packages\admin-ui"
endlocal
