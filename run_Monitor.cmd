@echo off
setlocal EnableExtensions
cd /d "%~dp0"

rem --- elige tu intérprete ---
set "PY=python"

rem --- abre NUEVA ventana maximizada y lanza el monitor ---
start "Monitor OKX" /MAX "%PY%" -u Monitor_Estrategias.py

rem Fallback por si START falla (algunos VPS):
rem powershell -NoProfile -ExecutionPolicy Bypass -Command ^
rem   "Start-Process -FilePath '%PY%' -ArgumentList '-u','Monitor_Estrategias.py' -WorkingDirectory '%CD%' -WindowStyle Maximized"
