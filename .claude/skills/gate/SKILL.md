---
name: gate
description: Corre el gate local (ruff + pytest completo, ~2:45 min) y enseña a leer el resultado — skips esperados en Windows, cómo listar los motivos con -rs y qué reportar cuando está rojo. Usar antes de commitear/pushear y al cerrar cualquier cambio.
argument-hint: "[fast]"
allowed-tools: Bash(pwsh scripts/check.ps1*), PowerShell(pwsh scripts/check.ps1*), Bash(py -3.12 -m pytest *), PowerShell(py -3.12 -m pytest *), Bash(py -3.12 -m ruff *), PowerShell(py -3.12 -m ruff *), Bash(git status*), PowerShell(git status*)
---

# /gate [fast] — ¿está verde el repo?

`scripts/check.ps1` es el "verde/rojo" canónico del repo: ruff + la suite completa (2.631
tests). El CI (`.github/workflows/gate.yml`, x86 + ARM) corre lo mismo en Linux; el gate local
es el que se corre ANTES de pushear. No reemplaza al CI: hay ramas `hasattr(time, "tzset")`
que en Windows jamás se ejecutan.

## Correr

```powershell
pwsh scripts/check.ps1          # /gate       → ruff + pytest completo (~2:45 min)
pwsh scripts/check.ps1 -Fast    # /gate fast  → ruff + pytest -x (corta en el 1er fallo)
```

- **Timeout de la tool: 300000 ms.** El default (120 s) corta el gate por la mitad y deja
  un "fallo" que no es tal.
- `fast` es para iterar; el que cuenta antes de un push es el completo.
- Nunca con compresión *lossy* de la salida (agents.md §0.1.12): si hay que recortar,
  redirigir al scratchpad y leer el fragmento con `Read`:
  `pwsh scripts/check.ps1 *> "<scratchpad>\gate.txt"`.

## Leer el resultado

La última línea es `=== GATE VERDE ===` o `=== GATE ROJO ===`. Antes viene el resumen de
pytest: `N passed, M skipped in X s`.

**Skips esperados en Windows: exactamente 3**, todos `time.tzset() es sólo Unix`
(`tests/test_timezone.py`). Corrido desde el harness de Claude sin `bash` en el PATH aparecen
**5 más** con motivo `requiere bash` (total 8): no son un problema. **Cualquier otro skip —
o cualquier otra cuenta— es novedad y se reporta.** Para ver los motivos:

```powershell
py -3.12 -m pytest tests/ -q -rs     # -rs = lista cada skip con su reason (vuelve a correr la suite)
```

Un skip nuevo suele ser una dependencia opcional que dejó de importar o un `skipif` que
cambió de condición: no es verde, es "no se probó".

## Si está rojo

1. **No tocar el test para que pase.** Regla dura (agents.md §0.1.11): no se debilitan
   tests, tipos ni validaciones. Un test que se "arregla" pierde justo lo que estaba
   protegiendo.
2. **Aislar**: `py -3.12 -m pytest "tests/test_x.py::test_y" -q --tb=long` (con
   `--tb=long` el traceback trae la línea del assert y los valores).
3. **¿Preexistente?** Si el árbol tiene cambios propios (`git status --porcelain`), hay que
   saber si el test falla también sin ellos. Se registra como preexistente (§0.1.7) y se
   avisa; no se "arregla de paso".
4. **Reportar, rotulado** (§0.1.2 — HECHO OBSERVADO / HIPÓTESIS / DESCONOCIDO):
   - test: `tests/archivo.py::nombre`
   - assert: la línea exacta del traceback, con el esperado vs. obtenido
   - hipótesis: qué cambio lo explica (archivo:línea) y qué falta comprobar
5. **Frenar y preguntar** (§0.1.10) si el fallo está fuera de lo que el cambio toca.

Si lo que falla es **ruff**, `py -3.12 -m ruff check . --diff` muestra qué cambiaría el
autofix sin aplicarlo; aplicar sólo si es trivial y dentro del alcance del cambio.

## Casos conocidos

- `test_pricing_equivalence.py` compara el motor contra el legacy congelado con fecha fija
  (`tests/_clock.py`, override `MONITOR_TEST_REF_DATE`). Si se pone rojo tras tocar pricing,
  es la señal correcta, no ruido.
- Los tests redirigen TODAS las bases a un sandbox bajo `tempfile.gettempdir()`
  (`tests/conftest.py`): un fallo por "no such table" o por una base que no es la del sandbox
  es de entorno, no de código — reportarlo así.
