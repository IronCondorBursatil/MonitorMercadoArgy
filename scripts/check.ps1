# Gate de calidad local (M0.2): ruff + pytest. Falla (exit 1) si cualquiera falla.
#
# No hay CI ni remoto (repo local). Este es el "¿está verde el repo?" canónico —
# correrlo antes de cerrar una branch o mergear a master. Equivale a lo que haría
# un CI gate, ejecutado a mano.
#
# Uso:
#   pwsh scripts/check.ps1            # ruff + pytest completo
#   pwsh scripts/check.ps1 -Fast     # ruff + pytest -x (corta en el 1er fallo)
#
# Opcional: instalarlo como git hook pre-push:
#   "pwsh -File scripts/check.ps1" > .git/hooks/pre-push  (y chmod +x en Git Bash)

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
