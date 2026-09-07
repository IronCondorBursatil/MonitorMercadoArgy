---
name: deps-refresh
description: Regenera requirements.lock desde el freeze validado por el CI (deps-refresh.yml o gate.yml), instala el lock en la laptop y corre el gate. Usar tras un run verde o cuando cambian las cotas de requirements.txt.
disable-model-invocation: true
allowed-tools: Bash(gh run *), Bash(gh workflow *), Bash(py -3.12 scripts/relock.py *), Bash(py -3.12 -m pip show *), Bash(py -3.12 -m pytest *), Bash(pwsh scripts/check.ps1*), Bash(git diff *), Bash(git status*)
---

# /deps-refresh — emparejar la laptop con lo que validó el CI

Objetivo (agents.md §0.3): que el agente desarrolle contra las **mismas versiones** que
instalan producción y el CI. El lock es la foto; este skill la renueva desde un freeze que
la suite ya aprobó en Linux x86 + ARM. Nunca desde un `pip freeze` de Windows (pierde
`uvloop`).

## Pasos

1. **Elegir el freeze.** Último run verde de `deps-refresh.yml` (o de `gate.yml` en `main`):
   `gh run list --workflow deps-refresh.yml --branch main --limit 5`
   Si el último no está verde, **parar**: el lock solo se regenera desde una resolución que
   pasó la suite. Reportar el run rojo con `gh run view <id> --log-failed`.

2. **Bajar el artifact** (`freeze-ubuntu-latest`; el closure aarch64 es idéntico):
   `gh run download <id> -n freeze-ubuntu-latest -D "$env:TEMP\freeze"`
   El artifact **exige autenticación aunque el repo sea público** (verificado 2026-09-07:
   `GET .../artifacts/<id>/zip` sin token → 401). Sin `gh auth`, fallback en este orden:
   a. el freeze `*-despues.txt` más nuevo de `/var/lib/monitor/freeze/` en el servidor
      (`scp monitor-oci:/var/lib/monitor/freeze/<archivo> "$env:TEMP\freeze\"`) — es una
      resolución Linux aarch64 sobre la que la app está corriendo;
   b. `deploy/freeze/prod-*.txt` versionado (la baseline);
   c. **no** intentar la resolución cruzada con `pip install --dry-run --report --platform
      manylinux_*_aarch64`: el 2026-09-07 se colgó en backtracking del resolver (el árbol de
      Jupyter que arrastra optionlab) tras 700 s de CPU sin terminar.
   Aclarar en el reporte cuál camino se usó: el artifact del CI **fue validado por la suite
   en x86 y ARM**; un freeze de prod fue validado por correr en prod, no por la suite.

3. **Ver el drift antes de escribir** (no modifica nada):
   `py -3.12 scripts/relock.py "$env:TEMP\freeze\freeze-ubuntu-latest.txt" --check`

4. **Regenerar el lock** (conserva comentarios y secciones; falla si un paquete del lock
   o del .txt no está en el freeze, o si una versión viola una cota del .txt):
   `py -3.12 scripts/relock.py "$env:TEMP\freeze\freeze-ubuntu-latest.txt" --source "CI deps-refresh run <id>, <fecha>"`
   Revisar `git diff requirements.lock`: solo deben cambiar versiones.

5. **Instalar el lock en la laptop** y verificar las deps sensibles:
   `py -3.12 -m pip install -r requirements.lock`
   `py -3.12 -m pip show fastapi starlette uvicorn numpy pydantic SQLAlchemy httpx | Select-String '^(Name|Version)'`

6. **Gate completo** (`pwsh scripts/check.ps1`). Si rompe algo que no estaba roto, es el
   dato que buscamos: reportar el test y la versión; no debilitar el test. Reversión:
   `git checkout -- requirements.lock` + `py -3.12 -m pip install -r requirements.lock`.

7. **Commit** solo de `requirements.lock`, con el run del CI en el mensaje. Actualizar la
   tabla de versiones de `agents.md §0.3` si cambió una de las sensibles.

## Cuándo NO usarlo

- Con el CI rojo (paso 1).
- Para "arreglar" una cota: las cotas se cambian en `requirements.txt` primero, pasan por
  el CI, y recién después se relockea.
