@echo off
REM ============================================================
REM  run.bat - Monitor Data912 — app standalone (Edge app-mode)
REM  Levanta el servidor FastAPI en background y abre la UI
REM  en Edge sin barra de navegacion (ventana propia).
REM ============================================================
setlocal
cd /d "%~dp0"

REM --- Verificar Python 3.12 ---
py -3.12 -c "import sys" >nul 2>&1
if errorlevel 1 goto nopy

REM --- Levantar servidor en ventana minimizada ---
echo Iniciando Monitor Data912...
start "Monitor-Data912-Server" /min py -3.12 run.py

REM --- Esperar hasta que el servidor responda (max ~20s) ---
set /a _tries=0
:wait_loop
  set /a _tries+=1
  if %_tries% gtr 20 goto open_anyway
  py -3.12 -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/', timeout=1)" >nul 2>&1
  if not errorlevel 1 goto open_app
  timeout /t 1 /nobreak >nul
goto wait_loop

:open_anyway
echo [WARN] El servidor tardo mas de lo esperado, abriendo de todas formas...

:open_app
REM --- Abrir Edge en modo app standalone ---
powershell -NoProfile -WindowStyle Hidden -Command "$e=@('C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe','C:\Program Files\Microsoft\Edge\Application\msedge.exe') | Where-Object {Test-Path $_} | Select-Object -First 1; if ($e) { Start-Process $e '--app=http://localhost:8000','--window-size=1440,900' } else { Start-Process 'http://localhost:8000' }"
goto :eof

:nopy
echo.
echo [ERROR] No se encontro Python 3.12.
echo         Instala Python 3.12 y corre setup.bat primero.
echo.
pause
