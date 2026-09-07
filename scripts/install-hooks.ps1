# Instala el hook pre-push local: corre el gate (scripts/check.ps1) antes de cada push y
# aborta el push si está rojo. Es la disciplina que check.ps1 sugería como "opcional" y que
# nunca estuvo instalada (.git/hooks vacío). El CI sigue siendo la red real; esto evita
# pushear rojo y esperar 3 minutos para enterarse.
#
#   pwsh scripts/install-hooks.ps1          # gate completo (~3 min) en cada push
#   pwsh scripts/install-hooks.ps1 -Fast    # ruff + pytest -x (corta en el 1er fallo)
#   pwsh scripts/install-hooks.ps1 -Remove  # desinstala
#
# Detalles que importan (agents.md §0.6): Git for Windows ejecuta el hook con su `sh`, así
# que el archivo tiene que tener finales LF (un CRLF en el shebang da "bad interpreter");
# `exec` propaga el exit code de check.ps1; `< /dev/null` evita que pwsh se coma la lista
# de refs que git manda por stdin; la ruta se resuelve con `git rev-parse --show-toplevel`
# (el cwd del hook es la raíz del árbol, pero el path tiene espacios y se quotea).
param([switch]$Fast, [switch]$Remove)

$ErrorActionPreference = "Stop"
$repo = git rev-parse --show-toplevel
if (-not $repo) { throw "no estoy dentro de un repo git" }
$hooksDir = Join-Path $repo ".git\hooks"
$hook = Join-Path $hooksDir "pre-push"

if ($Remove) {
    if (Test-Path $hook) { Remove-Item $hook; Write-Host "pre-push desinstalado" } else { Write-Host "no había pre-push" }
    exit 0
}

$flag = if ($Fast) { " -Fast" } else { "" }
$body = @"
#!/bin/sh
# Instalado por scripts/install-hooks.ps1 — gate local antes de cada push (agents.md §0.8 Fase 3).
# Para saltearlo UNA vez (p. ej. push de una rama WIP): git push --no-verify
exec pwsh -NoProfile -ExecutionPolicy Bypass -File "`$(git rev-parse --show-toplevel)/scripts/check.ps1"$flag < /dev/null
"@

New-Item -ItemType Directory -Force $hooksDir | Out-Null
# LF obligatorio: -NoNewline + reemplazo explícito, porque Set-Content en Windows escribe CRLF.
[System.IO.File]::WriteAllText($hook, ($body -replace "`r`n", "`n"), [System.Text.UTF8Encoding]::new($false))
Write-Host "pre-push instalado en $hook (gate$flag). Probar: git push --dry-run no lo dispara; un push real sí."
