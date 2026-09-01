@echo off
REM ============================================================
REM  setup.bat - Instala las dependencias de requirements.txt
REM  con el Python 3.12 del sistema (py -3.12).
REM  Corre UNA vez por PC. Despues usa run.bat.
REM ============================================================
setlocal
cd /d "%~dp0"

echo.
echo == Monitor - Instalacion de dependencias ==
echo.

REM --- Verificar Python 3.12 ---
py -3.12 -c "import sys" >nul 2>&1
if errorlevel 1 goto nopy

echo Interprete encontrado: py -3.12

REM --- Instalar dependencias pinneadas ---
echo Actualizando pip...
py -3.12 -m pip install --upgrade pip
echo Instalando dependencias (requirements.txt)...
py -3.12 -m pip install -r requirements.txt
if errorlevel 1 goto depsfail

echo.
echo == Listo! Dependencias instaladas. ==
echo Para arrancar el monitor: doble-click en run.bat
echo.
pause
exit /b 0

:nopy
echo [ERROR] No encontre Python 3.12 en esta PC.
echo         Instalalo desde https://www.python.org/downloads/
echo         (marca "Add python.exe to PATH" en el instalador) y volve a correr setup.bat
echo.
pause
exit /b 1

:depsfail
echo [ERROR] Fallo la instalacion de dependencias.
echo         Revisa tu conexion a internet y volve a correr setup.bat
echo.
pause
exit /b 1
