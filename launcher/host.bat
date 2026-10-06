@echo off
REM Chrome Native Messaging host wrapper (Sprint 4.4).
REM The manifest points here (relative to this folder on Windows), so all
REM paths are resolved relative to %~dp0 - never a developer-specific path.
setlocal
set "ROOT=%~dp0.."
if exist "%ROOT%\backend\.venv\Scripts\python.exe" (
  "%ROOT%\backend\.venv\Scripts\python.exe" "%~dp0native_host.py"
) else (
  python "%~dp0native_host.py"
)
