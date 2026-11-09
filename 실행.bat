@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONUTF8=1
where py >nul 2>nul && (set PY=py) || (set PY=python)
echo 공장 한 창을 시작합니다. 이 창을 닫으면 프로그램이 꺼집니다.
start "" "http://127.0.0.1:8780"
%PY% server.py 8780
pause
