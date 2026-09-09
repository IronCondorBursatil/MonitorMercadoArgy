---
name: compound
description: Checklist de cierre de fase/feature/bugfix antes de pedir merge (agents.md §0.1.9 y §0.8) — qué lección quedó materializada (test guardián, hook/permiso, línea de AGENTS.md/agents.md), qué memoria y qué doc se corrigieron, qué quedó fuera de alcance, y el bloque de reporte para el PR.
allowed-tools: Bash(git status*), PowerShell(git status*), Bash(git diff *), PowerShell(git diff *), Bash(git log *), PowerShell(git log *), Bash(gh pr *), PowerShell(gh pr *)
---

# /compound — que lo aprendido no viva sólo en un mensaje de commit

Motivo (agents.md §0.1.9): los errores se repitieron entre sesiones exactamente mientras
vivieron sólo en commits — "cero = dato ausente" tres veces (floor de precios, `ccp` de FCI,
`tem` de letras); "perder/duplicar el escalón de settlement" tres veces. Cesaron cuando se
volvieron docstring-contrato + tests guardianes. Este checklist se responde **por escrito**
al cerrar cualquier fase, feature o bugfix, antes de `/code-review` y de pedir merge. Las
respuestas van a la descripción del PR (plantilla al final).

Cuándo: al terminar la implementación, con el gate verde (`/gate`). Si tocó auth o routers,
`/security-review` antes de pushear (§0.1.13).

## Las cinco preguntas (cada una con destino)

**1. ¿Qué invariante o lección nueva apareció?**
Una frase, concreta ("un `tem: 0` de ArgentinaDatos es dato ausente"). Si no hubo ninguna,
decirlo: "sin lección nueva" es una respuesta válida; inventar una, no.

**2. ¿Quedó materializada? ¿Dónde?** Elegir al menos una; "está en el mensaje de commit"
no cuenta.

| Forma | Cuándo | Qué anotar |
|---|---|---|
| **Test guardián** | la lección se puede ejecutar | id completo `tests/test_x.py::test_y` **y** la prueba por mutación (abajo) |
| **Hook / permiso** | la lección es "no hacer X" | la regla exacta en `.Codex/settings.json` (`allow`/`ask`/`deny`) o el hook, y la llamada deliberada con la que se probó que bloquea |
| **Línea de `AGENTS.md`** | es un invariante del repo (no romper) | sección "Invariantes" o "Robustez"; una línea, sin narrativa histórica |
| **Línea de `agents.md §0`** | es una versión, un veredicto de herramienta, una regla de trabajo o un hecho del entorno | la subsección (0.1 reglas · 0.2 entorno · 0.3 versiones · 0.4 herramientas · 0.6 hechos del repo) |
| **Docstring-contrato** | es un orden de pasos que ya se rompió (p. ej. `pricing/tamar.py`) | archivo y función |

Un test guardián sin mutación probada es decorativo (§0.1.8). Un hook que nunca se disparó
a propósito puede estar apagado en silencio (§0.5).

**3. ¿Qué archivo de memoria se actualizó?**
`~/.Codex/projects/c--Users-david-OneDrive-Monitores---Data912/memory/` + la línea del
índice `MEMORY.md`. Memoria = observación con fecha (qué se vio, qué falló, preferencias
de trabajo), no la verdad del código: un hecho del código va a docs o tests, y la memoria
apunta. Si una entrada del índice quedó vencida por este cambio (p. ej. "por rotar",
"pendiente de correr"), corregirla o marcarla cerrada — no dejarla contradiciendo al repo.

**4. ¿Qué doc quedó obsoleta y se corrigió?**
Regla "un hecho, un archivo" (§0.1.4): infraestructura → `deploy/README-ops.md`; versiones y
herramientas → `agents.md §0`; invariantes → `AGENTS.md`; convenciones financieras →
`agents.md` "CONVENCIONES CRÍTICAS". Los demás archivos **apuntan**, no copian. Buscar los
duplicados del hecho que cambió (`Grep` del término en `*.md`, `scripts/*.ps1`, comentarios
de cabecera) y dejar UNA fuente; las otras, un puntero o nada. Anotar cuáles se tocaron y
cuál quedó como fuente.

**5. ¿Qué quedó fuera de alcance y dónde se anotó?**
Un bug fuera de alcance se anota y no se corrige en la misma rama, salvo que bloquee (§0.8).
Destinos: sección "Pendientes" del PR · `AGENTS.md` "Pendiente (cola, no funcional)" si es
del repo · `docs/decisiones.md` si es una decisión de David · issue si necesita seguimiento.
"Lo vi y no lo anoté" no es una opción.

## Bloque de reporte para la descripción del PR

```markdown
## Qué cambió
<2-5 líneas: el cambio, no la historia>

## Evidencia por criterio de aceptación
| Criterio | Comando | Salida (recortada, sin lossy sobre tracebacks) | Cumple |
|---|---|---|---|
| gate local | `pwsh scripts/check.ps1` | `N passed, 3 skipped` · `=== GATE VERDE ===` | sí |
| CI x86 + ARM | `gh run watch <id> --exit-status` | exit 0 | sí |
| <criterio de la fase> | `<comando>` | `<salida>` | sí/no |

## Criterios no cumplidos y por qué
<o "ninguno">

## Compound
- Lección: <una frase | "sin lección nueva">
- Materializada en: <test id + mutación probada | regla/hook | AGENTS.md §… | agents.md §0.x | docstring>
- Memoria: <archivo + línea del índice | "sin cambios">
- Doc corregida: <archivo(s) → fuente única en …> | "ninguna obsoleta"
- Fuera de alcance: <qué + dónde quedó anotado>

## Pendientes
<lista, con dueño si lo necesita (David: gh auth login, OK de deploy, decisiones D1-D4)>
```

## Prueba por mutación (antes de dar por bueno un test guardián)

Un test que no falla al revertir el fix que dice cubrir es decorativo. Secuencia: revertir
el fix (o `git stash` del cambio de producción, dejando el test) → correr el test → **exigir
rojo** → restaurar → verde. Anotar en el PR el comando y el rojo observado. Está admitido
dos veces en el historial (`0bb77d5`, `95380b2`); no sumar una tercera.
