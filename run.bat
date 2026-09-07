@echo off
rem rsv-data-router: local start (127.0.0.1:8780 by default)
rem Works once "pip install -e ." has been run; PYTHONPATH also covers a
rem not-yet-installed checkout. bin\RSVData.cfe (from the RSVData project)
rem is optional: the flag is passed only when the file exists.
set "RSVDATA=%~dp0bin\RSVData.cfe"
set "PYTHONPATH=%~dp0src;%PYTHONPATH%"
set "RSVDATA_ARG="
if exist "%RSVDATA%" set "RSVDATA_ARG=--rsvdatabinary "%RSVDATA%""
if exist "%~dp0.venv\Scripts\python.exe" (
    "%~dp0.venv\Scripts\python.exe" -m rsv_data_router %RSVDATA_ARG% %*
) else (
    python -m rsv_data_router %RSVDATA_ARG% %*
)
