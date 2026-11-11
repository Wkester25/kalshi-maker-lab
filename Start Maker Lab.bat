@echo off
cd /d "%~dp0"
if not exist .venv (
  echo Setting up the first time...
  python -m venv .venv
  .venv\Scripts\python -m pip install -q -r requirements.txt
)
.venv\Scripts\python run.py %*
pause
