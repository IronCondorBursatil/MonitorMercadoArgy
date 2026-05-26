@echo off
REM ============================================================
REM  run.bat - Arranca el Monitor con Python 3.12 del sistema.
REM  Requiere tener instalado Python 3.12 (py -3.12 disponible).
REM  Si falta alguna dependencia, corre primero setup.bat.
REM ============================================================
setlocal
cd /d "%~dp0"

py -3.12 run.py
if errorlevel 1 (
  echo.
  echo [ERROR] No se pudo arrancar. Asegurate de tener Python 3.12 instalado.
  echo         Si faltan dependencias, corre setup.bat primero.
  echo.
  pause
)
