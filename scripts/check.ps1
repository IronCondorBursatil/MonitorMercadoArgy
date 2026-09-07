# Gate de calidad local (M0.2): ruff + pytest. Falla (exit 1) si cualquiera falla.
#
# Es la corrida LOCAL (Windows) del mismo gate que corre el CI en Linux x86 + ARM
# (.github/workflows/gate.yml → scripts/check.sh) en cada push. Correrlo antes de pushear a
# origin/main (github.com/IronCondorBursatil/MonitorMercadoArgy): el deploy a producción
# (deploy.sh en el servidor Oracle) sale de main. Skips esperados en Windows: 3
# (time.tzset() es sólo Unix); un skip por node es rojo (tests/_skip_guard.py).
#
# Uso:
#   pwsh scripts/check.ps1            # ruff + pytest completo
#   pwsh scripts/check.ps1 -Fast     # ruff + pytest -x (corta en el 1er fallo)
#
# Como git hook pre-push (recomendado): pwsh scripts/install-hooks.ps1 [-Fast]

param([switch]$Fast)

$ErrorActionPreference = "Stop"
$py = "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe"
if (-not (Test-Path $py)) { $py = "py"; $pyArgs = @("-3.12") } else { $pyArgs = @() }

$repo = Split-Path -Parent $PSScriptRoot
Set-Location $repo

$failed = $false

Write-Host "==> ruff check" -ForegroundColor Cyan
& $py @pyArgs -m ruff check .
if ($LASTEXITCODE -ne 0) { $failed = $true; Write-Host "ruff FALLÓ" -ForegroundColor Red }

Write-Host "`n==> pytest" -ForegroundColor Cyan
$pytestArgs = @("-m", "pytest", "tests/", "-q")
if ($Fast) { $pytestArgs += "-x" }
& $py @pyArgs @pytestArgs
if ($LASTEXITCODE -ne 0) { $failed = $true; Write-Host "pytest FALLÓ" -ForegroundColor Red }

if ($failed) {
    Write-Host "`n=== GATE ROJO ===" -ForegroundColor Red
    exit 1
}
Write-Host "`n=== GATE VERDE ===" -ForegroundColor Green
exit 0
