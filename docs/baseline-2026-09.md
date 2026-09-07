# Baseline 2026-09 — línea de base antes de las mejoras al flujo asistido por IA

Estado medido el **2026-09-07** sobre `main` = `7da0557`, antes de ejecutar las fases de
`agents.md › §0.8`. Cada número dice cómo se midió, para poder repetir la medición después de
cada fase y comparar. Lo que no se pudo medir queda marcado como pendiente, no estimado.

## Suite de tests

| Medida | Valor | Cómo |
|---|---|---|
| Tests colectados | 2 625 en 198 archivos `test_*.py` | `py -3.12 -m pytest tests/ --collect-only -q` |
| Suite completa, Windows (laptop) | **2617 passed, 8 skipped, 160,1 s (2:40)** | `py -3.12 -m pytest tests/ -q -p no:cacheprovider` desde el harness de Claude Code |
| Skips en esa corrida | 3 × `time.tzset() es sólo Unix` (`test_timezone.py`) + 5 × `requiere bash` (`test_rem_R5_ops_tests_deploy_venv.py`) | `-rs` |
| Skips en la shell habitual de David (con bash en PATH) | 3 (`2622 passed / 3 skipped`) | mensaje del commit `7da0557` |
| Skips en el runner ARM del CI | 32 (`2445 passed / 32 skipped`, commit `a154eac`) — 29 son los tests node de `fci.js` | mensaje del commit |
| Muestra de timing | 328 tests (`test_xirr_solver.py` + `test_panels_router.py`) en 6,0 s | `time pytest ...` |

Conclusión: la cuenta de skips **depende del entorno** (bash/node en PATH, plataforma). Un
assert de cuenta fija sería frágil; la Fase 3 asierta por *reason* esperado por plataforma.

## CI (GitHub Actions, `gate.yml`, matrix x86 + ARM)

| Run | SHA | Rama | Conclusión | Duración |
|---|---|---|---|---|
| gate | `7da0557` | main | success | 2,57 min |
| gate | `0bb77d5` | main | success | 2,75 min |
| gate | `0cd4170` | perf/optimizacion-extrema | success | 2,72 min |

Medido con `GET https://api.github.com/repos/IronCondorBursatil/MonitorMercadoArgy/actions/runs`
(repo público; los logs por job requieren auth → `gh`). Versiones que resolvió cada run:
**pendiente** hasta tener `gh auth login` (primer entregable de la Fase 1).

## Documentación cargada por sesión

| Archivo | Líneas | Bytes | Tokens estimados | Cómo |
|---|---|---|---|---|
| `CLAUDE.md` (auto-cargado por Claude Code) | 373 | 36 008 | ≈ 8,9k | `Measure-Object -Line`; chars/4 |
| `agents.md` (no auto-cargado; antes de §0) | 534 | 69 114 | ≈ 16,9k | ídem |
| `agents.md` (con §0 PROTOCOLO IA) | 1 138 | 123 446 | — | ídem |
| `README.md` | — | 6 053 | ≈ 1,5k | ídem |

`/context` con el `CLAUDE.md` actual: **pendiente — lo corre David en una sesión nueva** (el
agente no puede invocar slash commands del harness). Objetivo oficial: < 200 líneas.

## Configuración del agente

| Medida | Valor | Cómo |
|---|---|---|
| `~/.claude/settings.json` `permissions.allow` | 179 entradas · 0 `deny` · 0 `ask` | `ConvertFrom-Json` |
| — de otro proyecto (rutas "Calcu Online Claudio") | 19 (4 con typo que nunca matchean) | grep |
| — del puerto 5000 (proyecto Flask viejo) | 28 | grep |
| `.claude/settings.local.json` | 5 `allow` (incl. `Bash(git checkout *)`, `Bash(py -3.12 -c ' *)`) | lectura |
| Hooks configurados | 0 | `hooks` ausente en ambos settings |
| Skills / rules / commands de proyecto | 0 | `Test-Path` |
| Plugin Superpowers | 5.1.0 (instalado 2026-05-27) → **6.3.0 el 2026-09-07 (Fase 0)** | `claude plugin update superpowers` |
| MCP | `playwright` (scope usuario) | `~/.claude.json` |
| Memoria auto | 19 archivos, ≈ 46 KB, slug `c--Users-david-OneDrive-Monitores---Data912` | listado |
| Backup del settings global | `~/.claude/settings.json.bak-2026-09-07` (16 033 B) | Fase 0 |

## Dependencias — los tres conjuntos

| Paquete | Laptop (= lock 2026-06) | Prod (freeze 2026-09-07) | CI |
|---|---|---|---|
| fastapi | 0.136.3 | 0.141.1 | resolución del día |
| starlette | 1.1.0 | 1.6.0 | ídem |
| uvicorn | 0.48.0 | 0.52.4 | ídem |
| numpy | 2.4.6 | 2.5.2 | ídem |
| pydantic | 2.13.4 | 2.13.5 | ídem |
| SQLAlchemy | 2.0.50 | 2.0.52 | ídem |
| httpx | 0.28.1 | 0.28.1 | ídem |
| pandas | 2.3.3 | 2.3.3 (capado por optionlab `<3`) | ídem |
| holidays | 0.44 | 0.44 (capado por optionlab `<0.45`) | ídem |

Freeze completo de prod: `deploy/freeze/prod-2026-09-07.txt` (139 paquetes, Python 3.12.3,
`ssh monitor-oci venv/bin/pip freeze`). Deps sensibles a forma que gobiernan la Fase 1:
**fastapi, starlette, uvicorn, pydantic, SQLAlchemy, numpy, httpx** (+ pandas y holidays
observadas pero acotadas por `optionlab`, no por nosotros).

`requirements.txt`: 24 deps, 21 abiertas, 2 con cota inferior, 1 con cota superior
(`bcrypt<4.0.0`). `requirements.lock`: 26 pins curados a mano, sin markers; el local ya
divergió en `certifi` (2026.6.17 vs 2026.2.25) y `requests` (2.34.2 vs 2.33.1).

## Calidad estática

| Medida | Valor |
|---|---|
| `ruff check .` | 0 violaciones (ruff 0.15.16; reglas `F,E,W`) |
| Type-checking | ninguno |
| Coverage | no se mide |
| Pre-push hook | no instalado |

## Producción (`GET http://129.80.148.166/api/health`, 2026-09-07 13:46 UTC)

`status ok · instruments 1160 · metrics_cached 701 · is_stale false · age_seconds 4.17 ·
degraded_loops [] · loop_crashes_24h 0 · catalog {orphans 1, defaulted 158, seed_failed false}`.

## Calendario de feriados

197 fechas 2020–2029 derivadas de `data/feriados_ar.xlsx` (207 filas, 10 `xbue_pmc`
descartadas). Hash del set (`sha256` de las ISO ordenadas unidas por `\n`):
`56aed962fb13dc47164060044ce7a1dab7deede6f02a0e1c959c12a3d5ad2818`.

## Cómo repetir la baseline

```powershell
py -3.12 -m pytest tests/ -q -rs -p no:cacheprovider          # suite + skips por reason
py -3.12 -m pytest tests/ --collect-only -q | Select-Object -Last 1
(Get-Content CLAUDE.md | Measure-Object -Line).Lines
(ConvertFrom-Json (Get-Content "$env:USERPROFILE\.claude\settings.json" -Raw)).permissions.allow.Count
ssh monitor-oci "cd /home/ubuntu/MonitorMercadoArgy && venv/bin/pip freeze"
Invoke-RestMethod http://129.80.148.166/api/health
```
