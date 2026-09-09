---
name: deploy
description: Deploy a producción (Oracle OCI, monitor-oci) desde main limpio con CI verde — precondiciones duras, ssh deploy.sh, health remoto y diff de freezes. Sólo el usuario lo dispara; el OK previo de David es un momento de intervención (agents.md §0.8).
disable-model-invocation: true
argument-hint: "[--upgrade]"
allowed-tools: Bash(gh auth status*), PowerShell(gh auth status*), Bash(gh run *), PowerShell(gh run *), Bash(git status*), PowerShell(git status*), Bash(git branch *), PowerShell(git branch *), Bash(git rev-parse *), PowerShell(git rev-parse *), Bash(git log *), PowerShell(git log *), Bash(git pull --ff-only *), PowerShell(git pull --ff-only *), Bash(git push origin main), PowerShell(git push origin main), PowerShell(Invoke-RestMethod http://129.80.148.166/*), PowerShell(Invoke-WebRequest http://129.80.148.166/*)
---

# /deploy [--upgrade] — main → producción, con evidencia antes y después

Producción es el servidor Oracle (`monitor-oci`, alias `monitor-do`; 129.80.148.166, usuario
`ubuntu`, repo `/home/ubuntu/MonitorMercadoArgy`, bases en `/var/lib/monitor`). `deploy.sh`
allá hace `git pull origin main` + `pip install -r requirements.txt` + `systemctl restart
monitores.service` + healthcheck, y escribe `pip freeze` ANTES y DESPUÉS en
`/var/lib/monitor/freeze/freeze-<stamp>-{antes,despues}.txt`. Es el ÚNICO camino a prod
(§0.1.6: tocar prod por fuera de `deploy.sh` está prohibido).

**Lo que se deploya es lo que está en `origin/main`**, no la rama local: de ahí las
precondiciones. Cada una tiene su comando y, si falla, se **para** y se reporta.

## 1. Precondiciones (todas, en orden)

**a. `gh` autenticado.** `gh auth login` lo hace David (§0.8), no el agente.

```powershell
gh auth status          # espera "Logged in to github.com account ..." y exit 0
```

Si `gh` no está en el PATH: `& "$env:LOCALAPPDATA\Programs\gh\bin\gh.exe" auth status`. Si
no está autenticado: **parar** y decirlo — sin `gh` no se puede leer el CI, y sin CI no
hay deploy.

**b. Rama `main`, árbol limpio, al día.**

```powershell
git branch --show-current       # tiene que decir main
git status --porcelain          # tiene que estar VACÍO (nada sucio, nada sin trackear)
git pull --ff-only origin main  # si no es fast-forward, parar: main divergió
git rev-parse HEAD              # el sha que se va a deployar
```

**c. CI verde para ESE sha** (`gate.yml`, matrix x86 + ARM, ~2,7 min).

```powershell
gh run list --branch main --workflow gate.yml -L1 --json databaseId,headSha,conclusion,status
```

- `headSha` == HEAD **y** `conclusion == "success"` → seguir.
- `headSha` != HEAD (el push está pendiente):
  ```powershell
  git push origin main
  gh run list --branch main --workflow gate.yml --commit <sha> -L1 --json databaseId,status   # repetir cada ~10 s hasta que aparezca
  gh run watch <databaseId> --exit-status                                                    # timeout de la tool: 600000 ms
  ```
  `--exit-status` hace que el comando salga distinto de 0 si el run falla: ese es el
  criterio, no leer el texto.
- `conclusion` en `failure`/`cancelled`/`timed_out` (o `status` distinto de `completed`):
  **parar**. `gate.yml` tiene `cancel-in-progress`: un run `cancelled` fue superado por
  otro push, no es verde. Detalle: `gh run view <id> --log-failed`.

**d. Recomendado**: si la fase tocó rutas, `/smoke <ruta>` antes del push. El health remoto
no prueba que una ruta nueva exista.

## 2. OK de David

Antes del ssh, mostrar el resumen — sha, run verde (id), `--upgrade` sí/no — y esperar el
OK. El `ask` sobre `ssh` en `.Codex/settings.json` es ese momento: **no está pre-aprobado
a propósito**.

Política de versiones (`docs/decisiones.md` D2, **pendiente de David**):

- sin flag (default histórico): pip da por satisfecho lo instalado; prod sigue siendo la
  foto del último rebuild del venv. Un deploy de código no mueve versiones (salvo una dep
  nueva en `requirements.txt`).
- `--upgrade` (opción A): resuelve DENTRO de las cotas de `requirements.txt`, así prod
  converge a lo que el CI validó ese día. Puede mover versiones — por eso el freeze.

Pasar `--upgrade` sólo si David lo pidió en la invocación.

## 3. Deploy (ssh de **Windows**, desde la tool PowerShell)

`ssh` en PowerShell resuelve a `C:\Windows\System32\OpenSSH\ssh.exe`, que ve el agente de
claves; el de Git Bash NO. Por eso este paso va por la tool PowerShell, nunca por Bash.
Timeout de la tool: 600000 ms (pip install + restart + healthcheck de hasta ~35 s).

```powershell
ssh monitor-oci "cd /home/ubuntu/MonitorMercadoArgy && bash deploy.sh"             # default
ssh monitor-oci "cd /home/ubuntu/MonitorMercadoArgy && bash deploy.sh --upgrade"   # sólo con OK explícito (D2-A)
```

Termina con `>>> Health OK. Despliegue completado.` y exit 0, o `!!! La app NO respondió
/api/health tras el restart` y exit 1. Guardar la línea `freezes: ...`: si el directorio
no se pudo crear, `deploy.sh` los deja en un `mktemp -d` y lo dice ahí.

## 4. Verificación remota

`/api/health` es público; el resto va por HTTP plano (sin dominio no hay TLS; jamás
`-SkipCertificateCheck`, que además está en deny).

```powershell
Invoke-RestMethod http://129.80.148.166/api/health | Format-List status, instruments, is_stale, age_seconds, degraded_loops, loop_crashes_24h
(Invoke-WebRequest http://129.80.148.166/login -TimeoutSec 15).StatusCode     # 200
```

Criterios: `status == "ok"`, `is_stale == false`, `age_seconds` < 30 (el umbral de stale es
6 ciclos × 5 s; recién reiniciado puede venir `null`/`true` hasta que corre el primer refresh:
repetir a los 10-15 s antes de darlo por malo), `instruments > 0`, `degraded_loops` vacío.

**Diff de freezes** (los dos más nuevos = el `antes` y el `despues` de este deploy, mismo
stamp). Comillas **simples** en PowerShell: con dobles, `$(...)` lo expande PowerShell y el
bash remoto recibe vacío.

```powershell
ssh monitor-oci 'cd /var/lib/monitor/freeze && ls -t | head -2'
ssh monitor-oci 'cd /var/lib/monitor/freeze && diff $(ls -t freeze-*-antes.txt | head -1) $(ls -t freeze-*-despues.txt | head -1)'
```

`diff` sale con 1 cuando hay diferencias: es dato, no error. Reportar cada `<` (antes) /
`>` (después) como `paquete: vieja → nueva`. Sin `--upgrade` lo esperado es vacío; con
`--upgrade`, contrastar las deps sensibles (FastAPI/Starlette/uvicorn/numpy/pydantic/
SQLAlchemy/httpx) contra la tabla de `agents.md §0.3` y el `requirements.lock` — si el
servidor resolvió algo distinto de lo esperado, §0.1.10: frenar y preguntar.

## 5. Si el healthcheck falla

```powershell
ssh monitor-oci "journalctl -u monitores.service -n 50 --no-pager"
```

**Parar ahí.** No se reintenta el deploy a ciegas (un segundo `deploy.sh` pisa el freeze
`antes` que sirve para volver). Reportar el log y proponer, **sin ejecutar** hasta tener OK:

- **Rollback de versiones**: dentro del venv del servidor,
  `cd /home/ubuntu/MonitorMercadoArgy && venv/bin/pip install -r /var/lib/monitor/freeze/freeze-<stamp>-antes.txt && sudo systemctl restart monitores.service`
  (el mismo `sudo systemctl restart` que ya usa `deploy.sh`).
- **Rollback de código**: `git checkout <sha-anterior>` en el repo del servidor + restart. Es
  el freno de emergencia: deja el repo en HEAD suelto y el próximo `deploy.sh` vuelve a
  traer `main`. El rollback durable es `git revert` en `main`, CI verde y `/deploy` de
  nuevo.

## Reporte

sha deployado · run del CI (id, verde) · `--upgrade` sí/no · salida de `/api/health` ·
freezes (`antes`/`despues` y el diff, o "sin cambios") · lo que quedó pendiente.
