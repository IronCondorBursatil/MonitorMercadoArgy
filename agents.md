# AGENTS.md — Protocolo IA del Monitor (+ stub de punteros)

**Qué es este archivo hoy** (Fase 4, 2026-09-07): dos cosas y nada más. (1) La sección
`§0 · PROTOCOLO IA` — reglas de trabajo, entorno real, versiones, veredictos de
herramientas, mecánica de Claude Code, hechos medidos, plan de fases y glosario. (2) Un
**stub** al final que mapea cada sección del documento viejo a dónde vive ahora, para que
los tests que citan `agents.md › "sección"` en sus docstrings sigan teniendo destino. Las
**convenciones financieras** (CER, TAMAR, settlement, day-counts, MD, accrued, goldens,
precisión, schema de campos) están en `docs/convenciones-financieras.md`; la arquitectura
actual, en `CLAUDE.md` y `docs/`.

**Orden de autoridad**: `CLAUDE.md` › `agents.md §0` › `docs/*`. Un hecho vive en UN solo
archivo; los demás apuntan (§0.1.4).

**Historia**: los changelogs v6.1–v7.2, el dashboard viejo (SPA `app.js` + layout
arrastrable), los endpoints `/api/*`, CACHE, TROUBLESHOOTING y el resto del documento viejo
no se perdieron: viven en
git (`git log -p agents.md`; último estado completo en `git show c0dda9d:agents.md`). No se
re-copian acá.

---

## §0 · PROTOCOLO IA — biblia de herramientas, versiones, mediciones y prácticas (2026-09-07)

> **Para quién**: cualquier agente de IA que trabaje en este repo (Claude Code, Codex, Copilot,
> Cursor, Gemini u otro). **Leerla ENTERA antes de** proponer una herramienta, tocar
> dependencias, configurar el agente (permisos/hooks/skills), reescribir documentación o
> ejecutar el plan de fases. Recién después aplicar conceptos.
> **Orden de autoridad**: `CLAUDE.md` › **esta sección** › resto de `agents.md` (donde el resto
> contradiga a esta sección o a `CLAUDE.md`, el resto es histórico y pierde).
> **Origen**: auditoría del 2026-09-07 sobre HEAD `7da0557` (13 agentes sobre repo +
> candidatas, 6 verificadores contra la doc oficial de Claude Code, verificación en vivo de
> producción y del CI). Todo dato de acá tiene fecha; si cambia, se corrige **acá** y no en
> N archivos (la regla anti-drift de §0.1).
> **Nota para Codex/otros**: buscan `AGENTS.md` en mayúsculas; en Windows el nombre
> `agents.md` resuelve igual, en Linux no. Claude Code **no** auto-carga este archivo: lo
> encuentra por el puntero de `CLAUDE.md`.

### 0.1 Reglas de trabajo (cómo opera cualquier IA en este repo)

1. **Corrección y seguridad antes que velocidad.** Un cambio rápido que rompe pricing o
   pierde datos del catálogo cuesta más que una sesión entera.
2. **Evidencia, siempre.** Cada conclusión se rotula **HECHO OBSERVADO** (archivo:línea,
   comando + salida, doc oficial con URL), **HIPÓTESIS** (plausible, falta comprobar) o
   **DESCONOCIDO** (falta acceso/dato). Lo que no se pudo verificar se marca "no verificado";
   nunca se completa de memoria. Fecha real de hoy > fecha de corte del modelo.
3. **Antes de sumar una herramienta**, comparar en este orden y elegir la más simple que
   alcance: **A** mantener lo actual · **B** configurar mejor una función nativa · **C** una
   instrucción, test o skill chica del proyecto · **D** herramienta externa. Nada entra por
   popularidad ni por estrellas; se verifica doc oficial, licencia (núcleo ≠ servicio),
   permisos, qué transmite, costo total, mantenimiento, solapamiento, cómo se desinstala.
   No confundir: código visible con open source · gratis con servicio gratis · compatibilidad
   declarada con paridad · menos salida con menos costo · más agentes/contexto con mejor
   resultado.
4. **Una sola fuente de verdad por hecho.** Infraestructura → `deploy/README-ops.md`;
   versiones y veredictos de herramientas → esta sección; invariantes → `CLAUDE.md`;
   convenciones financieras → `docs/convenciones-financieras.md` (desde la Fase 4; este archivo
   solo conserva el stub de punteros). Los demás archivos **apuntan**, no copian.
   Motivo: toda afirmación duplicada en N archivos se actualizó en N-1 (check.ps1 "No hay CI",
   CLAUDE.md "DigitalOcean", "~2330 tests").
5. **Modo auditoría** es el default cuando se pide analizar/evaluar: no instalar, no modificar
   archivos del proyecto, no commitear, no deployar, no tocar bases ni servicios. Se puede
   leer, correr comandos read-only y consultar producción con GET.
6. **Autorización explícita** (por escrito, en el momento) para: instalar cualquier cosa;
   editar `~/.claude/settings.json` o crear `.claude/settings.json`/`hooks/`/`skills/`/
   `rules/`; tocar `requirements.txt`, el lock, `gate.yml`, `deploy.sh`, `check.ps1`,
   `.gitignore`; reescribir `CLAUDE.md`/`agents.md`; commit/push; deploy.
   **Prohibido salvo OK puntual de David**: `bypassPermissions`; `--force`; reescribir
   historia; tocar prod por fuera de `deploy.sh`; rotar secretos; agregar MCPs; instalar
   herramientas no listadas como ADOPTAR/PROBAR en §0.4.
7. **Una mejora por vez.** Comprobar el estado del repo, registrar fallos preexistentes,
   aplicar la config mínima, correr el gate, revisar el diff (nada fuera de alcance),
   documentar uso y reversión, informar el resultado real. Olas chicas: ≤10 archivos o un
   subsistema por commit, con review adversarial **por tema** antes de cerrar. Evidencia:
   cada ola grande (35 archivos/+3698) produjo el mismo día una cola de fix-del-fix (7 pares
   documentados, de 1 minuto a 6 h 49 m).
8. **Prueba por mutación.** Un test que no se pone rojo al revertir el fix que dice cubrir es
   decorativo. Está admitido dos veces en el historial (`0bb77d5`: "la medición que lo dio
   por bueno probaba otra cosa"; `95380b2`: "tests que pasaban igual con el código roto").
   Se revierte el fix, se exige rojo, se restaura.
9. **Paso "compound" al cerrar cualquier fase/feature/bugfix** (antes de pedir merge):
   qué invariante o lección quedó **materializada** como test guardián, hook/permiso o línea
   de `CLAUDE.md`; qué memoria se actualizó; qué doc obsoleta se corrigió. Los errores se
   repitieron entre sesiones exactamente mientras vivieron solo en mensajes de commit:
   "cero = dato ausente" tres veces (floor de precios, `ccp` de FCI, `tem` de letras) y
   "perder/duplicar el escalón de settlement" tres veces (`d9412ec` + dos intra-sesión en
   `5322f33`). Cesaron cuando se volvieron docstring-contrato + tests guardianes.
10. **Frenar y preguntar** si: falla un test fuera de la lista conocida; falla un deploy; un
    hook o permiso bloquea trabajo legítimo; hay indicio de secreto expuesto; el CI resuelve
    versiones distintas a las esperadas.
11. **No debilitar** tests, tipos, validaciones ni controles de seguridad para que algo pase.
12. **Contexto y salidas.** Recortar salidas largas (`pytest -q`, `--collect-only`,
    `Select-Object -Last N`, redirigir al scratchpad y leer el fragmento) — pero nunca con una
    compresión *lossy* automática sobre el gate: un traceback recortado esconde justo la
    línea que importa. Delegar exploración pesada a subagentes de contexto fresco.
13. **Proceso**: Superpowers (brainstorm → spec → plan → TDD → review) con **spec tipo
    delta** (especificar solo el cambio). `/security-review` antes de pushear cualquier
    cambio en auth o routers; `/code-review` al cerrar cada fase. Prioridad: `CLAUDE.md` >
    skills > defaults del agente.
14. **Dominio financiero — controles obligatorios en cualquier cambio de pricing/ingesta**:
    unidades y escala (el VCP de FCI viene por cada 1.000 cuotapartes; un `×1000` ya pasó);
    moneda por sufijo D/C/— (`core/domain/currency.py`, única fuente); fechas en ART naive
    (`apply_timezone`, el droplet corre en UTC); `today()` del dominio (`clock.py`,
    `MONITOR_AS_OF` solo tests; fecha fija de tests 2026-06-10 en `tests/_clock.py`);
    **float de punta a punta, sin Decimal** (tolerancias canónicas: equivalencia 1e-7 rel,
    golden TIR 1,5 bp, solver |NPV| < 1e-4, forma cerrada ≤ 1e-9 rel); tasas TEM/TNA/TEA con
    las conversiones de `conventions.py` (TAMAR k = 365/32; MD con m=12 en TAMAR/DUAL);
    settlement T+1 para todos → lag CER 10 hábiles → spread → max de rieles (contrato en
    `pricing/tamar.py`, roto dos veces); un `0` o `≤0` de una fuente externa es **dato
    ausente**, no un valor; toda referencia externa (golden) lleva **procedencia** (fuente,
    fecha, captura).

### 0.2 Entorno real (máquina de David) — hechos verificados 2026-09-07

- **Superficie**: Claude Code como extensión de VSCode, Windows 11, shell primaria
  PowerShell 7 + Bash (Git Bash). La **tool PowerShell está activa**: una regla de permisos
  `Bash(...)` NO cubre el mismo comando lanzado por la tool PowerShell (ver §0.5).
- **Config del agente**: modelo `claude-fable-5[1m]`, `effortLevel: xhigh`; plugin
  **Superpowers 5.1.0** (instalado 2026-05-27; publicado **6.3.0** el 2026-08-12 — pendiente
  de actualizar; los marketplaces de terceros no auto-actualizan); **Playwright MCP** único
  MCP (scope usuario, `npx @playwright/mcp@latest`, 24 tools; paquete 0.0.80 vigente, sin
  renombres pendientes); memoria auto en
  `C:\Users\david\.claude\projects\c--Users-david-OneDrive-Monitores---Data912\memory\`
  (19 archivos + `MEMORY.md`; el *slug* está atado a la **ruta** del repo, ver §0.5);
  **sin** `gh`, **sin** hooks, **sin** skills/rules/commands de proyecto, sin `.mcp.json`,
  sin `.claude/settings.json` versionado (solo `settings.local.json`), sin `CLAUDE.local.md`.
- **Python**: `py -3.12` = `%LOCALAPPDATA%\Programs\Python\Python312\python.exe`. Sin venv
  dentro del proyecto (OneDrive + límite de 5 MB). El repo vive en OneDrive
  (`C:\Users\david\OneDrive\Monitores - Data912`) — mudarlo a `C:\dev\monitor` es decisión
  pendiente de la Fase 0 y exige migrar el slug de memoria (§0.5).
- **Git**: `origin = git@github.com:IronCondorBursatil/MonitorMercadoArgy.git`, trunk `main`,
  repo público; `core.sshCommand` apunta al `ssh.exe` de Windows (el de Git Bash no ve el
  agente de claves); la clave de la cuenta IronCondorBursatil usa `IdentitiesOnly` (hay otra
  cuenta con lectura sola: si ssh la ofrece primero, el push da 403).
- **Producción**: Oracle OCI, Ampere A1 **ARM aarch64**, Ubuntu 24.04, IP **129.80.148.166**,
  usuario `ubuntu`, repo `/home/ubuntu/MonitorMercadoArgy`, venv creado por `deploy.sh`,
  Python **3.12.3**, `systemd monitores.service` (User=ubuntu, puerto 8000 local), nginx en
  :80 **HTTP sin TLS** (`cookie_secure=False` a propósito), `MONITOR_DB_DIR=/var/lib/monitor`
  por drop-in de systemd + `/etc/profile.d/monitor.sh` (**NO** lo hereda `ssh host 'cmd'`;
  con `set -u` un `$MONITOR_DB_DIR` pelado aborta). Alias ssh **`monitor-do` y `monitor-oci`
  son el MISMO host Oracle** desde 2026-09-07 (el droplet DigitalOcean 157.230.87.79 está
  apagado; toda mención a "droplet" en docs viejas se lee "servidor Oracle"). Deploy:
  `ssh monitor-oci` → `cd /home/ubuntu/MonitorMercadoArgy && bash deploy.sh` (git pull + `pip
  install -r requirements.txt` **sin `--upgrade`** + restart + healthcheck `/api/health`).
- **CI**: `.github/workflows/gate.yml` — matrix `ubuntu-latest` + `ubuntu-24.04-arm`, instala
  `requirements.txt` (abierto) **a propósito** para cazar drift antes que prod, corre
  `bash scripts/check.sh`, `concurrency: cancel-in-progress`, timeout 25 min. Estado
  verificado por API: **3 runs, 3 verdes, 2,6–2,75 min cada uno** (último `7da0557`,
  2026-09-05). Los logs por job requieren auth → por eso `gh` es ADOPTAR.
- **Gate local**: `pwsh scripts/check.ps1` (ruff + pytest; `-Fast` = `-x`). `check.sh` es el
  gemelo Linux (`--fast`, `--install-dev`). `check.ps1:3` dice "No hay CI": **falso**, corregir
  en Fase 4. Pre-push hook: **no instalado** (`.git/hooks` solo samples).
- **Salud pública de la app**: `GET /api/health` (sin auth; `apps/web/app.py:818-850`)
  devuelve `status` (ok/degraded), `is_stale` (umbral `refresh_sec×6` = 30 s),
  `age_seconds`, `last_refresh` (ART **naive**: comparar `age_seconds`, no timestamps),
  `degraded_loops`, `loop_crashes_24h`, `catalog{instruments,orphans,defaulted,seed_failed}`.
  Verificado en vivo: 200, `age_seconds 4.17`, 1160 instrumentos, 701 métricas, orphans 1,
  defaulted 158. **No** expone `last_error` ni tickers (política: es público). El conjunto de
  rutas públicas está fijado por `tests/test_aud_G_tests_route_auth.py:31` (`_PUBLIC_PATHS` =
  `/login`, `/logout`, `/api/health` + mount `/static`): **no crear otro endpoint de salud**.

### 0.3 Paquetes y versiones — la verdad a 2026-09-07

**Hay TRES conjuntos de versiones, no dos:**

| Paquete | Laptop (= `requirements.lock`, foto 2026-06) | **Prod** (`pip freeze` real 2026-09-07) | CI | Cota propuesta para `requirements.txt` |
|---|---|---|---|---|
| fastapi | 0.136.3 | **0.141.1** | resolución del día | `>=0.141,<0.150` |
| starlette | 1.1.0 | **1.6.0** | ídem | `<2` |
| uvicorn[standard] | 0.48.0 | **0.52.4** | ídem | `>=0.52,<1` |
| numpy | 2.4.6 | 2.5.2 | ídem | `<3` |
| pydantic | 2.13.4 | 2.13.5 | ídem | `>=2,<3` |
| SQLAlchemy | 2.0.50 | 2.0.52 | ídem | `>=2,<2.1` |
| httpx | 0.28.1 | 0.28.1 | ídem | (sin drift) |
| pandas | 2.3.3 | 2.3.3 | ídem | **no poner cota propia**: `optionlab` la capa en `<3` |
| holidays | 0.44 | 0.44 | ídem | ídem: `optionlab` la capa en `<0.45` |
| scipy | (ver lock) | 1.18.1 | ídem | — |
| Dev (no van a prod) | pytest 9.0.3 · hypothesis 6.155.2 · ruff 0.15.16 | — | mismos | pineados en `requirements-dev.txt` |

**Actualización 2026-09-07 (Fase 1, rama `fase-1-drift`)**: la columna "Laptop" de la tabla
describe el estado ANTERIOR. Ese día `requirements.txt` recibió las cotas de la última
columna, `requirements.lock` se regeneró desde el freeze de prod con `scripts/relock.py`
(20 pins actualizados, `uvloop==0.22.1 ; sys_platform != 'win32'` agregado) y la laptop se
instaló desde él: **laptop = prod** en todas las sensibles (fastapi 0.141.1, starlette 1.6.0,
uvicorn 0.52.4, numpy 2.5.2, scipy 1.18.1, pydantic 2.13.5, SQLAlchemy 2.0.52, httpx 0.28.1,
optionlab 1.8.5). Gate local con esas versiones: 2631 passed / 3 skipped. Aviso de pip sin
efecto en el Monitor: `ccxt` (paquete global ajeno) pide `certifi==2026.6.17`.

Hechos (estado previo a la Fase 1, conservado como evidencia):

- `requirements.txt`: 24 deps de runtime, **21 completamente abiertas**, 2 con cota inferior
  (`SQLAlchemy>=2`, `pydantic>=2`), **1** con cota superior (`bcrypt<4.0.0`, el precedente).
- `requirements.lock`: lista **curada a mano**, 26 pins, **sin environment markers**, sin
  closure (~110 transitivas libres); `uvloop` y `colorama` sin pin **a propósito** (comentario,
  no marker). Header fechado 2026-06, regeneración manual sin cadencia ni tooling (no hay
  pip-tools ni uv en uso; no hay `pyproject.toml`). El entorno local **ya divergió** del lock:
  `certifi` 2026.6.17 vs 2026.2.25, `requests` 2.34.2 vs 2.33.1.
- Prod = **foto congelada del último rebuild del venv** (2026-09-04, migración a Oracle)
  porque `deploy.sh` instala sin `--upgrade` y pip da por satisfecho lo instalado.
  CI = resolución fresca en cada push. Laptop = lock viejo. Ninguno de los tres coincide.
- **`optionlab` 1.8.5** capa `pandas<3.0.0` y `holidays<0.45,>=0.44` (por eso pandas 3.0.5,
  publicado 2026-07-22 con wheels aarch64, **no entra**) y arrastra `jupyter<2` como
  dependencia dura → prod instala ~60 paquetes del ecosistema Jupyter sin usarlos. Un freeze
  completo los pinea. Pendiente fuera de alcance: `--no-deps`/vendorizar.
- **Incidente de referencia (2026-09-04)**: FastAPI 0.141 cambió `include_router` (envuelve en
  `_IncludedRouter` sin `.path`/`.dependant`; deps del include en
  `include_context.dependencies`) → `app.routes` pasó de 66 rutas con `.path` a 4 de 21 y el
  guard "toda ruta exige login" quedó **ciego** en prod. Arreglado con `tests/_routes.py`
  (`iter_app_routes`, agnóstico a la versión) en `a154eac`; el CI corre verde con 0.141.1.
  **Esperado hoy al subir la laptop a 0.141.1: 0 roturas.** Regla: cualquier test que recorra
  `app.routes` usa `tests/_routes.py`.
- El único chequeo `.txt`↔`.lock` automatizado compara **extras**, no versiones, y saltea los
  paquetes sin extras (`tests/test_aud_F_ops_deploy_lock.py:75-80`).
  `tests/test_rem_R5_ops_tests_lock_extras.py:110` exige `httptools` del lock == instalado local.
- Apuntar `deploy.sh` al lock **quedó refutado** (`docs/plan-optimizacion-2026-08-31.md:92,316`,
  citado por `deploy.sh:70-72` y `gate.yml:9-11`). El esquema "abierto en prod + CI que
  instala lo mismo" es deliberado; lo que falta es emparejar la laptop.

**Reglas de dependencias (prácticas a aplicar en la Fase 1):**

1. **Cotas superiores** en `requirements.txt` para las deps sensibles a forma (tabla), sin
   contradecir los caps de `optionlab`. Nunca `==` en el `.txt` (rompe el diseño abierto).
2. **Regenerar el lock desde el freeze del runner x86 del CI** (el closure x86 == aarch64,
   verificado) con curación de **5 markers**: `uvloop`, `pexpect`, `ptyprocess`
   `; sys_platform != 'win32'` y `colorama`, `pywinpty` `; sys_platform == 'win32'`;
   conservar header/comentarios y `httptools` igual al local. Un `pip freeze` hecho en
   **Windows no sirve** (pierde `uvloop`). Alternativa de un comando:
   `uv pip compile requirements.txt --universal --python-version 3.12 -o requirements.lock`
   (emite markers; **no verificado** si `uv` está instalado).
3. **Test de paridad**: todo paquete del `.txt` existe en el lock con `==` y el pin satisface
   el especificador del `.txt` (`packaging.specifiers`), sin saltear paquetes sin extras.
4. **El CI publica lo que validó**: partir el step de instalación en `requirements.txt` →
   `pip freeze > freeze-${{ matrix.runner }}.txt` → `requirements-dev.txt` (si no, el freeze
   arrastra pytest/ruff) y subirlo con `actions/upload-artifact` (nombre único por matrix).
   `cancel-in-progress` implica que un run cancelado no deja artifact.
5. **`deploy.sh`**: `pip freeze` antes y después de instalar a
   `"${MONITOR_DB_DIR:-/var/lib/monitor}/freeze/freeze-$(date +%Y%m%d-%H%M%S).txt"`
   (StateDirectory de `ubuntu`, ya aloja `backups/`). Nunca `$MONITOR_DB_DIR` pelado
   (`set -u`). Política `--upgrade` dentro de cotas = decisión pendiente (A recomendada:
   prod converge a la resolución que el CI validó; el freeze antes/después hace el rollback
   un comando).
6. **Cadencia**: workflow `deps-refresh.yml` semanal (resuelve el `.txt` fresco en ambas
   arquitecturas, corre la suite, publica el freeze) + skill `/deps-refresh` que regenera el
   lock local y corre el gate. Dependabot solo para versiones de GitHub Actions (pip solo si
   el lock resulta neutral de plataforma). Cotas sin cadencia = drift diferido.
7. **Al desarrollar**: verificar la API contra la versión **instalada** (`py -3.12 -m pip show
   <pkg>`) y contra los release notes del repo oficial, no contra la doc "latest" (motivo
   por el que Context7 no sirve acá: FastAPI no tiene snapshots versionados).

### 0.4 Herramientas evaluadas — veredictos (no volver a proponer las descartadas)

| Herramienta | Veredicto | Motivo verificado (2026-09-07) |
|---|---|---|
| **GitHub CLI (`gh`)** | **ADOPTAR** (Fase 0) | Único modo de leer el veredicto del CI desde la terminal. `winget install --id GitHub.cli --source winget`; `gh auth login` por browser (scopes `repo`, `read:org`, `gist`; **no** fine-grained PAT: `gh run watch` no lo soporta); `gh run watch --exit-status` gatea el deploy, `gh run view <id> --log-failed` diagnostica. MIT, gratis, cero contexto residente. |
| **Superpowers 5.1.0 → 6.3.0** | **ADOPTAR** (Fase 0) | `claude plugin update superpowers` (o `/plugin`). 6.2.0 arregló el hook SessionStart en Windows y comprimió skills; 6.3.0 batchea tareas chicas. **Efecto secundario**: 6.x escribe scratch en `.superpowers/sdd/` dentro del árbol → agregar a `.gitignore` (hoy no está). Ya está también en el marketplace oficial de Anthropic. |
| **Hooks + permisos nativos** | **ADOPTAR** (Fase 2) | `.claude/settings.json` versionado con `allow/ask/deny` + hooks `PreToolUse`. Reemplaza al plugin `hookify` (hace lo mismo empaquetado; innecesario para 3–4 reglas). |
| **pyright-lsp** (plugin oficial Anthropic) | **PROBAR** (Fase 6) | El repo no tiene type-check. Diagnósticos por edición sin correr nada. Baseline con `pyright` standalone primero (modo basic sobre `core/domain` + `apps/web`); conservar solo si ≥1 defecto real por tarea y <10 diagnósticos/turno. Requiere `pyright-langserver` aparte. Riesgos: ruido, watchers + OneDrive. |
| **Context7** | **POSTERGAR** | Verificado contra su API: FastAPI **sin snapshots versionados**, Pydantic solo v1.10, SQLAlchemy solo 1.4 → serviría doc *latest*, exactamente la versión equivocada respecto del lock. Backend cerrado, contenido community sin garantía, queries almacenadas. Free tier alcanzaría, pero no resuelve el problema observado. |
| **Chrome DevTools MCP** | **POSTERGAR** (ad hoc) | Diferencial real (performance traces, Lighthouse, heap snapshots) pero 57 tools de contexto y manda URLs a CrUX salvo `--no-performance-crux`. Playwright MCP cubre UI + consola + network. Solo ante sospecha de leak de memoria del dashboard SSE. |
| **security-guidance** (oficial) | **POSTERGAR** | Review continuo por hook en cada edición = costo por turno. Alternativa C: regla "`/security-review` antes de pushear auth/routers" con la skill ya presente. |
| **pip-audit** | **POSTERGAR** | No es problema observado; step `continue-on-error` en `gate.yml` cuando se lo toque (Fase 3). `sonatype-guide` (MCP) sería desproporcionado. |
| **Ralph / ralph-wiggum** | **POSTERGAR** | Loop desatendido choca con TDD supervisado y con la equivalencia bit-a-bit. Solo para un backfill mecánico de bajo riesgo, algún día. |
| **Serena** | **DESCARTAR** | Su propia doc admite adherencia "drastically reduced" con Claude Code y pide pisar el system prompt + hooks; procesos residentes, `.serena/` dentro del árbol (OneDrive), telemetría opt-out; promete ventaja solo en repos "larger and more complex". Grep/Glob/subagentes + el mapa de `CLAUDE.md` alcanzan. |
| **GitHub MCP Server** | **DESCARTAR** | ~93 tools / 17–55k tokens de definiciones por request (issue oficial #1286); modo local exige Docker. `gh` hace lo mismo con cero contexto. |
| **claude-mem** | **DESCARTAR** | Duplica la memoria nativa que ya funciona; API HTTP local en :37777 **sin autenticación** en una máquina con credenciales BYMA y secreto JWT. |
| **Beads · Task Master · Context Mode · RTK · Repomix** | **DESCARTAR** | Dev solo con memoria nativa + `docs/superpowers/`; RTK comprime *lossy* la salida de pytest y es Unix-first; Repomix solo para llevar un subsistema a otro LLM (`npx repomix --include core/domain/pricing/**`, sin instalar). |
| **OpenSpec · Spec Kit · BMAD · CCPM · SuperClaude · Claude Flow/Ruflo · ECC · Oh My ClaudeCode · GSD · Compound Engineering** | **DESCARTAR** | Todos activos, todos solapan 1:1 con Superpowers + plan mode; dos pipelines de planificación = dos fuentes de verdad. Claude Flow es un enjambre (lo contrario de lo que pide la equivalencia). GSD tiene problema de gobernanza. **Ideas rescatables gratis**: *delta spec* (OpenSpec), paso *compound* (Compound Eng.), hooks de seguridad de ECC como lectura, "contexto principal magro, lo pesado a subagentes" (GSD). |
| **Continue · Vibe Kanban · Aider · Cline · OpenCode** | **DESCARTAR** | Continue muerto (adquirido, repo read-only, jun-2026); Vibe Kanban sin empresa (abr-2026); Aider sin commits desde may-2026; Cline/OpenCode son reemplazos, no complementos (ediciones cruzadas sobre el mismo árbol). |
| **Catálogos** (wshobson/agents, VoltAgent, skills.sh, aitmpl, awesome-copilot) | **DESCARTAR** como instalación | Un `python-pro` genérico no sabe que `tem: 0` es dato ausente. Usar como biblioteca de lectura antes de escribir un skill propio. skills.sh: 670k entradas sin curación = superficie de supply chain. |
| **logfire · mattpocock-skills · pr-review-toolkit · claude-security** | **DESCARTAR** | No cubren un hueco real (observabilidad propia existe; TDD/review lo cubren Superpowers + skills ya presentes). No existe plugin oficial de pytest ni de FastAPI (verificado contra el `marketplace.json` completo). |

Qué se pierde si no se adopta: sin `gh`, se sigue pusheando a ciegas y no se sabe contra qué
versiones probó el CI; sin actualizar Superpowers, más tokens por skill (no crítico); sin
hooks/permisos, `on.js` sigue editable a mano y `git checkout --` sin prompt; sin el piloto de
pyright, una clase de errores (atributos, `None`) que hoy solo atrapa un test que pase por la
rama — si el piloto no muestra un defecto real por tarea, no se pierde nada.

### 0.5 Mecánica de Claude Code verificada contra la doc oficial (2026-09-07)

Fuente: `code.claude.com/docs/en/{permissions,settings,settings-reference,hooks,hooks-guide,
skills,memory,plugins-reference,commands,debug-your-config}`. Lo que no está acá se verifica
antes de afirmarlo.

**Settings y permisos**
- Precedencia (mayor → menor): managed › `--settings`/flags › `.claude/settings.local.json` ›
  `.claude/settings.json` (versionable, para commitear) › `~/.claude/settings.json`. Las listas
  `permissions.allow/ask/deny` se **mergean** entre archivos. En Windows, `settings.local.json`
  queda junto a `settings.json` (dentro de `.claude/`).
- Evaluación **deny → ask → allow**, primer match gana, la especificidad **no** altera el
  orden: un `ask` prompta aunque haya un `allow` más específico; un deny de cualquier nivel
  bloquea un allow de cualquier otro. Los `allow` del `.claude/settings.json` recién aplican
  tras aceptar el trust dialog; deny/ask aplican siempre. Deny aplica también en
  `bypassPermissions`; allow no tiene efecto ahí.
- **Bash**: forma canónica `Bash(git add *)`; `:*` equivale solo como sufijo (`Bash(git:*
  push)` trata el `:` como literal); `*` puede ir en cualquier posición y matchea cualquier
  texto (incluidos espacios); sin `*` = match exacto; `Bash(ls *)` matchea también `ls` pelado
  solo si es el único wildcard. Comandos compuestos (`&&`, `||`, `;`, `|`, `&`, newline):
  para **allow** cada subcomando debe matchear; para **deny/ask** basta que uno matchee
  (incluso en subshell/`$(...)`). Patrones que restringen argumentos son "fragile" según la
  doc: `Bash(git push --force *)` no matchea `git push -f`. Un `*` antes del subcomando en un
  allow dispara warning al arrancar.
- **PowerShell**: reglas propias `PowerShell(Remove-Item *)`, misma forma que Bash, alias
  canonicalizados (`ls`/`gci`/`dir` → `Get-ChildItem`), case-insensitive, cada subcomando de
  `|`/`;`/`&&`/`||` debe matchear para allow. **Una regla `Bash(...)` no cubre la tool
  PowerShell**: duplicar las reglas críticas.
- **Archivos**: `Read`/`Edit` usan sintaxis gitignore. `Read(.env)` ≡ `Read(**/.env)`
  (cualquier profundidad bajo el cwd); `Read(./.env)` = solo el del cwd (receta oficial:
  `Read(./.env)`, `Read(./.env.*)`); `Edit(apps/web/static/js/on.js)` válido (relativo al cwd).
  Prefijos: `//` absoluto desde la raíz del FS, `~/` home, `/path` relativo al **archivo de
  settings** (no es absoluto), `path` o `./path` relativo al cwd. Windows normaliza a POSIX
  (`//c/**/.env`). **`Edit(path)` cubre Edit, Write y NotebookEdit; una regla `Write(path)` se
  acepta pero nunca se consulta y emite warning** (v2.1.210+). Un deny `Read(path)` bloquea
  también Edit/Write en ese path (v2.1.208+).
- **Los deny `Read`/`Edit` SÍ aplican dentro de Bash** a `cat`, `head`, `tail`, `sed` y a los
  targets de redirecciones (`< file`, `> file`). **No** aplican a subprocesos arbitrarios (un
  script Python/Node que abre el archivo); para eso, sandbox.
- MCP: `mcp__<server>` (todo el server), `mcp__<server>__<tool>`; en allow el glob solo tras el
  prefijo literal `mcp__<server>__` (`mcp__*` en allow se descarta con warning; en deny/ask
  vale). Ejemplo: `mcp__playwright__browser_navigate`, `mcp__playwright__*`.
- Warning al arrancar para reglas deny/ask con nombre de tool inexistente (salvo nombres con
  `_` o `*`); reglas malformadas van al diálogo de settings inválidos y a `claude doctor`.
  `/permissions` lista reglas y archivo de origen.
- `bypassPermissions`: solo se activa al lanzar (`--permission-mode bypassPermissions` ≡
  `--dangerously-skip-permissions`) o desde `~/.claude/settings.json` (desde project/local no
  aplica, v2.1.257+); se bloquea con `permissions.disableBypassPermissionsMode: "disable"`.
  **Prohibido en este repo** (§0.1.6).

**Hooks**
- Config en `.claude/settings.json` (commiteable) o `~/.claude/settings.json`; scripts en
  `.claude/hooks/`. `"matcher": "Bash|PowerShell"` es **lista de nombres exactos** (se evalúa
  como regex solo si contiene otros caracteres). La doc recomienda matchear ambos porque la
  tool PowerShell existe.
- Campo **`if`** con sintaxis de permisos (`"if": "Edit(**/on.js)"`) — **best-effort**: si
  Claude Code no puede determinar qué comandos corre un Bash, el hook corre igual; para
  imponer un allow/deny duro usar el sistema de permisos. No admite `|` de tools; solo aplica
  en eventos de tool (PreToolUse/PostToolUse/PostToolUseFailure/PermissionRequest/
  PermissionDenied).
- **Forma exec**: `{"type":"command","command":"py","args":["-3.12","${CLAUDE_PROJECT_DIR}/.claude/hooks/guard.py"]}`
  — sin shell, cada `args` es un argumento literal; el campo `shell` se ignora. Forma shell
  (sin `args`): Git Bash en Windows, o PowerShell si no hay Git Bash.
  `${CLAUDE_PROJECT_DIR}` = raíz donde arrancó la sesión (no cambia al entrar a un worktree).
- Salida de un `PreToolUse`: JSON en stdout
  `{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny","permissionDecisionReason":"..."}}`
  con **exit 0**; `permissionDecision` ∈ allow/deny/ask (`reason` solo se muestra en deny).
  **Exit 0 sin salida = sin decisión** (sigue el flujo normal; no aprueba). **Exit 1 NO
  bloquea** (error no bloqueante). **Exit 2 bloquea siempre** (aunque el JSON diga allow),
  mensaje por stderr. Una ruta mal escrita en **forma shell** → exit 127 → no bloqueante, el
  gate queda "silently disabled" (solo un aviso `<hook> hook error` en la transcripción).
  **Pero en forma exec con un intérprete** (`"command": "py", "args": [..., "guard.py"]`)
  es el intérprete el que sale, y `python` sale con **exit 2** cuando no encuentra el script
  → **bloquea TODO Bash/PowerShell** hasta que el archivo exista (observado 2026-09-07 en la
  Fase 2: ~10 min sin shell por escribir `settings.json` antes que `guard.py`). Orden
  correcto: primero el script, después el settings; y probar cada hook con una llamada
  deliberada tras configurarlo.
- Tipos: `command`, `http`, `mcp_tool`, `prompt`, `agent` (experimental). Timeouts default:
  600 s command/http/mcp_tool, 30 s prompt, 60 s agent. `/hooks` es un visor **read-only**;
  los cambios en settings los toma el file watcher sin reiniciar. Debug: `claude --debug` o
  `/debug`.

**Skills, comandos y plugins**
- Los custom commands se **fusionaron** con los skills: `.claude/commands/x.md` y
  `.claude/skills/x/SKILL.md` crean el mismo `/x`; si coinciden, gana el skill; commands **no**
  está deprecado, pero la doc recomienda skills (soportan archivos auxiliares). Se crean skills.
- Frontmatter (todos opcionales, `description` recomendado): `name`, `description`,
  `when_to_use`, `argument-hint`, `arguments`, **`disable-model-invocation`** (solo el usuario
  lo dispara; además saca la descripción del contexto y evita el preload en subagentes —
  usar para `/deploy`), `user-invocable`, **`allowed-tools`** (**pre-aprueba** durante el
  turno, **no restringe**; para restringir, `disallowed-tools`; ambos se limpian al siguiente
  mensaje; nombres MCP completos `mcp__playwright__browser_navigate`, el wildcard
  `mcp__x__*` no está documentado dentro del skill), `model`, `effort`, `context: fork`,
  `agent`, `background`, `hooks`, `paths`, `shell` (`bash`|`powershell`), `metadata`.
- Carga: al inicio solo el listado nombre + descripción (truncado a 1.536 chars); el cuerpo
  entra como un único mensaje al invocarse (por el usuario o por Claude) y persiste en turnos
  siguientes. Los skills en `skills:` de un subagente se precargan enteros.
- Plugins: `claude plugin update <plugin>` (o `<plugin>@<marketplace>`; scope default `user`);
  en sesión `/plugin`, `/plugin marketplace update <marketplace>`, `/reload-plugins`.
  Marketplaces oficiales de Anthropic: auto-update **ON**; terceros y locales: **OFF** por
  default (por eso Superpowers quedó en 5.1.0).
- `/context [all]`: desglose de tokens por categoría (system prompt, tools, MCP tools,
  subagentes, memory files = CLAUDE.md/rules/auto memory, skills, mensajes). `/usage` (`/cost`
  es alias) para consumo. `/doctor` (v2.1.206+) propone recortes de `CLAUDE.md`.

**CLAUDE.md, rules y memoria**
- Objetivo oficial: **"target under 200 lines per CLAUDE.md file"** (límite duro 4 MiB;
  "longer files consume more context and reduce adherence"). Este repo: **373 líneas**.
  Excluir lo que Claude puede derivar del código (árboles de directorios, descripciones
  archivo por archivo, dependencias); conservar pitfalls, rationale y convenciones que
  difieren del default.
- **`@imports` cargan al inicio** (hasta 4 saltos): organizan, **no ahorran contexto**.
  Prohibido migrar contenido a `@imports` para "aliviar". Lo que sí carga bajo demanda:
  `CLAUDE.md` en subdirectorios (al leer archivos de ahí), `.claude/rules/*.md` con `paths:`,
  y skills.
- **`.claude/rules/<tema>.md`** con frontmatter `paths:` (lista YAML de globs entre comillas,
  `**` cruza directorios, `{a,b}` expande). Sin `paths` cargan **siempre** (como CLAUDE.md
  partido). Con `paths` cargan **cuando Claude LEE un archivo que matchea**: editar uno
  existente las dispara (Edit exige Read previo); **crear un archivo nuevo con Write NO las
  dispara** (issues #23478, #38487, #63142, cerradas *not planned*). Las convenciones de
  creación (headers, plantillas, nombres) van en `CLAUDE.md` o en un skill.
- `CLAUDE.local.md`: se carga después del `CLAUDE.md` del mismo directorio, para preferencias
  personales; **no se gitignorea solo** (agregarlo a `.gitignore` a mano); no se propaga
  entre worktrees.
- **Memoria auto**: `~/.claude/projects/<slug>/memory/`, donde `<slug>` = ruta absoluta de la
  raíz del repo con todo carácter no alfanumérico → `-` (acá
  `c--Users-david-OneDrive-Monitores---Data912`). Está atada a la **ruta**, no al repo:
  mover o renombrar la carpeta deja huérfanos memoria, transcripts y el entry de trust/MCP en
  `~/.claude.json` (issues #41344, #52494, *not planned*; la doc no cubre migración, solo
  `claude project purge`). **Antes de mudar el repo**: fijar
  `"autoMemoryDirectory": "C:/Users/david/.claude/memory-monitores"` en settings (opción
  documentada, cualquier scope) y/o copiar `~/.claude/projects/<slug-viejo>/` al slug nuevo;
  volver a confiar la carpeta; `.claude/settings.local.json` viaja con el árbol;
  `claude --resume <id>` encuentra sesiones cross-project (v2.1.223+).

### 0.6 Hechos verificados del repo que condicionan cualquier plan

- **Tests**: 2.625 en 198 archivos (`--collect-only` 4,7–10 s); último gate local
  `2622 passed / 3 skipped` (los 3 = `_solo_unix` de `test_timezone.py`); runner ARM
  `2445 / 32 skipped` (29 son los tests node de `fci.js` que se saltean **en silencio** por
  falta de node + otros). **La cuenta de skips depende del entorno** (8 en una shell sin
  bash): asertar por *reason* esperado por plataforma, no por número. Distribución: dominio
  1.282 · infra 506 · web 425 · ops 139 · meta 88. Hypothesis solo en
  `test_pricing_invariants.py`. TestClient en 38 archivos. Cero tests de browser.
- **Calidad estática**: `ruff.toml` solo `F,E,W` (sin `I`, `B`, `UP`, `S`; sin `format`);
  **sin** mypy/pyright; **sin** coverage; sin `pytest.ini`/`pyproject.toml` (marker `noauth`
  registrado en `conftest.py`).
- **Documentación**: `CLAUDE.md` 373 líneas / 36 KB ≈ **8,9k tokens por sesión**, ~28 %
  invariantes críticos y ~72 % procedural/histórico. `agents.md` (estado 2026-09-07, Fase 4):
  quedó como **§0 + stub de punteros**; las convenciones financieras viven en
  `docs/convenciones-financieras.md` y el documento viejo (534 líneas / 69 KB: ~16 KB de
  convenciones vigentes + ~51 KB histórico/refutado) está en git (`git show c0dda9d:agents.md`).
  **Los tests no pinean el contenido de `CLAUDE.md`**; 9 tests citan `agents.md › sección` en
  docstrings (resuelto por el stub del final). **Afirmaciones obsoletas a corregir (Fase 4)**:
  `scripts/check.ps1:3` "No hay CI"; `CLAUDE.md` "DigitalOcean/droplet" (~8 veces, prod es
  Oracle ARM); "~2330 tests" (son 2.625); `CLAUDE.md:233` ubica `letras_sync` en
  `core/domain/` (vive en `core/infrastructure/letras_sync.py`); `CLAUDE.md:332` "los otros 5
  loops" (son 4); las tres de `agents.md` (la línea «el Excel master como fuente de verdad»
  —**falsa**: SQLite es la verdad, Excel semilla—, el stack web viejo con su base «analytics»
  inexistente y `apps/web/server.py`) **ya se retiraron** en la Fase 4. Redundancias: regla
  txt-en-server/lock-en-local en 5 archivos; bootstrap del admin en 4; invariante `on.js` en 2.
- **`.gitignore`**: ignora **`.claude/` entero** (línea 69) → nada de `.claude/settings.json`,
  `hooks/`, `skills/`, `rules/` entraría a git; cambiar a `.claude/settings.local.json`.
  No tiene `.superpowers/`. Sí tiene `.playwright-mcp/`, `.worktrees/`, `worktrees/`.
- **Permisos actuales**: global 179 `allow`, 0 `deny`, 0 `ask` — 19 de otro proyecto ("Calcu
  Online Claudio"), 4 con typo que nunca matchean, 28 del puerto 5000, ejecución arbitraria
  (`Bash(python)`, `Bash(python3 -)`, `Bash(python -c:*)`, `Bash(pip install:*)`), 8 `curl -k`,
  `Stop-Process -Id 38872,35856 -Force` con PIDs fijos. Proyecto: `Bash(git checkout *)`
  (cubre `git checkout -- archivo`, destructivo) y `Bash(py -3.12 -c ' *)`.
- **`on.js`**: lo genera `scripts/build_on_static.py` desde `apps/web/on_src/` con
  `write_text` (por Bash, no por Edit); **determinista** (reconstruido en scratch: byte-idéntico
  al trackeado). Un deny `Edit(apps/web/static/js/on.js)` es seguro. El guard actual de
  sincronía (`test_on_ratings_badge.py:202-210`) solo cubre el fragmento del badge; un test que
  regenere a tmp y compare bytes sería el guard fuerte.
- **`.env`**: solo lo leen procesos Python en runtime (`config/settings.py::_load_dotenv`,
  `core/infrastructure/byma/credentials.py`, `scripts/backup_bundle.py`); ningún flujo
  documentado exige que el agente lo abra → deny `Read(.env)` no rompe nada.
- **Calendario de feriados**: en runtime **100 % offline** — `core/holiday_engine.py::_ar_holidays()`
  lee `data/feriados_ar.xlsx` (trackeado; 207 filas, 197 fechas tras filtrar `xbue_pmc`),
  cobertura 2020–2029, fallback XBUE local fuera de cobertura. Las 5 fuentes por red solo las
  toca `refresh()`/`descargar_todos()` (y **reescriben el xlsx**): jamás llamarlas en tests.
  Hash del set derivado 2020–2029 (`sha256('\n'.join(sorted(iso)))`) hoy =
  `56aed962fb13dc47164060044ce7a1dab7deede6f02a0e1c959c12a3d5ad2818`; no hashear los bytes
  del xlsx (un re-save cambia metadata).
- **Motor legacy (`tests/_legacy_engine.py`)** comparte **a propósito** el solver
  `_xirr_from_years`, `pricing.metrics` y `days_30_360` con producción (docstring :45-54: la
  copia del solver crasheaba por overflow en CUAP; sin las mismas year-fractions AO28D
  divergiría). Cobertura independiente de esos tres: `test_xirr_solver.py` (17 yields
  conocidos × 4 day-counts, `abs=1e-6`), `test_golden_referencia.py` (extensión de stub
  ISMA contra verdad de mercado: CLISA 17,03 % vs 17,47 % sin ella), `test_cashflow_synth.py`
  (30/360 a mano). **No copiar esos símbolos al legacy.** Hueco real: `test_daycount.py:107-120`
  es tautológico (`THIRTY_360` llama a `days_30_360`) → agregar valores 30/360 a mano para
  31→30 y 29-feb.
- **Goldens externos**: 13 ONs hard-dollar + 2 dólar-linked + 2 LECAP contra "la calculadora
  de referencia" (sin procedencia registrada). **0 para CER, 0 para TAMAR**: la validación
  IAMC de TTJ26 (precio 158,20 → V.Téc 146,39 / payback 164,32 / TIR EA 39,06 %) vive solo en
  docstrings (`pricing/tamar.py:4-5`). Candidato BONCER: **TX28** (ISIN ARARGE3209X6, emisión
  2020-09-04, vto 2028-11-09, cupón 2,25 %, 5 flujos restantes); **no TX26** (vence 2026-11-09,
  un solo flujo). `cer_base = 22.5439510896` = CER BCRA del 2020-08-21 (= emisión − 10
  hábiles, verificado contra la API v4.0 variable 30). Lo que falta es el **corte externo**
  (fecha + precio + CER del día + TIR/paridad publicada por IAMC/BYMA).
- **Retrabajo**: 175 commits en 12 días activos; `apps/web/app.py` tocado en 45 de los
  últimos 150; 7 pares fix-del-fix el mismo día; bug latente más viejo `timeout=None` en
  `async_http.py` (~100 días, sobrevivió 2 reviews del archivo, causó el incidente del
  2026-09-01: 22 h sirviendo el mismo snapshot); 0 reverts; 5 rondas de auditoría.

### 0.7 Mediciones de referencia (baseline, 2026-09-07)

| Medida | Valor | Cómo se midió |
|---|---|---|
| Tests colectados | 2.625 / 198 archivos | `py -3.12 -m pytest tests/ --collect-only -q` |
| Gate local | 2622 passed / 3 skipped | mensaje del commit `7da0557` |
| CI | 3 runs, 3 success, 2,57–2,75 min | `api.github.com/repos/.../actions/runs` |
| Muestra de timing local | 328 tests en 6,0 s | `test_xirr_solver.py` + `test_panels_router.py` |
| `CLAUDE.md` | 373 líneas · 36.008 B · ≈8,9k tokens | `Measure-Object -Line`; chars/4 |
| `agents.md` (antes de §0) | 534 líneas · 69.114 B | ídem |
| Permisos globales | 179 allow · 0 deny · 0 ask | `ConvertFrom-Json ~/.claude/settings.json` |
| Memoria auto | 19 archivos, ~46 KB | listado del directorio |
| `ruff check .` | 0 violaciones (ruff 0.15.16) | `--statistics` |
| Prod `/api/health` | 1160 instrumentos, 701 métricas, age 4,17 s | GET en vivo |
| Freeze de prod | fastapi 0.141.1 · starlette 1.6.0 · uvicorn 0.52.4 · numpy 2.5.2 · pydantic 2.13.5 · SQLAlchemy 2.0.52 · httpx 0.28.1 · scipy 1.18.1 · pandas 2.3.3 · holidays 0.44 | `ssh monitor-oci venv/bin/pip freeze` |
| Calendario | 197 fechas 2020–2029, hash `56aed962…2818` | `_ar_holidays()` offline |
| Pendientes de medir en Fase 0 | duración de la suite completa en Windows; `/context` con el `CLAUDE.md` actual | — |

**Telemetría que NO existe**: tokens/costo por tarea. Se declara, no se estima. Lo observable:
gate rojo/verde, corridas del CI, commits fix-del-fix, intervenciones humanas, tool calls en
los transcripts (`~/.claude/projects/<slug>/`).

### 0.8 Plan de ejecución (brief v2, corregido) — una fase = una rama = un PR

Reglas del plan: la fase siguiente no arranca hasta que el PR anterior esté mergeado con CI
verde en x86 y ARM leído con `gh run watch --exit-status`; olas chicas y review adversarial
por tema; un bug fuera de alcance se anota en el PR y no se corrige ahí (salvo que bloquee);
cierre de fase = paso *compound* (§0.1.9); reporte en la descripción del PR (qué cambió,
evidencia comando+salida por criterio, criterios no cumplidos, pendientes). David interviene
en: decisiones de la Fase 0, `gh auth login`, revisión/merge de cada PR, OK previo a cada
deploy.

**Fase 0 — Línea de base y decisiones (sin cambios funcionales).**
Instalar `gh` + `gh auth login` (va acá, no en la Fase 1: la regla de cierre lo necesita).
`.gitignore`: reemplazar `.claude/` por `.claude/settings.local.json`; agregar `.superpowers/`.
Actualizar Superpowers a 6.3.0 y verificar en sesión nueva. Backup fechado de
`~/.claude/settings.json`. Mediciones a `docs/baseline-2026-09.md` (suite local completa,
skips por pata, `/context`, `/doctor`, freeze de prod → `deploy/freeze/prod-2026-09-07.txt`
como baseline único versionado, lista de las deps sensibles). Decisiones de David a
`docs/decisiones.md`: (1) mudar el repo fuera de OneDrive — recomendado sí, **con**
`autoMemoryDirectory` fijado antes y copia del slug; (2) política de deploy — A `--upgrade`
dentro de cotas con freeze antes/después (recomendada) o B `--rebuild` explícito; (3) rotar
secreto JWT (`/var/lib/monitor/jwt_secret`, migrado tal cual) y admin por defecto si siguen
sin rotar, con OK explícito; (4) canal de la alerta de staleness (email de GitHub por
workflow fallido = cero infraestructura). Aceptación: baseline archivada; Superpowers 6.3
operativo; decisiones registradas.

**Fase 1 — Drift de versiones.** *(Estado 2026-09-07: ejecutada en `fase-1-drift`; queda
pendiente la tabla de versiones del CI porque exige `gh auth login`. Mecanismo del lock:
`scripts/relock.py` + tests + skill `/deps-refresh`; fuente usada: freeze de prod.)*
Primer entregable: `gh run view <id> --log` de las corridas →
tabla laptop / CI / prod de las deps sensibles (cierra el único DESCONOCIDO). Cotas
superiores (tabla §0.3). Lock regenerado desde el freeze x86 del CI + markers (regla §0.3.2);
target = resolución del CI. Test de paridad (§0.3.3). `gate.yml` con freeze artifact (§0.3.4).
`deploy.sh` con freeze antes/después a `${MONITOR_DB_DIR:-/var/lib/monitor}/freeze/` y la
política decidida. `deps-refresh.yml` semanal + `/deps-refresh`. **Corte**: 0 roturas
esperadas al subir la laptop a FastAPI 0.141.1; más que 0 → parar y reportar. Aceptación:
gate local verde con el lock nuevo (2.625 tests, skips = los esperados); CI verde en ambas
patas; tras un deploy el freeze de prod coincide con el lock en las deps sensibles; el test
de paridad se pone rojo al sacar un pin (probar y revertir). Reversión: `git revert`;
`winget uninstall GitHub.cli`.

**Fase 2 — Permisos, hooks y skills.** Principio: reglas duras en `permissions.deny/ask`;
hooks como segunda capa (best-effort). Global: purgar a <20 entradas cross-proyecto (fuera:
las de otro proyecto, typos, puerto 5000, PIDs fijos, `curl -k`, python/pip pelados).
Proyecto `.claude/settings.json` (versionado): `allow` = `Bash(py -3.12 -m pytest *)`,
`Bash(py -3.12 -m ruff *)`, `Bash(pwsh scripts/check.ps1*)`, `Bash(git status*)`,
`Bash(git diff *)`, `Bash(git log *)`, `Bash(git add *)`, `Bash(git commit *)`,
`Bash(git push origin *)`, `Bash(gh run *)`, `Bash(gh pr *)`; `ask` = `Bash(git checkout *)`,
`Bash(git restore *)`, `Bash(git reset *)`, `Bash(git clean *)`, `Bash(git stash drop *)`,
`Bash(pip install *)`, `Bash(py -3.12 -c *)`, `Bash(python *)`, `Bash(ssh *)` **y sus gemelos
`PowerShell(...)`**; `deny` = `Read(./.env)`, `Read(./.env.*)`, `Edit(apps/web/static/js/on.js)`
(**sin** `Write(...)`), `Bash(git push --force*)`, `Bash(git push -f*)`, `Bash(curl -k *)`,
`Bash(curl --insecure *)`, `PowerShell(Invoke-WebRequest * -SkipCertificateCheck*)`.
Absorber las 5 entradas de `settings.local.json`. Hooks `PreToolUse` en forma exec
(`py -3.12 .claude/hooks/guard.py`) con matcher `Bash|PowerShell` y otro con matcher
`Edit|Write` + `if: Edit(**/on.js)`; `guard.py` cubre solo lo que los permisos no expresan:
force-push por argv (`--force`, `-f`, `--force-with-lease`, refspec `+`), `Get-Content`/
`Set-Content`/`type` sobre `.env`/`on.js` y `-SkipCertificateCheck` en la tool PowerShell;
responde con JSON `permissionDecision` + `hookEventName` y exit 0, o exit 2; nunca exit 1.
Probar cada regla con una llamada deliberada (una ruta mal escrita deja el hook apagado en
silencio). Skills en `.claude/skills/<nombre>/SKILL.md`: `/gate`, `/smoke` (protocolo del
puerto 8001 dentro del skill), `/deploy` (`disable-model-invocation: true`; solo desde `main`
limpio: push → `gh run watch --exit-status` → ssh deploy.sh → `/smoke` → diff de freezes),
`/deps-refresh`, `/compound`; `allowed-tools` con nombres completos. Aceptación: edición de
`on.js` bloqueada por permiso y por hook; `git checkout -- archivo` pregunta; `/gate` y
`/smoke` corren; `/hooks` lista los hooks; sesión nueva sin errores de hook. Reversión:
`git revert` + restaurar el backup del settings global.

**Fase 3 — CI y verificación.** *(Estado 2026-09-07: ejecutada en `fase-3-ci` (c0dda9d +
hotfix e354fc7, CI verde x86+ARM con los 29 tests node corriendo en ARM). Guard de
skips = `tests/_skip_guard.py` cableado en `conftest.py` (motivos, no cuenta; probado con
un skip `node` deliberado → exit 1). Lección del hotfix: el guard matcheaba cualquier
"tzset" y denunció el skip legítimo `_solo_windows` en Linux — el gate de Windows no podía
verlo; se diagnosticó sin logs por la API pública de jobs. Pre-push instalado en esta máquina con
`scripts/install-hooks.ps1`; `staleness.yml` escrito — la prueba "alerta disparada y
recibida" queda para David tras el merge, porque los `schedule`/`workflow_dispatch`
corren desde `main`.)* `actions/setup-node` en ambas patas (los 29 tests node de
`fci.js` corren en ARM) + `pytest -rs` con lista de *reasons* esperados por plataforma (falla
ante uno nuevo). Pre-push hook con instalador `scripts/install-hooks.ps1`; contenido de
`.git/hooks/pre-push` (finales **LF**):
`#!/bin/sh` / `exec pwsh -NoProfile -ExecutionPolicy Bypass -File "$(git rev-parse --show-toplevel)/scripts/check.ps1" -Fast < /dev/null`
(el CI tarda 2,7 min con install; el gate completo local entra en 5 min). `pip-audit` como
step `continue-on-error`. **Alerta de staleness sin tocar la app**: workflow con `schedule`
horario que hace GET a `http://129.80.148.166/api/health` y falla si `is_stale`, `status !=
"ok"` o `loop_crashes_24h > 0` (usar `age_seconds`; GitHub avisa por email al dueño del
workflow; documentar que los `schedule` corren con demora y se desactivan tras 60 días sin
actividad en el repo). Skill `verificar-ui` con el Playwright MCP existente (login → home →
panel → modal → `/fci` → `/on`, leer consola). Aceptación: ARM reporta la misma cuenta que
x86 con skips esperados; pre-push probado con un push deliberado; alerta disparada y recibida
una vez (apagar el refresco en un entorno de prueba); `verificar-ui` de punta a punta.

**Fase 4 — Dieta de `CLAUDE.md`, `agents.md` y memoria.** *(Estado 2026-09-07: ejecutada en
`fase-4-docs`: CLAUDE.md 379 → 163 líneas (36 → 11,8 KB, ≈8,9k → ≈3,0k tokens); 4 rules por
ruta; 6 docs nuevos incl. `docs/convenciones-financieras.md`; agents.md = cabecera + §0 +
stub; 5 entradas de memoria corregidas. Queda para David en sesión nueva: `/doctor`,
`/context` contra la baseline, y las tres preguntas "dónde vive X".)* Arrancar con `/doctor`.
`CLAUDE.md` → <200 líneas / ~12–14 KB: preámbulo corto, cómo correr, invariantes sin narrativa
histórica, las dos reglas duras de robustez (centinela httpx, TLS), prioridad sobre skills,
índice "cuándo leer qué" con punteros de una línea. `.claude/rules/`: `pricing.md`
(`paths: ["core/domain/pricing/**"]`), `web.md` (`apps/web/**`), `auth.md`, `deploy.md`
(`deploy/**`, `scripts/**`), ≤80 líneas cada una; los invariantes que aplican al **crear**
archivos se quedan en `CLAUDE.md`. `docs/`: `arquitectura.md`, `flujo-web.md`, `auth.md`,
`despliegue.md`, `pendiente.md`. Corregir las afirmaciones obsoletas de §0.6 y deduplicar
(txt/lock 5→1, admin 4→1, `on.js` 2→`CLAUDE.md` + hook + deny). `agents.md`: convenciones
financieras vigentes (~16 KB) → `docs/convenciones-financieras.md` corrigiendo la línea 159;
este archivo queda como **§0 + stub de punteros** (9 tests lo citan). Memoria: corregir las 3
entradas vencidas del índice (pricing-equivalence resuelto; "droplet"; JWT/admin según Fase
0). Prohibido migrar contenido a `@imports`. Aceptación: los 48 meta-tests de docs verdes;
`/context` muestra la caída respecto de la baseline; en sesión nueva, tres preguntas "dónde
vive X" y tres tareas que exigen abrir una doc se resuelven leyendo la doc correcta.

**Fase 5 — Controles financieros.** Golden ejecutable de TTJ26 contra el ancla IAMC (serie
TAMAR/CER congelada en fixture, fecha fija, procedencia). Golden de **TX28** con CER de BCRA
congelado y un corte externo capturado a mano (fecha, precio, TIR/paridad publicada).
Procedencia en los 17 goldens existentes. Hash del calendario (set derivado 2020–2029, sin
`refresh()`). Política float-only y tolerancias en `docs/convenciones-financieras.md`.
**Reemplazo del 5.6 original**: valores 30/360 a mano para 31→30 y 29-feb en
`test_daycount.py` (no copiar símbolos al legacy). Helper único "valor ≤ 0 = dato ausente" en
los bordes de ingesta (hoy repetido en `provider_hub`, `fci_history`, `letras_sync`) + test
guardián + línea en `CLAUDE.md`. Aceptación: los goldens nuevos se ponen rojos ante una
mutación deliberada del motor (revertida después); equivalencia verde; gate verde.

**Fase 6 — Piloto pyright-lsp.** Baseline `pyright` standalone (`pyrightconfig.json` modo
basic sobre `core/domain` + `apps/web`); si el ruido es inmanejable sin excluir media base,
cerrar con ese dato. Tres tareas representativas (glue de `apps/web`, `core/domain`, un
router), mismo modelo e instrucciones, sin y con plugin. Métricas: defectos reales antes del
gate, falsos positivos por turno, gate rojo/verde, fix-del-fix, intervenciones. Conservar solo
si ≥1 defecto real por tarea y <10 diagnósticos por turno. Reversión: `claude plugin uninstall
pyright-lsp`; borrar `pyrightconfig.json`.

### 0.9 No hacer (y no volver a proponer)

Context7, Serena, GitHub MCP Server, claude-mem, Chrome DevTools MCP (solo ad hoc ante
sospecha de leak), security-guidance por hook, hookify, frameworks de método (OpenSpec, Spec
Kit, BMAD, Task Master, CCPM, SuperClaude, Claude Flow, ECC, Oh My ClaudeCode, GSD, Compound
Engineering), Ralph, agentes o IDEs alternativos sobre el mismo árbol, catálogos de skills en
bloque, sonatype-guide, logfire. Tampoco: `bypassPermissions`, `--force`, reescritura de
historia, tocar prod fuera de `deploy.sh`, MCPs nuevos, `@imports` para "aliviar" `CLAUDE.md`,
`Write(path)` en reglas de permisos, endpoints de salud nuevos, `refresh()` del calendario
en tests, copiar los símbolos vivos al motor legacy, `pip freeze` de Windows como lock,
`$MONITOR_DB_DIR` pelado en `deploy.sh`, apuntar `deploy.sh` al lock, `== ` en
`requirements.txt`, mover el repo sin migrar el slug de memoria.

### 0.10 Glosario

- **Fuente activa / floor**: `ProviderHub` trae la fuente live (`byma_open` default) y le
  mergea **debajo** un snapshot Data912 para los símbolos que la activa no lista, pisando
  un precio 0 con un cierre real (`_apply_floor`). Un precio raro se mira en las dos.
- **Ancla analítica**: fila única `es_ancla=1` en `cashflows` para TAMAR/DUAL/DUAL_CER_TAMAR,
  que cobran por fórmula cerrada; el motor la filtra. Materializar un schedule es error.
- **Equivalencia**: `tests/test_pricing_equivalence.py` compara el motor nuevo contra
  `tests/_legacy_engine.py` (congelado salvo el solver/metrics/30-360 compartidos a
  propósito) sobre todo el universo, tolerancia 1e-7. Detecta regresiones, **no** errores de
  origen: para eso están los goldens externos.
- **Golden externo**: test con valores esperados de una fuente **independiente** del motor
  (calculadora de referencia, IAMC, BCRA), con procedencia declarada y fecha fija
  (`tests/_clock.py`).
- **Oráculo**: cualquier referencia contra la que un test pueda fallar de verdad
  (equivalencia, golden, property-based). Donde no hay oráculo (web, ingesta, auth), el
  detector real termina siendo producción.
- **Gate**: `ruff check .` + `pytest tests/ -q`; local (`check.ps1`) y CI (`gate.yml` →
  `check.sh`, x86 + ARM). "Gate verde" = ambos.
- **Drift (de versiones)**: divergencia entre laptop / prod / CI (§0.3). **Drift (de docs)**:
  copia que sobrevivió a su original (§0.1.4).
- **Fix-del-fix**: commit que corrige uno del mismo día. Señal de ola demasiado grande o de
  verificación que midió otra cosa.
- **Prueba por mutación**: revertir el fix y exigir que el test se ponga rojo (§0.1.8).
- **Test decorativo**: pasa en verde con el bug presente.
- **Delta spec**: spec que describe solo el cambio, no re-declara el sistema.
- **Compound**: paso de cierre que destila la lección en invariante ejecutable (§0.1.9).
- **Invariante ejecutable**: regla que vive en un test guardián, un hook, un permiso o un
  docstring-contrato — no solo en prosa.
- **Slug de memoria**: ruta del repo con no-alfanuméricos → `-`; clave de
  `~/.claude/projects/<slug>/`.
- **Rule por path**: `.claude/rules/x.md` con `paths:`; carga al leer archivos que matchean,
  no al crear.
- **Best-effort** (hooks `if`): puede correr de más, nunca garantiza bloquear; lo duro va en
  permisos.
- **Freeze**: `pip freeze` de un entorno en una fecha (prod, CI); el lock es un freeze
  **curado** con markers.
- **Cero = dato ausente**: `ccp<=0`, `tem: 0`, precio 0 de la activa → descartar, no usar
  como valor.
- **Corte** (ratings, letras, FCI): payload diario de una fuente externa; se descarta entero
  si trae <60 % del máximo reciente.
- **Modo auditoría**: §0.1.5.

### 0.11 Mantenimiento de esta sección

Se actualiza **acá** (y solo acá) cuando cambie: una versión de la tabla §0.3, un veredicto de
§0.4, una regla verificada de §0.5 (citar la URL de la doc y la fecha), un hecho de §0.6/§0.7,
o el estado de una fase de §0.8. Cada cambio lleva fecha. Si otra IA encuentra que algo de
acá ya no es cierto, lo corrige acá con evidencia antes de seguir — y no lo re-copia en otro
archivo. **Las convenciones financieras NO viven acá** (desde la Fase 4, 2026-09-07): están
en `docs/convenciones-financieras.md`, con `archivo:línea` del código por cada regla; un
cambio de convención se corrige allá (código + línea + test guardián), nunca en este archivo.


---

## Stub de secciones históricas → dónde viven ahora

El documento viejo (`git show c0dda9d:agents.md`, 534 líneas después de §0) se retiró en la
Fase 4 (2026-09-07). Cada título que existía se mapea acá a su destino vigente. «Histórico:
git» = describe una capa que ya no existe (`apps/web/server.py`, `app.js`, el layout
arrastrable, los endpoints `/api/*`, una base «analytics» que nunca existió) y solo sirve
como arqueología. Los tests que citan
`agents.md › "…"` en docstrings apuntan a las filas marcadas ★.

| Sección del doc viejo | Dónde vive ahora |
|---|---|
| Cabecera «REINGENIERÍA IMPLEMENTADA … Cambios clave» | Histórico: git. Arquitectura actual → `CLAUDE.md` · `docs/arquitectura.md` |
| VISIÓN GENERAL | `CLAUDE.md` (preámbulo) · `docs/arquitectura.md` |
| Stack técnico | `docs/arquitectura.md` (el stack web viejo y la base «analytics» que listaba eran falsos) |
| LOS 4 PILARES ARQUITECTÓNICOS — Pilar 1 (una config por curva) | `docs/flujo-web.md` (registro `PANELS`/`PANEL_ORDER` de `panels_schema.py`); el `_build_refresh_context` del server viejo es histórico |
| Pilar 2 (SQLite = fuente de verdad; Excel/CSV semillas) | `CLAUDE.md › Invariantes` · `docs/convenciones-financieras.md › «Schema de las hojas / campos del instrumento»` |
| Pilar 3 (matemática centralizada) | `docs/convenciones-financieras.md › «Cómo extender la matemática financiera»` |
| Pilar 4 (fuente de precios intercambiable: hub + floor) | `CLAUDE.md` (preámbulo + glosario §0.10) · `docs/arquitectura.md` |
| PIPELINE | `docs/arquitectura.md` · `docs/flujo-web.md` (el diagrama con `server.py`/threads es histórico) |
| ESTRUCTURA DE ARCHIVOS | `CLAUDE.md › Arquitectura` · `docs/arquitectura.md` (7 archivos del árbol viejo no existen: `apps/web/server.py`, `app.js`, `style.css`, `index.html`, `curva.js`, `config/theme.py`, `interfaces` de uso web) |
| SCHEMA DE LAS HOJAS DEL EXCEL | `docs/convenciones-financieras.md › «Schema de las hojas / campos del instrumento»` (corregido: Excel = SEMILLA) |
| CÓMO AGREGAR UNA NUEVA CURVA | `CLAUDE.md › Invariantes` («tipos») · `docs/flujo-web.md`; los pasos sobre `_get_columns` / `Snapshot.__init__` / `index.html` son históricos |
| CÓMO AGREGAR UN NUEVO INSTRUMENTO (sin tocar Excel a mano) | `CLAUDE.md › Invariantes` (la ABM escribe SQLite; preview de cashflows) · `docs/flujo-web.md` |
| CONVENCIONES CRÍTICAS | `docs/convenciones-financieras.md` (entero) |
| ★ Bonos CER (NT N°8/2024) | `docs/convenciones-financieras.md › «Bonos CER (NT N°8/2024)»` |
| ★ Bonos TAMAR (PURO, DUAL, DUAL_CER_TAMAR) | `docs/convenciones-financieras.md › «Bonos TAMAR (PURO, DUAL, DUAL_CER_TAMAR)»` («max rails», «MD bullet m=12») |
| Bonos DOLAR LINKED | `docs/convenciones-financieras.md › «Bonos DOLAR LINKED»` |
| Obligaciones Negociables (ON hard-dollar) | `docs/convenciones-financieras.md › «Obligaciones Negociables (ON hard-dollar)»` (convención ON `ACT/365`, BACH 30/360) |
| Bonos LECAP / BONCAP capitalizables | `docs/convenciones-financieras.md › «Bonos LECAP / BONCAP capitalizables»` |
| Modified Duration — convención BYMA/IAMC | `docs/convenciones-financieras.md › «Modified Duration — convención BYMA/IAMC»` |
| Day-count / convención de descuento | `docs/convenciones-financieras.md › «Day-count / convención de descuento»` |
| Soberanos: 3 especies por moneda (ARS / MEP / CABLE) + pricing de la pata ARS | `docs/convenciones-financieras.md › «Soberanos: 3 especies por moneda…»` |
| Date parsing — bug histórico | `docs/convenciones-financieras.md › «Schema…»` (fechas ISO, `repositories.py:92-110`) |
| Accrued period para soberanos mid-amortización | `docs/convenciones-financieras.md › «Intereses corridos (accrued) y período corriente»` |
| CÓMO EXTENDER LA MATEMÁTICA FINANCIERA (tabla de `FinancialEngine`) | `docs/convenciones-financieras.md › «Cómo extender la matemática financiera»` |
| Override de settle_date (T+0/T+1) / Convención T+0 / T+1 | `docs/convenciones-financieras.md › «Settlement: T+1 para todos»` |
| Curvas y BEI (`yield_curve.py`) · Sendero mensual (`inflation_path.py`) | `docs/convenciones-financieras.md › «Curvas y BEI (NT N°3/2019 + NT N°8/2024)»` |
| CACHE Y PERFORMANCE (incluida la regla «I/O y parsing fuera del lock» del BEI history, ex `agents.md:411`) | Histórico: git. Providers/caches vigentes → `docs/arquitectura.md`; la regla del lock vive en `tests/test_aud_A_infra_http_indices_lock.py` |
| DASHBOARD WEB (Paneles · Layout arrastrable · Endpoints `/api/*` · Popup 3 tabs · Resilience del web server) | Histórico: git. Vigente → `docs/flujo-web.md` · `CLAUDE.md › Flujo web (HTMX SSR)` |
| ENTORNO DE EJECUCIÓN | `CLAUDE.md › Cómo correr` · §0.2 |
| CONFIG OPCIONAL (.env) | `docs/despliegue.md` · `CLAUDE.md` (secretos, `MONITOR_*`) |
| TESTING & QUALITY | `CLAUDE.md` (gate) · §0.6 · skill `/gate` |
| TROUBLESHOOTING | Histórico: git (casi todo refiere a `server.py`/`app.js`). Salud vigente → `/api/health` (§0.2) · `docs/flujo-web.md` · `docs/pendiente.md` |
| CHECKLIST PARA DESARROLLADORES | `CLAUDE.md › Invariantes` · skill `/compound` (§0.1.9). La línea «¿Agregaste un instrumento? Solo en el Excel» era FALSA: las altas van por la ABM → SQLite |
| Última actualización / Versión (7.2 · 7.1 · 7.0 · 6.5 · 6.4) | Histórico: git |
| CHANGELOG v7.2 · v7.1 · v6.5 · v6.4 · v6.1 | Histórico: git (`git log -p agents.md`, `git show c0dda9d:agents.md`) |
