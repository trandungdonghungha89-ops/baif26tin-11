@echo off
chcp 65001 >nul
cd /d %~dp0
python -m pip install -r requirements.txt
start "" http://127.0.0.1:5000
python app.py
pause
