# Novedades del universo — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Un job diario (08:00 AR) que compara lo que el hub vio en la rueda contra el universo conocido, deja las especies nuevas en `universe_novedades`, y un ABM con pestaña «Novedades» primera + badge global para que David decida qué cargar — sin escribir jamás `instruments` solo.

**Architecture:** Módulo puro `core/infrastructure/byma/novedades.py` (diff, guard, agenda, store de la tabla nueva) + borde `apps/web/universe_service.py` (lee el hub, escribe `byma_catalog`/`universe_novedades` en UNA transacción bajo un lock, ficha BYMA best-effort) + loop `_universe_loop` en `apps/web/app.py` bajo el supervisor + siembra del universo en el lifespan ANTES de los loops + rutas nuevas en `apps/web/routers/abm.py` con fragment `abm_novedades.html`. El contador viaja por `AppState` → `status()` → `/api/health` (cuenta) y `/health/badge` (link al ABM).

**Tech Stack:** Python 3.12 (`py -3.12`, sin venv), FastAPI + HTMX SSR, SQLAlchemy 2 sobre SQLite (`catalog.db`, forward-only), pytest sin pytest-asyncio (`asyncio.run`), ruff.

**Spec:** `docs/superpowers/specs/2026-09-07-novedades-universo-design.md` (leerla entera antes de la Task 1; el plan argumenta desde ella). Este plan pasó una revisión adversarial (5 lentes + escéptico por hallazgo) el 2026-09-07; sus 17 hallazgos ya están integrados.

## Global Constraints

- Intérprete SIEMPRE `py -3.12` (nunca `python`/`pytest` pelados). Un test suelto: `py -3.12 -m pytest "tests/test_x.py::test_y" -q --tb=long`. Gate: `pwsh scripts/check.ps1` (ruff + pytest, ~2:45 min; timeout de tool 300000 ms).
- **Nunca se escribe `instruments`** desde este subsistema. El invariante «alta automática de letras = la ÚNICA escritura automática en `instruments`» se mantiene.
- **Nunca se borra**: ni filas de `byma_catalog` ni de `universe_novedades` (`descartada` es un estado). `ingest_byma_catalog` (DELETE+INSERT) sólo corre sobre la tabla vacía o con `force=True`.
- Schema forward-only: tablas/columnas nuevas SOLO por ORM en `core/infrastructure/db/models.py` (`init_db` hace `create_all` + `ALTER ADD COLUMN`). NO subir `CURRENT_SCHEMA_VERSION` (no hay migración de datos).
- La tabla nueva vive en `catalog.db` (mismo engine): NO tocar `_DB_DERIVED`.
- Fecha/hora: la lógica pura recibe `hoy`/`now` como parámetro; los tests usan `tests/_clock.ref_date()` y `datetime(..., tzinfo=ZoneInfo("America/Argentina/Buenos_Aires"))`. Nada de `date.today()` dentro de la lógica de diff ni de la agenda (un stub de test que tiene que devolver «hoy real» usa la MISMA expresión que el loop: `datetime.now(ZoneInfo(settings.timezone)).date()`).
- Un `0`/vacío de una fuente externa es dato ausente (`core/domain/missing.valor_o_none`).
- Timeouts httpx: centinela `httpx.USE_CLIENT_DEFAULT`, nunca `None`. (Este plan no crea requests nuevos: la ficha reusa `catalog_enrich._ficha_raw`.)
- Rutas nuevas cuelgan de `abm.router` (hereda `RequireTabPermission("abm")`, `apps/web/app.py:809`); el POST de refresh además `_admin=Depends(get_admin_user_html)`. `reject_cross_site` ya es dependencia global de la app.
- Tests de web: `TestClient(app)` sobre el catálogo compartido del sandbox — limpiar lo que se crea en `finally`; NUNCA combinar `tmp_db`/`configure()` con `TestClient(app)` (contamina el singleton `get_repo()`).
- Todo test que borre `MONITOR_DISABLE_LOOPS` y bootee el lifespan real DEBE stubear `_seed_byma_universe` (si no, ingiere el CSV de 4.700 filas en el sandbox) y, desde la Task 6, `_universe_loop` (si no, corre contra la red). Hoy esos tests son `tests/test_ratings_loop.py::_stub_loops`, `tests/test_aud_D1_seguridad_web.py:262-265`, `tests/test_rem_R3_web_state_severity.py:209-212`; el plan los toca en las Tasks 5 y 6.
- ruff `select F,E,W` (tests incluidos; E402 ignorado): un import sin usar (F401) rompe el gate. Cada Task agrega al archivo de tests SÓLO los imports que usa en ese corte.
- Commits: mensaje en español, imperativo, y terminar con `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`.

---

## Estructura de archivos

| Archivo | Responsabilidad |
| --- | --- |
| `core/infrastructure/db/models.py` (modificar) | `UniverseNovedadORM` + columnas `denominacion`, `vencimiento`, `last_seen` en `BymaCatalogORM`; docstring de `BymaCatalogORM` deja de decir «se puede borrar y reingerir sin pérdida». |
| `core/infrastructure/byma/novedades.py` (crear) | PURO: `meta_de`, `clasificar` (diff + guard), `proximo_despertar` (agenda 08:00). STORE: `registrar_nuevas_en`, `marcar_cargadas_en`/`marcar_cargadas`, `descartar`, `restaurar`, `listar`, `agrupadas`, `contar_nuevas`, `simbolos_registrados`, `leer_meta`, `escribir_meta_en`, `CATEGORIAS_SIN_HOJA`. |
| `apps/web/universe_service.py` (crear) | Borde: `sincronizar_universo(hub, *, hoy, ficha_fn, max_fichas) -> Resultado` (lock, guard de universo vacío, una transacción), `ultima_corrida()`, `META_*`. |
| `core/infrastructure/byma/universe.py` (modificar) | `ingest_byma_catalog(..., force=False)` con guard por `last_seen`; docstring del módulo; `prefill_for`: Letras → Tasa_Fija con `clase` por prefijo, vencimiento de la ficha; `_clase_letra`. |
| `apps/web/app.py` (modificar) | `_seed_byma_universe` (siembra sólo si vacía) llamada en el LIFESPAN antes de las tasks; `_startup_reconcile` sin ingest; `_universe_loop`; tupla del lifespan; clave `novedades` en `/api/health`; comentario «seis». |
| `apps/web/state.py` (modificar) | `_novedades`, `set_novedades`, `novedades`, clave `novedades` en `status()`. |
| `apps/web/templates/fragments/header_status.html` + `apps/web/static/css/app.css` (modificar) | Badge global aditivo `✦ N novedades` → `/abm`. |
| `apps/web/routers/abm.py` (modificar) | `GET /abm/novedades`, `POST /abm/novedades/{symbol}/descartar|restaurar`, `POST /abm/novedades/refresh` (admin), hook `nueva→cargada` en `/abm/save`, ctx `novedades` en `/abm`. |
| `apps/web/templates/fragments/abm_novedades.html` (crear) + `apps/web/templates/pages/abm.html` (modificar) | Pestaña «Novedades» primera, grupos por categoría, Cargar/Descartar/Restaurar, «Refrescar ahora». |
| `tests/test_universe_novedades.py`, `tests/test_universe_service.py`, `tests/test_universe_loop.py`, `tests/test_abm_novedades.py` (crear) + `tests/test_byma_universe.py`, `tests/test_ratings_loop.py`, `tests/test_aud_D1_seguridad_web.py`, `tests/test_rem_R3_web_state_severity.py` (modificar) | Cobertura por tarea (abajo). |
| `docs/flujo-web.md`, `docs/arquitectura.md`, `CLAUDE.md`, `apps/web/supervisor.py`, `tests/test_aud_D2_web_supervisor.py` (modificar) | «5 loops» → 6 donde describe el presente; invariante aclarado. |

---

### Task 1: Schema — tabla `universe_novedades` y columnas nuevas en `byma_catalog`

**Files:**
- Modify: `core/infrastructure/db/models.py:42-70`
- Test: `tests/test_universe_novedades.py`

**Interfaces:**
- Produces: `UniverseNovedadORM(symbol, first_seen, source, categoria, estado, updated_at)`; `BymaCatalogORM.denominacion/vencimiento/last_seen` (todas `Optional[str]`).

- [ ] **Step 1: Escribir el test de migración (rojo)**

Crear `tests/test_universe_novedades.py` (SOLO estos imports: los de las Tasks 2 y 3 se agregan en sus bloques):

```python
"""Novedades del universo — schema, diff puro, agenda y store (spec 2026-09-07)."""
from __future__ import annotations

from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import inspect

from core.infrastructure.db.catalog_repository import init_db
from core.infrastructure.db.engine import configure, get_engine
from tests._clock import ref_date

AR = ZoneInfo("America/Argentina/Buenos_Aires")
HOY = ref_date()


@pytest.fixture
def restore_engine():
    """Restaura el engine de la suite al terminar (mismo patrón que test_catalog_migration)."""
    from config.settings import settings
    yield
    configure(settings.catalog_db)


@pytest.fixture
def base(tmp_path):
    """DB temporal VACÍA con schema al día."""
    from config.settings import settings
    configure(tmp_path / "nov.db")
    init_db()
    try:
        yield
    finally:
        configure(settings.catalog_db)


# ── schema ──────────────────────────────────────────────────────────────────
def test_init_db_crea_universe_novedades_y_migra_byma_catalog(tmp_path, restore_engine):
    """Forward-only: una DB vieja (byma_catalog sin las columnas nuevas, sin la tabla de
    novedades) sale de init_db con todo agregado y la fila previa intacta."""
    configure(str(tmp_path / "old.db"))
    eng = get_engine()
    with eng.begin() as conn:
        conn.exec_driver_sql(
            "CREATE TABLE byma_catalog (symbol VARCHAR PRIMARY KEY, categoria VARCHAR)")
        conn.exec_driver_sql(
            "INSERT INTO byma_catalog (symbol, categoria) VALUES ('AL30', 'Títulos Públicos')")

    init_db()

    insp = inspect(get_engine())
    cols = {c["name"] for c in insp.get_columns("byma_catalog")}
    assert {"denominacion", "vencimiento", "last_seen"} <= cols
    assert insp.has_table("universe_novedades")
    ncols = {c["name"] for c in insp.get_columns("universe_novedades")}
    assert {"symbol", "first_seen", "source", "categoria", "estado", "updated_at"} <= ncols
    with get_engine().begin() as conn:
        row = conn.exec_driver_sql(
            "SELECT categoria FROM byma_catalog WHERE symbol='AL30'").fetchone()
    assert row is not None and row[0] == "Títulos Públicos"
```

- [ ] **Step 2: Correr y ver que falla**

Run: `py -3.12 -m pytest "tests/test_universe_novedades.py::test_init_db_crea_universe_novedades_y_migra_byma_catalog" -q --tb=long`
Expected: FAIL — `assert {'denominacion', ...} <= cols` (las columnas no existen) o `has_table` False.

- [ ] **Step 3: Agregar las columnas, el ORM y corregir el docstring**

En `core/infrastructure/db/models.py`, dentro de `BymaCatalogORM`, después de la línea `updated_at: Mapped[Optional[str]] = mapped_column(String, default=None)`:

```python
    # Las tres las escribe el job diario de novedades (`apps/web/universe_service.py`);
    # el seed CSV no las conoce y las deja en NULL. `vencimiento`/`denominacion` salen de
    # la ficha técnica BYMA (best-effort); `last_seen` = última corrida que vio el
    # símbolo en los feeds ('YYYY-MM-DD', hora AR).
    denominacion: Mapped[Optional[str]] = mapped_column(String, default=None)
    vencimiento: Mapped[Optional[str]] = mapped_column(String, default=None)
    last_seen: Mapped[Optional[str]] = mapped_column(String, default=None)
```

En el docstring de `BymaCatalogORM`, reemplazar las dos frases finales

```
    `data/byma/titulos_final.csv` (symbol→ISIN/categoría/emisor/...). Tabla derivada:
    se puede borrar y reingerir sin pérdida (≠ `instruments`, que es la verdad ABM)."""
```
por

```
    `data/byma/titulos_final.csv` (symbol→ISIN/categoría/emisor/...) SÓLO si está vacía;
    después la mantiene el job diario de novedades (`apps/web/universe_service.py`), que
    agrega símbolos que sólo existen por el feed, `last_seen` y datos de ficha. Ya NO es
    derivada: un re-seed pierde eso (ver `ingest_byma_catalog(force=...)`). ≠ `instruments`,
    que es la verdad ABM."""
```

Y después del bloque de índices de `BymaCatalogORM` (`Index("ix_byma_emisor", BymaCatalogORM.emisor)`):

```python
class UniverseNovedadORM(Base):
    """Especies vistas en los feeds (BYMA open / Data912) que el universo conocido no
    tenía: el registro de triage de la pestaña «Novedades» del ABM. Separada de
    `byma_catalog` a propósito: un re-seed del universo nunca pisa una decisión del
    operador. `estado`: 'nueva' (a decidir) · 'cargada' (ya está en instruments/patas) ·
    'descartada' (el operador dijo que no; reversible). Nunca se borra una fila."""

    __tablename__ = "universe_novedades"

    symbol: Mapped[str] = mapped_column(String, primary_key=True)
    first_seen: Mapped[str] = mapped_column(String)          # 'YYYY-MM-DD' (hora AR)
    source: Mapped[str] = mapped_column(String)              # 'byma' | 'data912'
    categoria: Mapped[Optional[str]] = mapped_column(String, default=None)
    estado: Mapped[str] = mapped_column(String, default="nueva")
    updated_at: Mapped[Optional[str]] = mapped_column(String, default=None)


Index("ix_nov_estado", UniverseNovedadORM.estado)
```

- [ ] **Step 4: Correr y ver que pasa**

Run: `py -3.12 -m pytest "tests/test_universe_novedades.py" -q --tb=long`
Expected: PASS (1 passed).

- [ ] **Step 5: ruff + resto del schema + commit**

Run: `py -3.12 -m ruff check core/infrastructure/db/models.py tests/test_universe_novedades.py`
Expected: sin errores.

Run: `py -3.12 -m pytest tests/test_catalog_migration.py tests/test_byma_universe.py -q`
Expected: PASS.

```bash
git add core/infrastructure/db/models.py tests/test_universe_novedades.py
git commit -m "Novedades del universo: tabla universe_novedades y columnas last_seen/denominacion/vencimiento en byma_catalog"
```

---

### Task 2: Lógica pura — `meta_de`, `clasificar` (diff + guard) y `proximo_despertar`

**Files:**
- Create: `core/infrastructure/byma/novedades.py`
- Test: `tests/test_universe_novedades.py` (agregar)

**Interfaces:**
- Produces:
  - `meta_de(symbol: str, bucket: str) -> dict` con claves `symbol, ticker_pesos, moneda, categoria, security_type, panel, ins_type, clase_liquidacion, cotiza`. `ticker_pesos` sigue la convención del seed CSV: soberanos/letras `AL30D→AL30`, ON `AEC2D→AEC2O` (la pata pesos completa), acciones/cedears `ALUAD→ALUA`.
  - `Diff(altas_catalogo: List[dict], nuevas: List[dict], cargadas: List[str], vistos: int, rechazo: Optional[str])` con `.resumen()`.
  - `clasificar(vistos: Dict[str, str], listados_byma: Set[str], catalogo: Set[str], registradas: Set[str], pendientes: Set[str], cargados: Set[str], *, ref_vistos: Optional[int] = None) -> Diff`. Cada dict de `nuevas`/`altas_catalogo` trae además `source` ('byma'|'data912'). Los dicts de `nuevas` SON los mismos objetos que en `altas_catalogo`.
  - `proximo_despertar(now: datetime, *, hecha_hoy: bool) -> float` (segundos).
  - `CATEGORIAS_SIN_HOJA: frozenset`.

- [ ] **Step 1: Tests puros (rojos)**

Agregar al final de `tests/test_universe_novedades.py`:

```python
# ── diff puro ───────────────────────────────────────────────────────────────
from datetime import datetime  # noqa: E402

from core.infrastructure.byma import novedades as nov  # noqa: E402


def _vistos(n: int, extra: dict | None = None) -> dict:
    """n símbolos conocidos (bucket stocks) + extra {symbol: bucket}."""
    d = {f"K{i:03d}": "stocks" for i in range(n)}
    d.update(extra or {})
    return d


def _conocidos(n: int) -> set:
    return {f"K{i:03d}" for i in range(n)}


def test_meta_de_deriva_categoria_moneda_y_ticker_base_como_el_seed():
    m = nov.meta_de("S29E7", "notes")
    assert m["categoria"] == "Títulos Públicos" and m["panel"] == "Letras"
    assert m["security_type"] == "GO" and m["ins_type"] == "BOND"
    assert m["moneda"] == "ARS" and m["ticker_pesos"] == "S29E7"
    d = nov.meta_de("BPOA8D", "bonds")
    assert d["moneda"] == "MEP" and d["ticker_pesos"] == "BPOA8"
    # ON: el seed agrupa las tres patas bajo la pata PESOS completa (AEC2D → AEC2O)
    c = nov.meta_de("YMCXC", "corp")
    assert c["moneda"] == "cable" and c["ticker_pesos"] == "YMCXO"
    assert c["categoria"] == "Obligaciones Negociables"
    assert (nov.meta_de("YMCXO", "corp")["ticker_pesos"]
            == nov.meta_de("YMCXD", "corp")["ticker_pesos"] == "YMCXO")
    # acciones/cedears: misma convención por sufijo que el seed (ALUAD → ALUA, MEP)
    a = nov.meta_de("ALUAD", "stocks")
    assert a["moneda"] == "MEP" and a["ticker_pesos"] == "ALUA"
    assert nov.meta_de("XXX", "")["categoria"] == "Otros"   # bucket desconocido: no se inventa


def test_un_simbolo_visto_que_nadie_conoce_es_nueva_con_su_procedencia():
    diff = nov.clasificar(_vistos(60, {"S29E7": "notes", "D30O6": "notes"}),
                          listados_byma={"S29E7"}, catalogo=_conocidos(60),
                          registradas=set(), pendientes=set(), cargados=set())
    assert diff.rechazo is None and diff.vistos == 62
    assert [f["symbol"] for f in diff.nuevas] == ["D30O6", "S29E7"]
    por = {f["symbol"]: f["source"] for f in diff.nuevas}
    assert por == {"S29E7": "byma", "D30O6": "data912"}
    assert diff.altas_catalogo == diff.nuevas


def test_lo_conocido_no_es_novedad_ni_entra_al_catalogo():
    diff = nov.clasificar(_vistos(60), set(), catalogo=_conocidos(60),
                          registradas=set(), pendientes=set(), cargados=set())
    assert diff.nuevas == [] and diff.altas_catalogo == []


def test_lo_ya_cargado_en_instruments_entra_al_catalogo_pero_no_es_novedad():
    """Un alta hecha a mano de algo que el CSV no tenía: se conoce el símbolo (va a
    byma_catalog para el Universo) pero no hay nada que decidir."""
    diff = nov.clasificar(_vistos(60, {"TSTX1O": "corp"}), set(), catalogo=_conocidos(60),
                          registradas=set(), pendientes=set(), cargados={"TSTX1O"})
    assert [f["symbol"] for f in diff.altas_catalogo] == ["TSTX1O"]
    assert diff.nuevas == []


def test_una_registrada_que_el_catalogo_perdio_vuelve_al_universo_sin_ser_novedad():
    """byma_catalog re-sembrada a mano (force): la especie vuelve al universo, pero la
    decisión que ya está en universe_novedades se respeta."""
    diff = nov.clasificar(_vistos(60, {"ZZZ1": "corp"}), set(), catalogo=_conocidos(60),
                          registradas={"ZZZ1"}, pendientes=set(), cargados=set())
    assert [f["symbol"] for f in diff.altas_catalogo] == ["ZZZ1"]
    assert diff.nuevas == []


def test_una_pendiente_que_ya_se_cargo_pasa_a_cargada():
    diff = nov.clasificar(_vistos(60, {"S29E7": "notes"}), set(),
                          catalogo=_conocidos(60) | {"S29E7"}, registradas={"S29E7"},
                          pendientes={"S29E7"}, cargados={"S29E7"})
    assert diff.cargadas == ["S29E7"] and diff.nuevas == []


def test_una_descartada_no_vuelve_a_ser_novedad():
    diff = nov.clasificar(_vistos(60, {"ZZZ1": "corp"}), set(),
                          catalogo=_conocidos(60) | {"ZZZ1"}, registradas={"ZZZ1"},
                          pendientes=set(), cargados=set())
    assert diff.nuevas == [] and diff.cargadas == []


@pytest.mark.parametrize("n, ref, motivo", [
    (0, None, "no tiene símbolos"),
    (10, None, "anémica"),
    (60, 1000, "corte parcial"),
])
def test_una_lectura_rota_se_rechaza_entera_sin_decidir_nada(n, ref, motivo):
    diff = nov.clasificar(_vistos(n, {"S29E7": "notes"} if n else None), set(),
                          catalogo=set(), registradas=set(), pendientes={"S29E7"},
                          cargados={"S29E7"}, ref_vistos=ref)
    assert diff.rechazo and motivo in diff.rechazo
    assert diff.nuevas == [] and diff.altas_catalogo == [] and diff.cargadas == []


def test_el_guard_relativo_no_aplica_sin_corrida_anterior():
    diff = nov.clasificar(_vistos(60), set(), catalogo=_conocidos(60), registradas=set(),
                          pendientes=set(), cargados=set(), ref_vistos=None)
    assert diff.rechazo is None


def test_clasificar_no_muta_lo_que_recibe():
    vistos = _vistos(60, {"S29E7": "notes"})
    copia = dict(vistos)
    nov.clasificar(vistos, set(), catalogo=_conocidos(60), registradas=set(),
                   pendientes=set(), cargados=set())
    assert vistos == copia


# ── agenda 08:00 AR ──────────────────────────────────────────────────────────
def _ar(h, m=0, d=10):
    return datetime(2026, 6, d, h, m, tzinfo=AR)


def test_antes_de_las_8_duerme_hasta_las_8():
    assert nov.proximo_despertar(_ar(7, 30), hecha_hoy=False) == 1800.0


def test_despues_de_las_8_sin_corrida_del_dia_corre_ya():
    assert nov.proximo_despertar(_ar(8, 0), hecha_hoy=False) == 0.0
    assert nov.proximo_despertar(_ar(15, 45), hecha_hoy=False) == 0.0


def test_con_la_corrida_hecha_duerme_hasta_manana_a_las_8():
    assert nov.proximo_despertar(_ar(9, 0), hecha_hoy=True) == 23 * 3600.0
    assert nov.proximo_despertar(_ar(7, 0), hecha_hoy=True) == 25 * 3600.0


def test_categorias_sin_hoja_en_el_abm():
    assert {"Acciones", "Cedears", "Índices", "Totales", "Otros"} <= nov.CATEGORIAS_SIN_HOJA
    assert "Obligaciones Negociables" not in nov.CATEGORIAS_SIN_HOJA
    assert "Títulos Públicos" not in nov.CATEGORIAS_SIN_HOJA
```

- [ ] **Step 2: Correr y ver que falla**

Run: `py -3.12 -m pytest tests/test_universe_novedades.py -q --tb=short`
Expected: FAIL con `ModuleNotFoundError: No module named 'core.infrastructure.byma.novedades'`.

- [ ] **Step 3: Crear el módulo (parte pura)**

Crear `core/infrastructure/byma/novedades.py`. **Imports de ESTE corte** (los del store se suman en la Task 3; si vas a hacer Tasks 2 y 3 en la misma sesión antes del gate, podés poner directamente el bloque de imports de la Task 3):

```python
"""Novedades del universo: qué especies aparecieron en los feeds que el universo conocido
no tenía, y el triage de cada una (spec docs/superpowers/specs/2026-09-07-novedades-universo-design.md).

REGLAS DURAS (mismo espíritu que `letras_sync`):

1. **Detección ≠ alta.** Acá no se escribe `instruments` jamás: el alta la dispara el
   operador desde el ABM con el form prefillado.
2. **Nunca se borra.** Ni una fila de `byma_catalog` (el job sólo agrega/actualiza) ni una
   de `universe_novedades` (`descartada` es un ESTADO, no un delete).
3. **Una lectura anémica no decide nada.** Si el hub trae muchos menos símbolos que la
   corrida anterior (server recién arrancado pre-market, breaker abierto), la corrida se
   rechaza entera y se reintenta más tarde.

La lógica de decisión (`clasificar`, `proximo_despertar`) es PURA y recibe el reloj y las
lecturas como parámetros; el acceso a la base vive abajo, en funciones chicas que reciben
la sesión (`*_en(s, ...)`) o abren la suya.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Set

from core.domain.currency import ccy_from_suffix

logger = logging.getLogger(__name__)

ESTADOS = ("nueva", "cargada", "descartada")

# Guard de la lectura entera (criterio hermano de ratings/letras): por debajo del piso
# ABSOLUTO el hub todavía no vio la rueda (arranque pre-market); por debajo del piso
# RELATIVO a la corrida anterior es un corte parcial (breaker abierto, panel vacío).
_MINIMO_VISTOS = 50
_PISO_RELATIVO = 0.6

# Hora de la corrida diaria (AR): antes de la rueda, con el hub todavía con la de ayer.
_HORA_CORRIDA = 8

# bucket del hub (mismo vocabulario en BYMA open y Data912: `ProviderHub.sources()`) →
# cómo se ve la especie en el universo. `security_type`/`panel` espejan los valores del
# seed CSV para que `universe._categoria` y el agrupado del ABM traten igual a una fila
# del job que a una del CSV. Un bucket desconocido cae en "Otros": nunca se inventa.
_BUCKET_META: Dict[str, Dict[str, Optional[str]]] = {
    "notes":   {"security_type": "GO",   "panel": "Letras",             "ins_type": "BOND",   "categoria": "Títulos Públicos"},
    "bonds":   {"security_type": "GO",   "panel": "Titulos Publicos",   "ins_type": "BOND",   "categoria": "Títulos Públicos"},
    "corp":    {"security_type": "CORP", "panel": "Oblig. Negociables", "ins_type": "BOND",   "categoria": "Obligaciones Negociables"},
    "stocks":  {"security_type": "CS",   "panel": "Acciones General",   "ins_type": "EQUITY", "categoria": "Acciones"},
    "cedears": {"security_type": "CD",   "panel": "CEDEARs",            "ins_type": "EQUITY", "categoria": "Cedears"},
}
_META_OTROS: Dict[str, Optional[str]] = {"security_type": None, "panel": None,
                                        "ins_type": None, "categoria": "Otros"}

# Sufijo → vocabulario de `byma_catalog.moneda` (el seed usa ARS/MEP/cable, no CABLE).
_MONEDA_CATALOGO = {"ARS": "ARS", "MEP": "MEP", "CABLE": "cable"}

# Categorías que NO tienen hoja en el ABM: la pestaña Novedades sólo ofrece Descartar
# (las acciones ya se registran solas al arranque; cedears/índices no se precian).
CATEGORIAS_SIN_HOJA = frozenset({
    "Acciones", "Cedears", "Índices", "Totales", "Futuros", "Acciones Internacionales",
    "Otros",
})


def meta_de(symbol: str, bucket: str) -> dict:
    """Fila de `byma_catalog` para un símbolo que vino de los feeds: metadata por bucket
    + moneda por sufijo (`core.domain.currency`, única fuente) + ticker base con la MISMA
    convención del seed CSV — importa porque `search_byma_grouped`/`count_unloaded`
    agrupan por `isin or ticker_pesos or symbol` y `backfill_legs_from_universe` (cada
    arranque) arma las patas por `ticker_pesos`: soberanos/letras `AL30D→AL30`; ON
    `AEC2D→AEC2O` (la pata pesos completa, con su `O`); acciones/cedears `ALUAD→ALUA`."""
    sym = (symbol or "").upper().strip()
    m = _BUCKET_META.get(bucket or "", _META_OTROS)
    ccy = ccy_from_suffix(sym)
    base = sym[:-1] if ccy in ("MEP", "CABLE") and len(sym) > 1 else sym
    if bucket == "corp" and base != sym:
        base += "O"
    return {
        "symbol": sym,
        "ticker_pesos": base,
        "moneda": _MONEDA_CATALOGO[ccy],
        "categoria": m["categoria"],
        "security_type": m["security_type"],
        "panel": m["panel"],
        "ins_type": m["ins_type"],
        "clase_liquidacion": "primary",
        "cotiza": 1,
    }


@dataclass
class Diff:
    """Qué haría la corrida. Nada de esto se ejecutó todavía."""

    altas_catalogo: List[dict] = field(default_factory=list)  # vistos que byma_catalog no tenía
    nuevas: List[dict] = field(default_factory=list)          # ⊆ altas_catalogo: a decidir
    cargadas: List[str] = field(default_factory=list)         # pendientes ya en instruments
    vistos: int = 0
    rechazo: Optional[str] = None

    def resumen(self) -> str:
        if self.rechazo:
            return "universo: corrida RECHAZADA (%s)" % self.rechazo
        return "universo: %d vistos, +%d al catálogo, %d nueva(s), %d pasan a cargada" % (
            self.vistos, len(self.altas_catalogo), len(self.nuevas), len(self.cargadas))


def _guard(n_vistos: int, ref_vistos: Optional[int]) -> Optional[str]:
    """Motivo para descartar la lectura entera, o None si es usable. Mira el TOTAL
    mergeado del hub (BYMA ∪ floor Data912), no una fuente en particular: el hub es
    stale-safe y no expone «qué fuente falló»."""
    if n_vistos == 0:
        return "el hub no tiene símbolos (¿server recién arrancado antes de la rueda?)"
    if n_vistos < _MINIMO_VISTOS:
        return "lectura anémica: %d símbolos (< %d)" % (n_vistos, _MINIMO_VISTOS)
    if ref_vistos and n_vistos < _PISO_RELATIVO * ref_vistos:
        return "corte parcial: %d símbolos < %d%% de los %d de la corrida anterior" % (
            n_vistos, int(_PISO_RELATIVO * 100), ref_vistos)
    return None


def clasificar(vistos: Dict[str, str], listados_byma: Set[str], catalogo: Set[str],
               registradas: Set[str], pendientes: Set[str], cargados: Set[str], *,
               ref_vistos: Optional[int] = None) -> Diff:
    """Diff puro de una corrida. No muta lo que recibe.

    `vistos`: {symbol: bucket} del hub (snapshot ∩ sources). `listados_byma`: símbolos que
    la fuente activa listó (`hub.freshness()`) SI la activa es BYMA; el resto vino del
    floor Data912. `catalogo`: símbolos de `byma_catalog` ANTES del upsert. `registradas`:
    símbolos ya en `universe_novedades` (cualquier estado); `pendientes`: los que siguen
    `nueva`. `cargados`: tickers de `instruments` (primario + patas). `ref_vistos`: cuántos
    símbolos vio la corrida anterior (guard relativo).
    """
    diff = Diff(vistos=len(vistos))
    diff.rechazo = _guard(len(vistos), ref_vistos)
    if diff.rechazo:
        return diff
    for sym, bucket in sorted(vistos.items()):
        sym = (sym or "").upper().strip()
        if not sym or sym in catalogo:
            continue
        fila = meta_de(sym, bucket)
        fila["source"] = "byma" if sym in listados_byma else "data912"
        diff.altas_catalogo.append(fila)
        if sym not in cargados and sym not in registradas:
            diff.nuevas.append(fila)
    diff.cargadas = sorted(p for p in pendientes if p in cargados)
    return diff


def proximo_despertar(now: datetime, *, hecha_hoy: bool) -> float:
    """Segundos hasta la próxima corrida. `now` es aware en hora AR (lo inyecta el loop;
    los tests lo fijan). Hecha hoy → mañana a las 08:00; antes de las 08:00 → hoy a las
    08:00; después, sin hacer → ahora (0)."""
    objetivo = now.replace(hour=_HORA_CORRIDA, minute=0, second=0, microsecond=0)
    if hecha_hoy:
        objetivo += timedelta(days=1)
    elif now >= objetivo:
        return 0.0
    return max(0.0, (objetivo - now).total_seconds())
```

- [ ] **Step 4: Correr y ver que pasa**

Run: `py -3.12 -m pytest tests/test_universe_novedades.py -q --tb=short`
Expected: PASS (todos).

- [ ] **Step 5: ruff + commit**

Run: `py -3.12 -m ruff check core/infrastructure/byma/novedades.py tests/test_universe_novedades.py`
Expected: sin errores.

```bash
git add core/infrastructure/byma/novedades.py tests/test_universe_novedades.py
git commit -m "Novedades del universo: diff puro con guard de lectura rota y agenda de las 08:00 AR"
```

---

### Task 3: Store de `universe_novedades` y `schema_meta`

**Files:**
- Modify: `core/infrastructure/byma/novedades.py` (imports + agregar al final)
- Test: `tests/test_universe_novedades.py` (agregar)

**Interfaces:**
- Produces (con sesión): `registrar_nuevas_en(s, nuevas: Iterable[dict], *, hoy: date) -> List[str]`, `marcar_cargadas_en(s, symbols: Iterable[str]) -> List[str]`, `simbolos_registrados(s) -> Tuple[Set[str], Set[str]]`, `escribir_meta_en(s, key: str, value) -> None`.
- Produces (transacción propia): `marcar_cargadas(symbols) -> List[str]`, `descartar(symbol) -> bool`, `restaurar(symbol) -> bool`, `contar_nuevas() -> int`, `listar(estado="nueva") -> List[dict]` (claves `symbol, first_seen, source, estado, categoria, panel, emisor, denominacion, isin, moneda, vencimiento, cargable`), `agrupadas(estado="nueva") -> List[{"categoria", "filas"}]`, `leer_meta(key) -> Optional[str]`.

- [ ] **Step 1: Tests del store (rojos)**

Agregar al final de `tests/test_universe_novedades.py`:

```python
# ── store ───────────────────────────────────────────────────────────────────
from core.infrastructure.db.engine import SessionLocal  # noqa: E402
from core.infrastructure.db.models import BymaCatalogORM, UniverseNovedadORM  # noqa: E402


def _nueva(symbol, source="byma", categoria="Obligaciones Negociables"):
    return {"symbol": symbol, "source": source, "categoria": categoria}


def test_registrar_solo_agrega_y_nunca_pisa_un_estado(base):
    with SessionLocal.begin() as s:
        assert nov.registrar_nuevas_en(s, [_nueva("AAA1O"), _nueva("BBB2O")], hoy=HOY) == ["AAA1O", "BBB2O"]
    assert nov.descartar("AAA1O") is True
    with SessionLocal.begin() as s:
        assert nov.registrar_nuevas_en(s, [_nueva("AAA1O"), _nueva("CCC3O")], hoy=HOY) == ["CCC3O"]
    with SessionLocal() as s:
        assert s.get(UniverseNovedadORM, "AAA1O").estado == "descartada"
        assert s.get(UniverseNovedadORM, "AAA1O").first_seen == HOY.isoformat()
    assert nov.contar_nuevas() == 2


def test_descartar_y_restaurar_son_transiciones_reversibles_y_no_borran(base):
    with SessionLocal.begin() as s:
        nov.registrar_nuevas_en(s, [_nueva("AAA1O")], hoy=HOY)
    assert nov.descartar("aaa1o") is True          # normaliza a upper
    assert nov.descartar("AAA1O") is False         # ya no está en `nueva`
    assert nov.contar_nuevas() == 0
    assert nov.restaurar("AAA1O") is True
    assert nov.contar_nuevas() == 1
    assert nov.descartar("NOEXISTE") is False
    with SessionLocal() as s:
        assert s.get(UniverseNovedadORM, "AAA1O") is not None


def test_marcar_cargadas_solo_toca_las_pendientes(base):
    with SessionLocal.begin() as s:
        nov.registrar_nuevas_en(s, [_nueva("AAA1O"), _nueva("BBB2O")], hoy=HOY)
    nov.descartar("BBB2O")
    assert nov.marcar_cargadas(["aaa1o", "BBB2O", "ZZZ"]) == ["AAA1O"]
    with SessionLocal() as s:
        assert s.get(UniverseNovedadORM, "AAA1O").estado == "cargada"
        assert s.get(UniverseNovedadORM, "BBB2O").estado == "descartada"
        todas, pend = nov.simbolos_registrados(s)
    assert todas == {"AAA1O", "BBB2O"} and pend == set()


def test_listar_cruza_con_byma_catalog_y_marca_si_es_cargable(base):
    with SessionLocal.begin() as s:
        s.add(BymaCatalogORM(symbol="AAA1O", categoria="Obligaciones Negociables",
                             emisor="ACME S.A.", isin="ARACME000001", moneda="ARS",
                             vencimiento="2028-03-31", denominacion="ON ACME CL 1"))
        nov.registrar_nuevas_en(s, [_nueva("AAA1O"), _nueva("GGAL2", categoria="Acciones")],
                                hoy=HOY)
    filas = {f["symbol"]: f for f in nov.listar("nueva")}
    assert filas["AAA1O"]["emisor"] == "ACME S.A."
    assert filas["AAA1O"]["vencimiento"] == "2028-03-31"
    assert filas["AAA1O"]["cargable"] is True
    assert filas["GGAL2"]["categoria"] == "Acciones"   # sin fila en byma_catalog: la de la novedad
    assert filas["GGAL2"]["cargable"] is False
    grupos = nov.agrupadas("nueva")
    assert [g["categoria"] for g in grupos] == ["Acciones", "Obligaciones Negociables"]


def test_meta_se_lee_y_escribe_en_schema_meta(base):
    assert nov.leer_meta("universe_ultima_corrida") is None
    with SessionLocal.begin() as s:
        nov.escribir_meta_en(s, "universe_ultima_corrida", HOY.isoformat())
        nov.escribir_meta_en(s, "universe_ultimos_vistos", 1234)
    assert nov.leer_meta("universe_ultima_corrida") == HOY.isoformat()
    assert nov.leer_meta("universe_ultimos_vistos") == "1234"
    with SessionLocal.begin() as s:
        nov.escribir_meta_en(s, "universe_ultimos_vistos", 99)     # upsert, no duplica
    assert nov.leer_meta("universe_ultimos_vistos") == "99"
```

- [ ] **Step 2: Correr y ver que falla**

Run: `py -3.12 -m pytest tests/test_universe_novedades.py -q --tb=short`
Expected: FAIL con `AttributeError: module ... has no attribute 'registrar_nuevas_en'`.

- [ ] **Step 3: Implementar el store**

Reemplazar el bloque de imports de `core/infrastructure/byma/novedades.py` por:

```python
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Dict, Iterable, List, Optional, Set, Tuple

from sqlalchemy import func, inspect, select

from core.domain.currency import ccy_from_suffix
from core.infrastructure.db.catalog_repository import init_db
from core.infrastructure.db.engine import SessionLocal, get_engine
from core.infrastructure.db.models import BymaCatalogORM, UniverseNovedadORM
```

y agregar al final del archivo:

```python
# ── store: `universe_novedades` + claves propias en `schema_meta` ──────────────
def _ahora() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _norm(symbol) -> str:
    return str(symbol or "").upper().strip()


def registrar_nuevas_en(s, nuevas: Iterable[dict], *, hoy) -> List[str]:
    """Inserta como `nueva` las que NO existen. Una fila existente (en cualquier estado)
    se respeta: el operador ya decidió, o el job ya la vio."""
    out: List[str] = []
    for f in nuevas:
        sym = _norm(f.get("symbol"))
        if not sym or s.get(UniverseNovedadORM, sym) is not None:
            continue
        s.add(UniverseNovedadORM(symbol=sym, first_seen=hoy.isoformat(),
                                 source=f.get("source") or "data912",
                                 categoria=f.get("categoria"), estado="nueva",
                                 updated_at=_ahora()))
        out.append(sym)
    return sorted(out)


def marcar_cargadas_en(s, symbols: Iterable[str]) -> List[str]:
    """`nueva` → `cargada` para los símbolos dados (sólo las pendientes)."""
    syms = {_norm(t) for t in symbols if t}
    if not syms:
        return []
    filas = s.execute(select(UniverseNovedadORM).where(
        UniverseNovedadORM.symbol.in_(sorted(syms)),
        UniverseNovedadORM.estado == "nueva")).scalars().all()
    for f in filas:
        f.estado = "cargada"
        f.updated_at = _ahora()
    return sorted(f.symbol for f in filas)


def marcar_cargadas(symbols: Iterable[str]) -> List[str]:
    init_db()
    with SessionLocal.begin() as s:
        return marcar_cargadas_en(s, symbols)


def _cambiar_estado(symbol: str, desde: str, hacia: str) -> bool:
    init_db()
    with SessionLocal.begin() as s:
        f = s.get(UniverseNovedadORM, _norm(symbol))
        if f is None or f.estado != desde:
            return False
        f.estado = hacia
        f.updated_at = _ahora()
        return True


def descartar(symbol: str) -> bool:
    """`nueva` → `descartada` (reversible con `restaurar`). Nunca borra."""
    return _cambiar_estado(symbol, "nueva", "descartada")


def restaurar(symbol: str) -> bool:
    return _cambiar_estado(symbol, "descartada", "nueva")


def contar_nuevas() -> int:
    init_db()
    with SessionLocal() as s:
        n = s.execute(select(func.count()).select_from(UniverseNovedadORM)
                      .where(UniverseNovedadORM.estado == "nueva")).scalar()
    return int(n or 0)


def simbolos_registrados(s) -> Tuple[Set[str], Set[str]]:
    """(todas, pendientes): lo que ya está en `universe_novedades` y, de eso, lo que
    sigue en `nueva`."""
    todas: Set[str] = set()
    pend: Set[str] = set()
    for sym, estado in s.execute(select(UniverseNovedadORM.symbol,
                                        UniverseNovedadORM.estado)).all():
        todas.add(sym)
        if estado == "nueva":
            pend.add(sym)
    return todas, pend


def listar(estado: str = "nueva") -> List[dict]:
    """Novedades en `estado` con la metadata de `byma_catalog` (outer join: una novedad
    sin fila en el universo muestra lo que guardó al detectarse)."""
    init_db()
    with SessionLocal() as s:
        rows = s.execute(
            select(UniverseNovedadORM, BymaCatalogORM)
            .outerjoin(BymaCatalogORM, BymaCatalogORM.symbol == UniverseNovedadORM.symbol)
            .where(UniverseNovedadORM.estado == estado)
            .order_by(UniverseNovedadORM.first_seen.desc(), UniverseNovedadORM.symbol)
        ).all()
    out: List[dict] = []
    for n, u in rows:
        categoria = (u.categoria if u else None) or n.categoria or "Otros"
        out.append({
            "symbol": n.symbol, "first_seen": n.first_seen, "source": n.source,
            "estado": n.estado, "categoria": categoria,
            "panel": u.panel if u else None, "emisor": u.emisor if u else None,
            "denominacion": u.denominacion if u else None, "isin": u.isin if u else None,
            "moneda": u.moneda if u else None, "vencimiento": u.vencimiento if u else None,
            "cargable": categoria not in CATEGORIAS_SIN_HOJA,
        })
    return out


def agrupadas(estado: str = "nueva") -> List[dict]:
    """[{categoria, filas}] ordenado por categoría (estable, para que la pestaña no
    salte de orden entre refrescos)."""
    grupos: Dict[str, List[dict]] = {}
    for f in listar(estado):
        grupos.setdefault(f["categoria"], []).append(f)
    return [{"categoria": c, "filas": fs} for c, fs in sorted(grupos.items())]


def leer_meta(key: str) -> Optional[str]:
    """Valor de una clave propia en `schema_meta` (None si no existe). Mismo mecanismo
    que `catalog_repository._stamp_schema_version` y los scripts de backfill."""
    eng = get_engine()
    if not inspect(eng).has_table("schema_meta"):
        return None
    with eng.begin() as conn:
        row = conn.exec_driver_sql("SELECT value FROM schema_meta WHERE key=?",
                                   (key,)).fetchone()
    return row[0] if row else None


def escribir_meta_en(s, key: str, value) -> None:
    """Upsert de una clave propia en `schema_meta`, dentro de la transacción `s`."""
    s.connection().exec_driver_sql(
        "INSERT INTO schema_meta (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, str(value)),
    )
```

- [ ] **Step 4: Correr y ver que pasa**

Run: `py -3.12 -m pytest tests/test_universe_novedades.py -q --tb=short`
Expected: PASS.

- [ ] **Step 5: ruff + commit**

Run: `py -3.12 -m ruff check core/infrastructure/byma/novedades.py tests/test_universe_novedades.py`

```bash
git add core/infrastructure/byma/novedades.py tests/test_universe_novedades.py
git commit -m "Novedades del universo: store de universe_novedades (solo agrega, estados reversibles) y claves en schema_meta"
```

---

### Task 4: Borde — `apps/web/universe_service.py::sincronizar_universo`

**Files:**
- Create: `apps/web/universe_service.py`
- Test: `tests/test_universe_service.py`

**Interfaces:**
- Consumes: `novedades.clasificar/meta_de/registrar_nuevas_en/marcar_cargadas_en/simbolos_registrados/leer_meta/escribir_meta_en/contar_nuevas`; `universe._loaded_ids`, `universe._categoria`; `catalog_enrich._ficha_raw/_ficha_session/_curate_ficha`; hub con `.snapshot()`, `.sources()`, `.freshness()`, `.active_mode`.
- Produces: `sincronizar_universo(hub, *, hoy: date, ficha_fn: Optional[Callable[[str], Optional[dict]]] = None, max_fichas: int = 200) -> Resultado`; `Resultado(vistos, altas_catalogo, nuevas: List[str], cargadas: List[str], pendientes: int, fichas: int, rechazo)` con `.resumen()`; `ultima_corrida() -> Optional[str]`; `META_ULTIMA_CORRIDA = "universe_ultima_corrida"`, `META_ULTIMOS_VISTOS = "universe_ultimos_vistos"`.
- `ficha_fn(symbol) -> Optional[dict]` con claves `isin, emisor, denominacion, tipo_especie, vencimiento` (todas opcionales).
- Rechaza sin tocar nada (y sin sellar el día) si: el guard de `clasificar` dispara, `byma_catalog` está vacío (sin línea de base), o ya hay una corrida en curso (lock de módulo).

- [ ] **Step 1: Tests del borde con hub falso (rojos)**

Crear `tests/test_universe_service.py`:

```python
"""Borde del job de novedades: lee el hub, escribe byma_catalog + universe_novedades en
una transacción, nunca instruments (spec 2026-09-07 §2)."""
from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from apps.web import universe_service as svc
from core.infrastructure.byma import novedades as nov
from core.infrastructure.db.catalog_repository import init_db
from core.infrastructure.db.engine import SessionLocal, configure
from core.infrastructure.db.models import BymaCatalogORM, InstrumentORM, UniverseNovedadORM
from tests._clock import ref_date

HOY = ref_date()


@pytest.fixture
def base(tmp_path):
    from config.settings import settings
    configure(tmp_path / "svc.db")
    init_db()
    try:
        yield
    finally:
        configure(settings.catalog_db)


class _Hub:
    """Doble del ProviderHub: snapshot/sources/freshness/active_mode con la forma real."""

    def __init__(self, vistos: dict, frescos=(), active_mode="byma_open"):
        self._vistos = dict(vistos)
        self._frescos = set(frescos)
        self.active_mode = active_mode

    def snapshot(self, settle="24"):
        return {s: SimpleNamespace(c=100.0, v=None, q_op=None) for s in self._vistos}

    def sources(self):
        return dict(self._vistos)

    def freshness(self):
        return {s: 1.0 for s in self._frescos}


def _seed_catalogo(n: int) -> None:
    with SessionLocal.begin() as s:
        s.add_all([BymaCatalogORM(symbol=f"K{i:03d}", ticker_pesos=f"K{i:03d}", moneda="ARS",
                                  categoria="Acciones", clase_liquidacion="primary", cotiza=1)
                   for i in range(n)])


def _hub(extra: dict, frescos=(), n=60) -> _Hub:
    vistos = {f"K{i:03d}": "stocks" for i in range(n)}
    vistos.update(extra)
    return _Hub(vistos, frescos)


def _fila(symbol):
    with SessionLocal() as s:
        return s.get(BymaCatalogORM, symbol)


def _nov(symbol):
    with SessionLocal() as s:
        return s.get(UniverseNovedadORM, symbol)


def _sin_ficha(_symbol):
    return None


def test_una_especie_nueva_entra_al_catalogo_y_a_novedades(base):
    _seed_catalogo(60)
    res = svc.sincronizar_universo(_hub({"S29E7": "notes"}, frescos={"S29E7"}),
                                   hoy=HOY, ficha_fn=_sin_ficha)
    assert res.rechazo is None
    assert res.nuevas == ["S29E7"] and res.altas_catalogo == 1 and res.pendientes == 1
    f = _fila("S29E7")
    assert f.panel == "Letras" and f.categoria == "Títulos Públicos"
    assert f.last_seen == HOY.isoformat() and f.moneda == "ARS"
    n = _nov("S29E7")
    assert n.estado == "nueva" and n.source == "byma" and n.first_seen == HOY.isoformat()
    assert _fila("K001").last_seen == HOY.isoformat()      # lo visto se marca


def test_con_data912_activa_la_procedencia_no_dice_byma(base):
    """`freshness()` lista lo que trajo la ACTIVA, sea cual sea: con Data912 activa todo
    vino de Data912 aunque esté 'fresco'."""
    _seed_catalogo(60)
    hub = _hub({"S29E7": "notes"}, frescos={"S29E7"})
    hub.active_mode = "data912"
    svc.sincronizar_universo(hub, hoy=HOY, ficha_fn=_sin_ficha)
    assert _nov("S29E7").source == "data912"


def test_lo_que_desaparece_del_feed_no_se_borra_ni_se_toca(base):
    _seed_catalogo(60)
    with SessionLocal.begin() as s:
        s.add(BymaCatalogORM(symbol="ZZZ1", categoria="Obligaciones Negociables"))
    svc.sincronizar_universo(_hub({}), hoy=HOY, ficha_fn=_sin_ficha)
    f = _fila("ZZZ1")
    assert f is not None and f.last_seen is None


def test_un_simbolo_ya_cargado_en_instruments_no_es_novedad(base):
    _seed_catalogo(60)
    with SessionLocal.begin() as s:
        s.merge(InstrumentORM(ticker="TSTX1O", ticker_mep="TSTX1D"))
    res = svc.sincronizar_universo(_hub({"TSTX1D": "corp"}), hoy=HOY, ficha_fn=_sin_ficha)
    assert res.nuevas == [] and res.altas_catalogo == 1
    assert _fila("TSTX1D") is not None and _nov("TSTX1D") is None


def test_una_pendiente_que_se_cargo_pasa_a_cargada(base):
    _seed_catalogo(60)
    with SessionLocal.begin() as s:
        s.add(BymaCatalogORM(symbol="S29E7", categoria="Títulos Públicos"))
        nov.registrar_nuevas_en(s, [{"symbol": "S29E7", "source": "byma",
                                     "categoria": "Títulos Públicos"}], hoy=HOY)
        s.merge(InstrumentORM(ticker="S29E7"))
    res = svc.sincronizar_universo(_hub({"S29E7": "notes"}), hoy=HOY, ficha_fn=_sin_ficha)
    assert res.cargadas == ["S29E7"] and res.pendientes == 0
    assert _nov("S29E7").estado == "cargada"


def test_una_descartada_no_vuelve(base):
    _seed_catalogo(60)
    with SessionLocal.begin() as s:
        s.add(BymaCatalogORM(symbol="ZZZ1", categoria="Obligaciones Negociables"))
        nov.registrar_nuevas_en(s, [{"symbol": "ZZZ1", "source": "byma",
                                     "categoria": "Obligaciones Negociables"}], hoy=HOY)
    nov.descartar("ZZZ1")
    res = svc.sincronizar_universo(_hub({"ZZZ1": "corp"}), hoy=HOY, ficha_fn=_sin_ficha)
    assert res.nuevas == [] and _nov("ZZZ1").estado == "descartada"


def test_hub_vacio_rechaza_y_no_toca_nada_ni_sella_el_dia(base):
    _seed_catalogo(60)
    res = svc.sincronizar_universo(_Hub({}), hoy=HOY, ficha_fn=_sin_ficha)
    assert res.rechazo and res.nuevas == []
    assert _fila("K001").last_seen is None
    assert svc.ultima_corrida() is None


def test_sin_universo_sembrado_no_hay_diff(base):
    """Sin línea de base (byma_catalog vacío: la siembra no corrió o falló) una corrida
    marcaría TODO el feed como novedad y dejaría la siembra en no-op para siempre."""
    from core.infrastructure.byma import universe
    res = svc.sincronizar_universo(_hub({"S29E7": "notes"}), hoy=HOY, ficha_fn=_sin_ficha)
    assert res.rechazo and "vacío" in res.rechazo
    assert res.altas_catalogo == 0 and _fila("S29E7") is None and _nov("S29E7") is None
    assert svc.ultima_corrida() is None and universe.count() == 0


def test_lectura_parcial_contra_la_corrida_anterior_se_rechaza(base):
    _seed_catalogo(60)
    with SessionLocal.begin() as s:
        nov.escribir_meta_en(s, svc.META_ULTIMOS_VISTOS, 1000)
    res = svc.sincronizar_universo(_hub({"S29E7": "notes"}), hoy=HOY, ficha_fn=_sin_ficha)
    assert res.rechazo and "corte parcial" in res.rechazo
    assert _nov("S29E7") is None


def test_la_corrida_buena_sella_el_dia_y_los_vistos(base):
    _seed_catalogo(60)
    svc.sincronizar_universo(_hub({"S29E7": "notes"}), hoy=HOY, ficha_fn=_sin_ficha)
    assert svc.ultima_corrida() == HOY.isoformat()
    assert nov.leer_meta(svc.META_ULTIMOS_VISTOS) == "61"


def test_la_ficha_enriquece_isin_emisor_vencimiento_y_categoria(base):
    _seed_catalogo(60)

    def ficha(symbol):
        assert symbol == "YMCXO"
        return {"isin": "ARYPFS000001", "emisor": "YPF S.A.", "denominacion": "ON YPF CL X",
                "tipo_especie": "Obligaciones Negociables", "vencimiento": "2029-06-30"}

    res = svc.sincronizar_universo(_hub({"YMCXO": "corp"}), hoy=HOY, ficha_fn=ficha)
    assert res.fichas == 1
    f = _fila("YMCXO")
    assert f.isin == "ARYPFS000001" and f.emisor == "YPF S.A."
    assert f.vencimiento == "2029-06-30" and f.denominacion == "ON YPF CL X"
    assert f.categoria == "Obligaciones Negociables"
    assert _nov("YMCXO").categoria == "Obligaciones Negociables"


def test_una_ficha_rota_no_frena_la_corrida(base):
    _seed_catalogo(60)

    def ficha(_symbol):
        raise RuntimeError("BYMA 503")

    res = svc.sincronizar_universo(_hub({"YMCXO": "corp"}), hoy=HOY, ficha_fn=ficha)
    assert res.nuevas == ["YMCXO"] and res.fichas == 0
    assert _fila("YMCXO").isin is None


def test_la_ficha_se_pide_solo_para_lo_nuevo_y_con_tope(base):
    _seed_catalogo(60)
    pedidas = []

    def ficha(symbol):
        pedidas.append(symbol)
        return None

    extra = {f"N{i:02d}O": "corp" for i in range(5)}
    svc.sincronizar_universo(_hub(extra), hoy=HOY, ficha_fn=ficha, max_fichas=3)
    assert len(pedidas) == 3 and all(p.startswith("N") for p in pedidas)


def test_una_corrida_que_revienta_a_mitad_no_deja_nada_a_medias(base, monkeypatch):
    """Spec §5: diff+upsert son UNA transacción. `escribir_meta_en` es la última escritura
    del bloque: si revienta, catálogo, last_seen y novedades tienen que volver atrás."""
    _seed_catalogo(60)

    def boom(s, key, value):
        raise RuntimeError("fallo simulado a mitad de la corrida")

    monkeypatch.setattr(nov, "escribir_meta_en", boom)
    with pytest.raises(RuntimeError):
        svc.sincronizar_universo(_hub({"S29E7": "notes"}), hoy=HOY, ficha_fn=_sin_ficha)
    assert _fila("S29E7") is None                 # el alta al catálogo se deshizo
    assert _nov("S29E7") is None                  # la novedad también
    assert _fila("K001").last_seen is None        # y el last_seen de lo visto
    assert svc.ultima_corrida() is None


def test_dos_corridas_solapadas_no_se_pisan(base):
    """El loop de las 08:00 y «Refrescar ahora» corren en hilos distintos: las dos leerían
    el mismo `catalogo` y la segunda moriría insertando el mismo PK. Una por vez."""
    _seed_catalogo(60)
    adentro, soltar = threading.Event(), threading.Event()

    def ficha_lenta(_symbol):
        adentro.set()
        soltar.wait(5)
        return None

    out = {}

    def primera():
        out["r1"] = svc.sincronizar_universo(_hub({"S29E7": "notes"}), hoy=HOY,
                                             ficha_fn=ficha_lenta)

    t = threading.Thread(target=primera)
    t.start()
    try:
        assert adentro.wait(5)
        r2 = svc.sincronizar_universo(_hub({"S29E7": "notes"}), hoy=HOY, ficha_fn=_sin_ficha)
    finally:
        soltar.set()
        t.join(5)
    assert r2.rechazo and "en curso" in r2.rechazo
    assert out["r1"].nuevas == ["S29E7"] and _fila("S29E7") is not None


def test_jamas_escribe_instruments(base):
    _seed_catalogo(60)
    svc.sincronizar_universo(_hub({"S29E7": "notes"}), hoy=HOY, ficha_fn=_sin_ficha)
    with SessionLocal() as s:
        assert s.execute(select(InstrumentORM)).scalars().all() == []
```

- [ ] **Step 2: Correr y ver que falla**

Run: `py -3.12 -m pytest tests/test_universe_service.py -q --tb=short`
Expected: FAIL con `ModuleNotFoundError: No module named 'apps.web.universe_service'`.

- [ ] **Step 3: Crear el servicio**

Crear `apps/web/universe_service.py`:

```python
"""Borde del job diario de novedades del universo (spec 2026-09-07 §2).

Lee el hub (snapshot ACUMULADO: a las 08:00 BYMA responde `data: []` y un fetch fresco
rechazaría la corrida todas las mañanas), escribe `byma_catalog` (solo agrega; `last_seen`
para lo visto) y `universe_novedades` (solo agrega; `nueva` → `cargada`), sella la corrida
en `schema_meta`, y NUNCA escribe `instruments`. Sync: corre en `to_thread` desde
`app._universe_loop` y desde «Refrescar ahora» (POST admin del ABM), una corrida por vez.

Las filas que este job agrega a `byma_catalog` alimentan, en el próximo arranque,
`_universe_groups`/`backfill_legs_from_universe` (app.py `_backfill_legs`): por eso
`ticker_pesos`/`moneda` siguen la convención del seed (ver `novedades.meta_de`).
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Callable, Dict, List, Optional, Set

from sqlalchemy import select, update

from core.infrastructure.byma import novedades as nov
from core.infrastructure.byma.universe import _categoria, _loaded_ids
from core.infrastructure.db.catalog_repository import init_db
from core.infrastructure.db.engine import SessionLocal
from core.infrastructure.db.models import BymaCatalogORM

logger = logging.getLogger(__name__)
_audit = logging.getLogger("monitor.audit")

META_ULTIMA_CORRIDA = "universe_ultima_corrida"   # 'YYYY-MM-DD' (hora AR)
META_ULTIMOS_VISTOS = "universe_ultimos_vistos"   # cuántos símbolos vio (guard relativo)
_MAX_FICHAS = 200   # fichas BYMA por corrida (una POST sync por símbolo); el resto, mañana

# Una corrida por vez: el loop de las 08:00 y «Refrescar ahora» corren en hilos distintos
# y las dos leerían el mismo `catalogo` para después insertar el mismo PK.
_CORRIDA = threading.Lock()

FichaFn = Callable[[str], Optional[dict]]


@dataclass
class Resultado:
    vistos: int = 0
    altas_catalogo: int = 0
    nuevas: List[str] = field(default_factory=list)
    cargadas: List[str] = field(default_factory=list)
    pendientes: int = 0          # novedades en `nueva` al terminar (lo que muestra el badge)
    fichas: int = 0
    rechazo: Optional[str] = None

    def resumen(self) -> str:
        if self.rechazo:
            return "universo: corrida RECHAZADA (%s); %d pendiente(s)" % (
                self.rechazo, self.pendientes)
        return ("universo: %d vistos, +%d al catálogo, %d nueva(s), %d pasan a cargada, "
                "%d ficha(s), %d pendiente(s)" % (
                    self.vistos, self.altas_catalogo, len(self.nuevas), len(self.cargadas),
                    self.fichas, self.pendientes))


def ultima_corrida() -> Optional[str]:
    """'YYYY-MM-DD' de la última corrida sellada, o None."""
    return nov.leer_meta(META_ULTIMA_CORRIDA)


def _ficha_byma_factory() -> FichaFn:
    """Ficha técnica BYMA por símbolo (sync, `requests`, best-effort: None si no hay).
    Reusa los primitivos de `catalog_enrich`; una sesión para toda la corrida."""
    from core.infrastructure.byma.catalog_enrich import (
        _curate_ficha, _ficha_raw, _ficha_session,
    )
    session = _ficha_session()

    def _ficha(symbol: str) -> Optional[dict]:
        raw = _ficha_raw(session, symbol)
        if not raw:
            return None
        cur = _curate_ficha(raw)
        return {
            "isin": raw.get("codigoIsin") or None,
            "emisor": raw.get("emisor") or None,
            "denominacion": raw.get("denominacion") or None,
            "tipo_especie": cur.get("tipo_especie"),
            "vencimiento": cur.get("fecha_vencimiento"),
        }
    return _ficha


def _marcar_last_seen(s, symbols: Set[str], hoy_iso: str) -> None:
    syms = sorted(symbols)
    for i in range(0, len(syms), 500):   # SQLite limita las variables por statement
        s.execute(update(BymaCatalogORM)
                  .where(BymaCatalogORM.symbol.in_(syms[i:i + 500]))
                  .values(last_seen=hoy_iso))


def _rechazo(vistos: int, motivo: str) -> Resultado:
    logger.warning("universo: %s", motivo)
    return Resultado(vistos=vistos, rechazo=motivo, pendientes=nov.contar_nuevas())


def sincronizar_universo(hub, *, hoy: date, ficha_fn: Optional[FichaFn] = None,
                         max_fichas: int = _MAX_FICHAS) -> Resultado:
    """Una corrida. Devuelve qué pasó; con `rechazo` no se tocó nada ni se selló el día."""
    if not _CORRIDA.acquire(blocking=False):
        return _rechazo(0, "ya hay una corrida en curso")
    try:
        return _sincronizar(hub, hoy=hoy, ficha_fn=ficha_fn, max_fichas=max_fichas)
    finally:
        _CORRIDA.release()


def _sincronizar(hub, *, hoy: date, ficha_fn: Optional[FichaFn], max_fichas: int) -> Resultado:
    snapshot = hub.snapshot()
    sources = hub.sources()
    # `freshness()` lista lo que trajo la fuente ACTIVA, sea cual sea: sólo es «byma» si
    # la activa es BYMA (open/realtime); con Data912 activa todo vino de Data912.
    activa = str(getattr(hub, "active_mode", "") or "")
    frescos = set(hub.freshness()) if activa.startswith("byma") else set()
    vistos: Dict[str, str] = {sym: sources.get(sym, "") for sym in snapshot}

    init_db()
    ref = nov.leer_meta(META_ULTIMOS_VISTOS)
    with SessionLocal() as s:
        catalogo = {r[0].upper() for r in s.execute(select(BymaCatalogORM.symbol)).all() if r[0]}
        registradas, pendientes = nov.simbolos_registrados(s)
    if not catalogo:
        # Sin línea de base el diff marcaría TODO el feed como novedad y dejaría la
        # siembra del CSV (`app._seed_byma_universe`, sólo si vacía) en no-op para siempre.
        return _rechazo(len(vistos), "byma_catalog vacío: falta la siembra del universo "
                                     "(sin línea de base para el diff)")
    _isins, cargados = _loaded_ids()

    diff = nov.clasificar(vistos, frescos, catalogo, registradas, pendientes, cargados,
                          ref_vistos=int(ref) if ref and ref.isdigit() else None)
    if diff.rechazo:
        return _rechazo(diff.vistos, diff.rechazo)
    res = Resultado(vistos=diff.vistos)

    # Ficha técnica SOLO para símbolos nuevos en el catálogo. Red, FUERA de la transacción.
    ficha_fn = ficha_fn or _ficha_byma_factory()
    fichas: Dict[str, dict] = {}
    for fila in diff.altas_catalogo[:max_fichas]:
        try:
            f = ficha_fn(fila["symbol"])
        except Exception as e:  # noqa: BLE001 — la ficha es best-effort
            logger.debug("ficha %s falló: %s", fila["symbol"], e)
            f = None
        if f:
            fichas[fila["symbol"]] = f
    if len(diff.altas_catalogo) > max_fichas:
        logger.info("universo: %d símbolos nuevos sin ficha esta corrida (tope %d)",
                    len(diff.altas_catalogo) - max_fichas, max_fichas)

    hoy_iso = hoy.isoformat()
    ahora = datetime.now().isoformat(timespec="seconds")
    with SessionLocal.begin() as s:
        for fila in diff.altas_catalogo:
            f = fichas.get(fila["symbol"]) or {}
            campos = {k: v for k, v in fila.items() if k != "source"}
            campos["categoria"] = _categoria(f.get("tipo_especie") or "",
                                             campos["security_type"] or "",
                                             campos["panel"] or "")
            fila["categoria"] = campos["categoria"]   # la novedad guarda la misma categoría
            s.add(BymaCatalogORM(**campos, isin=f.get("isin"), emisor=f.get("emisor"),
                                 denominacion=f.get("denominacion"),
                                 vencimiento=f.get("vencimiento"),
                                 last_seen=hoy_iso, updated_at=ahora))
        _marcar_last_seen(s, {sym for sym in vistos if sym in catalogo}, hoy_iso)
        res.nuevas = nov.registrar_nuevas_en(s, diff.nuevas, hoy=hoy)
        res.cargadas = nov.marcar_cargadas_en(s, diff.cargadas)
        nov.escribir_meta_en(s, META_ULTIMA_CORRIDA, hoy_iso)
        nov.escribir_meta_en(s, META_ULTIMOS_VISTOS, diff.vistos)
    res.altas_catalogo = len(diff.altas_catalogo)
    res.fichas = len(fichas)
    for sym in res.nuevas:
        # Deja rastro en journald: es lo que va a mirar el operador cuando pregunte
        # "¿desde cuándo está esto?".
        _audit.info("universo action=novedad symbol=%s", sym, extra={"console": True})
    res.pendientes = nov.contar_nuevas()
    return res
```

- [ ] **Step 4: Correr y ver que pasa**

Run: `py -3.12 -m pytest tests/test_universe_service.py -q --tb=short`
Expected: PASS (16 passed).

- [ ] **Step 5: ruff + commit**

Run: `py -3.12 -m ruff check apps/web/universe_service.py tests/test_universe_service.py`

```bash
git add apps/web/universe_service.py tests/test_universe_service.py
git commit -m "Novedades del universo: servicio de sincronización (hub → byma_catalog + universe_novedades, una transacción bajo lock, ficha best-effort)"
```

---

### Task 5: La siembra de `byma_catalog` sale de `_startup_reconcile`, va al lifespan (antes de los loops) y sólo corre sobre la tabla vacía

**Files:**
- Modify: `apps/web/app.py` (`_seed_byma_universe` nueva; `_startup_reconcile:288-334`; lifespan `:669-673`)
- Modify: `core/infrastructure/byma/universe.py:1-6` (docstring) y `:46-89` (`ingest_byma_catalog`)
- Modify: `tests/test_ratings_loop.py::_stub_loops`, `tests/test_aud_D1_seguridad_web.py:262-265`, `tests/test_rem_R3_web_state_severity.py:209-212` (stub de `_seed_byma_universe`)
- Test: `tests/test_universe_service.py` (agregar), `tests/test_byma_universe.py` (agregar)

**Interfaces:**
- Produces: `app._seed_byma_universe() -> int` (sync; corre en `to_thread` desde el lifespan, ANTES de crear cualquier task; `_startup_reconcile` ya no siembra); `universe.ingest_byma_catalog(csv_path=None, *, force=False)` levanta `RuntimeError` si hay filas con `last_seen` y no `force`.

- [ ] **Step 1: Tests (rojos)**

Agregar al final de `tests/test_universe_service.py`:

```python
# ── siembra del universo: sólo si está vacía, en el lifespan, nunca por debajo del job ─
def test_seed_byma_universe_siembra_solo_la_tabla_vacia(base, monkeypatch):
    """`ingest_byma_catalog` es DELETE+INSERT: correrla sobre una tabla poblada borraba lo
    que el job diario agrega (S29E7, last_seen). Ahora sólo siembra la tabla VACÍA."""
    from apps.web import app as app_mod
    from core.infrastructure.byma import universe
    llamadas = []
    monkeypatch.setattr(universe, "ingest_byma_catalog",
                        lambda *a, **k: llamadas.append(1) or 7)

    assert app_mod._seed_byma_universe() == 7          # vacía → siembra
    assert llamadas == [1]
    with SessionLocal.begin() as s:
        s.add(BymaCatalogORM(symbol="S29E7", categoria="Títulos Públicos", last_seen="2026-09-07"))
    assert app_mod._seed_byma_universe() == 0          # poblada → ni la toca
    assert llamadas == [1]
    assert _fila("S29E7").last_seen == "2026-09-07"


def test_startup_reconcile_ya_no_siembra_el_universo(base, monkeypatch):
    """La siembra era el 6º paso de `_startup_reconcile` (después de minutos de fichas, en
    paralelo con el primer diff del job). Ahora vive en el lifespan: reconciliar no toca
    `ingest_byma_catalog` ni `_seed_byma_universe`, y llega hasta el final."""
    import asyncio

    from apps.web import app as app_mod
    from apps.web.state import AppState
    from core.infrastructure.byma import catalog_enrich, universe
    llamadas = []
    monkeypatch.setattr(universe, "ingest_byma_catalog",
                        lambda *a, **k: llamadas.append("ingest") or 0)
    monkeypatch.setattr(app_mod, "_seed_byma_universe", lambda: llamadas.append("seed") or 0)
    monkeypatch.setattr(app_mod, "_reconcile_catalog", lambda hub: 0)
    monkeypatch.setattr(app_mod, "_backfill_legs", lambda: llamadas.append("legs") or 0)
    for fn in ("enrich_isin_from_byma", "enrich_isin_from_ficha", "enrich_ficha_meta"):
        monkeypatch.setattr(catalog_enrich, fn, lambda *a, **k: 0)
    monkeypatch.setattr(app_mod, "get_repo", lambda: SimpleNamespace(
        type_health={"orphans": [], "defaulted": []}, seed_error=None,
        get_all_instruments=lambda: [], reload=lambda: None))

    class _HubBoot:
        async def refresh_all(self):
            return {}

    fake_app = SimpleNamespace(state=SimpleNamespace(hub=_HubBoot(), app_state=AppState()))
    asyncio.run(app_mod._startup_reconcile(fake_app))
    assert llamadas == ["legs"], llamadas      # llegó al final SIN sembrar


def _stub_loops_para_siembra(monkeypatch, evento):
    """Loops y reconcile por no-ops; la siembra avisa por `evento`. (La Task 6 suma
    `_universe_loop` a esta lista.)"""
    import asyncio

    from apps.web import app as app_mod

    async def _noop(app):
        return None

    for nombre in ("_startup_reconcile", "_refresh_loop", "_options_loop", "_bei_loop",
                   "_price_history_loop", "_ratings_loop"):
        monkeypatch.setattr(app_mod, nombre, _noop)
    monkeypatch.setattr(app_mod, "_seed_byma_universe", lambda: evento.set() or 0)
    return asyncio


def test_el_lifespan_siembra_el_universo_antes_de_cualquier_loop(monkeypatch):
    """Sin `MONITOR_DISABLE_LOOPS` el lifespan siembra (best-effort) ANTES de crear las
    tasks; con la variable puesta (pytest) no toca nada."""
    from fastapi.testclient import TestClient

    from apps.web import app as app_mod
    evento = threading.Event()
    _stub_loops_para_siembra(monkeypatch, evento)
    monkeypatch.delenv("MONITOR_DISABLE_LOOPS", raising=False)
    with TestClient(app_mod.app):
        assert evento.wait(5.0), "el lifespan no llamó a _seed_byma_universe"

    evento.clear()
    monkeypatch.setenv("MONITOR_DISABLE_LOOPS", "1")
    with TestClient(app_mod.app):
        assert not evento.wait(0.3)
```

(`test_el_lifespan_siembra...` usa `TestClient` sobre el catálogo compartido: por eso NO lleva la fixture `base`. El «antes de cualquier loop» estricto lo guarda la Task 6 con el loop real.)

Agregar al final de `tests/test_byma_universe.py` (asegurarse de que el archivo tenga `import pytest` al tope):

```python
def test_reingerir_con_estado_del_job_se_rechaza_salvo_force(tmp_db):
    """Desde el job diario `byma_catalog` tiene estado propio (`last_seen`, símbolos del
    feed, ficha): re-sembrar del CSV lo pisaría. Sólo con `force=True`."""
    from core.infrastructure.db.catalog_repository import init_db
    from core.infrastructure.db.engine import SessionLocal
    from core.infrastructure.db.models import BymaCatalogORM
    csvp = tmp_db / "t.csv"
    _write(csvp, [["AL30", "AL30", "ARS", "GO", "0", "primary", "", "True", "Titulos Publicos",
                   "ARARGE3209S6", "", "BOND", "REP. ARGENTINA"]])
    assert universe.ingest_byma_catalog(csvp) == 1
    init_db()
    with SessionLocal.begin() as s:
        s.add(BymaCatalogORM(symbol="S29E7", categoria="Títulos Públicos", last_seen="2026-09-07"))
    with pytest.raises(RuntimeError, match="last_seen"):
        universe.ingest_byma_catalog(csvp)
    assert universe.count() == 2                       # no tocó nada
    assert universe.ingest_byma_catalog(csvp, force=True) == 1
    assert universe.count() == 1
```

- [ ] **Step 2: Correr y ver que falla**

Run: `py -3.12 -m pytest tests/test_universe_service.py tests/test_byma_universe.py -q --tb=short -k "siembra or seed or reingerir or reconcile"`
Expected: FAIL (`AttributeError: ... '_seed_byma_universe'`; `TypeError: ... unexpected keyword argument 'force'`).

- [ ] **Step 3: `ingest_byma_catalog` con guard y docstrings**

En `core/infrastructure/byma/universe.py`, en el docstring del módulo (líneas 1-6), reemplazar la frase «se puede borrar y reingerir sin pérdida» por: «es derivada SÓLO hasta que corre el job diario de novedades (`apps/web/universe_service.py`); después tiene estado propio (símbolos que sólo existen por el feed, `last_seen`, ficha) y un re-seed lo pierde — ver `ingest_byma_catalog(force=...)`».

Reemplazar la cabecera de `ingest_byma_catalog` (firma + docstring) por:

```python
def ingest_byma_catalog(csv_path: Optional[Path] = None, *, force: bool = False) -> int:
    """Carga `byma_catalog` desde el CSV (delete + insert). Devuelve cuántas filas
    quedaron; 0 si el CSV no existe.

    DESTRUCTIVA. Desde el job diario de novedades la tabla tiene estado propio (símbolos
    que sólo existen por el feed, `last_seen`, ficha): si hay filas con `last_seen` se
    rechaza salvo `force=True` (server parado y backup previo). El único caller
    automático es `apps.web.app._seed_byma_universe`, que sólo la llama con la tabla
    vacía."""
```

y, dentro de la función, reemplazar

```python
    init_db()
    with SessionLocal.begin() as s:
        s.execute(delete(BymaCatalogORM))
```
por

```python
    init_db()
    if not force:
        with SessionLocal() as s:
            con_estado = s.execute(
                select(func.count()).select_from(BymaCatalogORM)
                .where(BymaCatalogORM.last_seen.is_not(None))).scalar()
        if con_estado:
            raise RuntimeError(
                "byma_catalog tiene %d fila(s) con last_seen (el job de novedades ya "
                "corrió): re-sembrar del CSV las pisaría. Usá force=True con el server "
                "parado y backup previo." % con_estado)
    with SessionLocal.begin() as s:
        s.execute(delete(BymaCatalogORM))
```

(`select` y `func` ya están importados en `universe.py`; verificar y, si no, agregarlos al `from sqlalchemy import ...` existente.)

En `tests/test_byma_universe.py::test_ingest_and_search`, cambiar el comentario `# idempotente (delete + insert)` por `# idempotente sobre filas del CSV (sin last_seen); con estado del job se rechaza sin force`.

- [ ] **Step 4: `_seed_byma_universe`, lifespan y `_startup_reconcile`**

En `apps/web/app.py`, agregar ANTES de `def _startup_reconcile(` (después de `_backfill_legs`):

```python
def _seed_byma_universe() -> int:
    """Siembra `byma_catalog` desde el CSV **sólo si está vacía** (sync, en to_thread).

    Mismo modelo que las ON (`_ensure_obligaciones_negociables`): el CSV es semilla de
    bootstrap, no la verdad. La llama el lifespan ANTES de crear cualquier task —el job
    de novedades (`_universe_loop`) necesita la línea de base y, dentro de
    `_startup_reconcile`, la siembra era el 6º paso (minutos de fichas) corriendo en
    paralelo con el primer diff. Re-sembrar a propósito = `ingest_byma_catalog(force=True)`
    con el server parado y backup previo: pierde lo que agregó el job (símbolos nuevos,
    `last_seen`, ficha) hasta la corrida siguiente."""
    from core.infrastructure.byma.universe import count, ingest_byma_catalog
    if count() > 0:
        return 0
    return ingest_byma_catalog()
```

En `_startup_reconcile`: borrar la línea `from core.infrastructure.byma.universe import ingest_byma_catalog` del bloque de imports de la función; borrar las dos líneas

```python
        # Universo BYMA navegable (tabla byma_catalog) para el buscador del ABM.
        universe = await asyncio.to_thread(ingest_byma_catalog)
```

y reemplazar el `logger.info` final

```python
        logger.info("Startup: catálogo +%d filas, %d ISIN, %d especies BYMA, +%d patas.",
                    n, enriched, universe, legs)
```
por

```python
        logger.info("Startup: catálogo +%d filas, %d ISIN, +%d patas.", n, enriched, legs)
```

En el lifespan, inmediatamente después de `_on_crash = _crash_reporter(app)` (dentro del `if not os.environ.get("MONITOR_DISABLE_LOOPS"):`), insertar:

```python
        # Siembra del universo (CSV → byma_catalog, sólo si está vacía) ANTES de crear
        # cualquier task: el job de novedades necesita la línea de base. Best-effort: un
        # CSV ilegible no bloquea el arranque (el job rechaza sin línea de base y avisa).
        try:
            await asyncio.to_thread(_seed_byma_universe)
        except Exception:  # noqa: BLE001
            logger.warning("siembra de byma_catalog falló (no bloquea el arranque)",
                           exc_info=True)
```

- [ ] **Step 5: Stubear la siembra en los tres tests que bootean el lifespan real**

`tests/test_ratings_loop.py::_stub_loops`: agregar al final de la función `monkeypatch.setattr(app_mod, "_seed_byma_universe", lambda: 0)`.

`tests/test_aud_D1_seguridad_web.py` (donde stubea `_startup_reconcile`, ~línea 262) y `tests/test_rem_R3_web_state_severity.py` (~línea 209): agregar la misma línea con el alias del módulo que use cada archivo (`app_mod`). Motivo (dejarlo como comentario de una línea): «sin esto el boot ingiere el CSV real de 4.700 filas en el sandbox».

- [ ] **Step 6: Correr y ver que pasa**

Run: `py -3.12 -m pytest tests/test_universe_service.py tests/test_byma_universe.py tests/test_fin_Z2_cableado_catalog_health.py tests/test_ratings_loop.py tests/test_aud_D1_seguridad_web.py tests/test_rem_R3_web_state_severity.py -q --tb=short`
Expected: PASS.

- [ ] **Step 7: ruff + commit**

Run: `py -3.12 -m ruff check apps/web/app.py core/infrastructure/byma/universe.py tests/test_universe_service.py tests/test_byma_universe.py`

```bash
git add apps/web/app.py core/infrastructure/byma/universe.py tests/test_universe_service.py tests/test_byma_universe.py tests/test_ratings_loop.py tests/test_aud_D1_seguridad_web.py tests/test_rem_R3_web_state_severity.py
git commit -m "byma_catalog: siembra solo si esta vacia y en el lifespan antes de los loops; ingest_byma_catalog exige force con estado del job"
```

---

### Task 6: `AppState.novedades` + `_universe_loop` bajo el supervisor

**Files:**
- Modify: `apps/web/state.py:61-98` (`__init__`), `:250-285` (`status`), y setters junto a `set_bei`
- Modify: `apps/web/app.py` (`_universe_loop` + tupla del lifespan + comentario «cinco» + `/api/health`)
- Modify: `tests/test_ratings_loop.py::_stub_loops`, `tests/test_aud_D1_seguridad_web.py`, `tests/test_rem_R3_web_state_severity.py`, `tests/test_universe_service.py::_stub_loops_para_siembra` (sumar `_universe_loop`)
- Test: `tests/test_universe_loop.py`

**Interfaces:**
- Produces: `AppState.set_novedades(n: int)`, `AppState.novedades() -> int`, clave `"novedades"` en `AppState.status()` y en `/api/health`; `app._universe_loop(app)`; nombre de loop `"universe"` (task `loop:universe`); `app._UNIVERSE_REINTENTO_SEC = 3600`.

- [ ] **Step 1: Tests (rojos)**

Crear `tests/test_universe_loop.py`:

```python
"""`_universe_loop`: wiring bajo el supervisor, siembra antes que el loop, apagado bajo
pytest, publica el contador, reintenta tras rechazo y no muere por una corrida rota
(spec 2026-09-07 §2, §4 y §6)."""
from __future__ import annotations

import asyncio
import contextlib
import threading
import time
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient
from sqlalchemy import delete

from apps.web import app as app_mod
from apps.web import universe_service as svc
from apps.web.state import AppState
from config.settings import settings
from core.infrastructure.byma import novedades as nov
from core.infrastructure.db.catalog_repository import init_db
from core.infrastructure.db.engine import SessionLocal
from core.infrastructure.db.models import UniverseNovedadORM
from tests._clock import ref_date


async def _esperar(pred, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        await asyncio.sleep(0.01)
    return False


async def _cancelar(task) -> None:
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


def _stub_loops(monkeypatch, evento: threading.Event, nombres=None):
    """Todo el lifespan por no-ops; el loop bajo test avisa por `evento` y anota el
    nombre de su task (lo pone `supervise`)."""
    async def _noop(app):
        return None

    async def _spy(app):
        if nombres is not None:
            nombres.append(asyncio.current_task().get_name())
        evento.set()
        await asyncio.sleep(3600)

    for nombre in ("_startup_reconcile", "_refresh_loop", "_options_loop", "_bei_loop",
                   "_price_history_loop", "_ratings_loop"):
        monkeypatch.setattr(app_mod, nombre, _noop)
    monkeypatch.setattr(app_mod, "_universe_loop", _spy)
    monkeypatch.setattr(app_mod, "_seed_byma_universe", lambda: 0)   # no ingerir el CSV real


def test_lifespan_registra_el_universe_loop_bajo_el_supervisor(monkeypatch):
    evento, nombres = threading.Event(), []
    _stub_loops(monkeypatch, evento, nombres)
    monkeypatch.delenv("MONITOR_DISABLE_LOOPS", raising=False)
    with TestClient(app_mod.app):
        assert evento.wait(5.0), "el lifespan no arrancó _universe_loop"
    assert nombres == ["loop:universe"], "el loop no corre envuelto en supervise()"


def test_la_siembra_del_universo_corre_antes_que_el_loop(monkeypatch):
    """La línea de base del diff (byma_catalog) tiene que existir ANTES de que el job
    pueda correr; si la siembra vuelve a `_startup_reconcile`, el primer diff marca todo
    el feed como novedad y anula la siembra para siempre."""
    orden, evento = [], threading.Event()
    _stub_loops(monkeypatch, evento)
    monkeypatch.setattr(app_mod, "_seed_byma_universe", lambda: orden.append("seed") or 0)

    async def _spy(app):
        orden.append("loop")
        evento.set()
        await asyncio.sleep(3600)

    monkeypatch.setattr(app_mod, "_universe_loop", _spy)
    monkeypatch.delenv("MONITOR_DISABLE_LOOPS", raising=False)
    with TestClient(app_mod.app):
        assert evento.wait(5.0)
    assert orden[:2] == ["seed", "loop"], orden


def test_disable_loops_no_arranca_el_universe_loop(monkeypatch):
    evento = threading.Event()
    _stub_loops(monkeypatch, evento)
    monkeypatch.setenv("MONITOR_DISABLE_LOOPS", "1")
    with TestClient(app_mod.app):
        assert not evento.wait(0.3)


def _fake_app():
    return SimpleNamespace(state=SimpleNamespace(hub=object(), app_state=AppState()))


def _hoy_ar() -> str:
    """La MISMA expresión que usa el loop para «hoy» (no `date.today()`)."""
    return datetime.now(ZoneInfo(settings.timezone)).date().isoformat()


def _cablear(monkeypatch, *, pendientes_iniciales=0, resultado=None, error=None):
    """Dobles de todo lo que el loop toca: contador persistido, agenda, sello, corrida.
    El doble NO sella el día cuando devuelve rechazo (como el servicio real)."""
    llamadas = {"sync": 0}
    hecho = {"fecha": None}

    def _sync(hub, *, hoy):
        llamadas["sync"] += 1
        if error:
            raise error
        res = resultado or svc.Resultado(pendientes=3)
        if not res.rechazo:
            hecho["fecha"] = hoy.isoformat()
        return res

    monkeypatch.setattr(nov, "contar_nuevas", lambda: pendientes_iniciales)
    monkeypatch.setattr(nov, "proximo_despertar",
                        lambda now, *, hecha_hoy: 3600.0 if hecha_hoy else 0.0)
    monkeypatch.setattr(svc, "ultima_corrida", lambda: hecho["fecha"])
    monkeypatch.setattr(svc, "sincronizar_universo", _sync)
    return llamadas


def test_publica_el_contador_persistido_al_arrancar_sin_sincronizar(monkeypatch):
    """El badge no espera a las 08:00: lo que quedó pendiente ayer se ve al reiniciar. Y
    con el día ya sellado NO corre la sincronización."""
    llamadas = _cablear(monkeypatch, pendientes_iniciales=7)
    monkeypatch.setattr(svc, "ultima_corrida", _hoy_ar)      # ya corrió hoy
    app = _fake_app()

    async def run():
        task = asyncio.create_task(app_mod._universe_loop(app))
        try:
            assert await _esperar(lambda: app.state.app_state.novedades() == 7)
            await asyncio.sleep(0.05)
            assert llamadas["sync"] == 0, "sincronizó con el día ya sellado"
        finally:
            await _cancelar(task)

    asyncio.run(run())


def test_corre_una_vez_y_publica_las_pendientes(monkeypatch):
    llamadas = _cablear(monkeypatch, resultado=svc.Resultado(pendientes=2))
    app = _fake_app()

    async def run():
        task = asyncio.create_task(app_mod._universe_loop(app))
        try:
            assert await _esperar(lambda: llamadas["sync"] == 1)
            assert await _esperar(lambda: app.state.app_state.novedades() == 2)
            await asyncio.sleep(0.05)
            assert llamadas["sync"] == 1, "volvió a correr con el día ya sellado"
            assert not task.done()
        finally:
            await _cancelar(task)

    asyncio.run(run())


def test_una_corrida_rechazada_espera_el_tick_y_reintenta(monkeypatch):
    """Sin el `sleep` del `if res.rechazo`, `ultima_corrida()` sigue None,
    `proximo_despertar` devuelve 0 y el loop martilla SQLite y el hub sin parar."""
    llamadas = _cablear(monkeypatch,
                        resultado=svc.Resultado(rechazo="lectura anémica: 10 símbolos (< 50)",
                                                pendientes=1))
    monkeypatch.setattr(app_mod, "_UNIVERSE_REINTENTO_SEC", 0.3)
    app = _fake_app()

    async def run():
        task = asyncio.create_task(app_mod._universe_loop(app))
        try:
            assert await _esperar(lambda: llamadas["sync"] == 1)
            assert app.state.app_state.novedades() == 1        # publica lo pendiente igual
            await asyncio.sleep(0.1)
            assert llamadas["sync"] == 1, "reintentó sin esperar el tick (busy loop)"
            assert not task.done(), "el rechazo terminó el loop"
            assert await _esperar(lambda: llamadas["sync"] >= 2, timeout=2.0), "no reintentó"
        finally:
            await _cancelar(task)

    asyncio.run(run())


def test_una_corrida_rota_no_tumba_el_loop_y_espera_el_tick(monkeypatch):
    llamadas = _cablear(monkeypatch, error=RuntimeError("SQLite locked"))
    monkeypatch.setattr(app_mod, "_UNIVERSE_REINTENTO_SEC", 0.3)
    app = _fake_app()

    async def run():
        task = asyncio.create_task(app_mod._universe_loop(app))
        try:
            assert await _esperar(lambda: llamadas["sync"] == 1)
            await asyncio.sleep(0.1)
            assert llamadas["sync"] == 1, "reintentó sin esperar el tick tras la excepción"
            assert not task.done(), "la excepción de la corrida tumbó el loop"
        finally:
            await _cancelar(task)

    asyncio.run(run())


def test_status_y_health_llevan_el_contador_sin_tickers():
    """`/api/health` es público: va la CUENTA, jamás un símbolo (patrón de
    test_fin_Z2 / test_rem_R3: meter un dato distintivo y asertar que no sale)."""
    state = AppState()
    state.set_novedades(4)
    assert state.status()["novedades"] == 4
    with TestClient(app_mod.app) as c:
        init_db()
        try:
            with SessionLocal.begin() as s:
                nov.registrar_nuevas_en(s, [{"symbol": "TSNVHLTH", "source": "byma",
                                             "categoria": "Acciones"}], hoy=ref_date())
            app_mod.app.state.app_state.set_novedades(nov.contar_nuevas())
            r = c.get("/api/health")
            assert r.json()["novedades"] >= 1
            assert "TSNVHLTH" not in r.text
        finally:
            with SessionLocal.begin() as s:
                s.execute(delete(UniverseNovedadORM)
                          .where(UniverseNovedadORM.symbol == "TSNVHLTH"))
```

- [ ] **Step 2: Correr y ver que falla**

Run: `py -3.12 -m pytest tests/test_universe_loop.py -q --tb=short`
Expected: FAIL (`AttributeError: ... '_universe_loop'` / `set_novedades`).

- [ ] **Step 3: AppState**

En `apps/web/state.py`, en `__init__` después de `self._options_by_ticker: Dict[str, object] = {}`:

```python
        # Novedades del universo en estado `nueva` (spec 2026-09-07). Lo publica
        # `_universe_loop` (y el ABM al descartar/cargar); lo leen el badge y /api/health.
        self._novedades: int = 0
```

En `status()`, dentro del dict devuelto, después de la clave `"catalog"`:

```python
            # Contador de novedades del universo (especies nuevas sin decidir). Sólo la
            # CUENTA: el detalle vive en el ABM, detrás de login y permiso de pestaña.
            "novedades": self._novedades,
```

Después de `bei_tables`:

```python
    def set_novedades(self, n: int) -> None:
        self._novedades = int(n or 0)   # un escritor a la vez; asignación atómica

    def novedades(self) -> int:
        return self._novedades
```

En el comentario de `apps/web/state.py:24-25` que enumera los loops laterales («ratings/bei/options/price_history»), sumar `universe`.

- [ ] **Step 4: El loop y el wiring**

En `apps/web/app.py`, antes de `def _crash_reporter(`:

```python
# Tick de reintento del job de novedades del universo cuando la corrida del día se
# rechazó (hub sin rueda: server reiniciado antes de las 11:00, breaker abierto) o
# reventó. La corrida normal la agenda `novedades.proximo_despertar` (08:00 AR, 1×/día).
_UNIVERSE_REINTENTO_SEC = 3600


async def _universe_loop(app: FastAPI) -> None:
    """Novedades del universo (spec 2026-09-07): 1×/día a partir de las 08:00 AR compara lo
    que el hub vio en la rueda anterior contra el universo conocido y deja las especies
    nuevas en `universe_novedades` para el triage del ABM. NUNCA escribe `instruments`.

    Lee el snapshot ACUMULADO del hub y no pide la rueda de nuevo: a las 08:00 BYMA
    responde `data: []` (pre-market) y un fetch fresco rechazaría la corrida todos los
    días. Idempotente por día (sello en `schema_meta`), restart-safe; si la lectura se
    rechaza o revienta, reintenta cada hora. Red/SQLite van en `to_thread`."""
    from datetime import datetime as _dt
    from zoneinfo import ZoneInfo

    from apps.web.universe_service import sincronizar_universo, ultima_corrida
    from core.infrastructure.byma.novedades import contar_nuevas, proximo_despertar

    tz = ZoneInfo(settings.timezone)
    state = app.state.app_state
    try:
        # El badge no espera a las 08:00: publicar lo persistido apenas arranca.
        state.set_novedades(await asyncio.to_thread(contar_nuevas))
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 — sin contador inicial el loop igual sirve
        logger.exception("universe loop: no pude leer el contador inicial")
    while True:
        try:
            now = _dt.now(tz)
            hecha = (await asyncio.to_thread(ultima_corrida)) == now.date().isoformat()
            espera = proximo_despertar(now, hecha_hoy=hecha)
            if espera > 0:
                await asyncio.sleep(espera)
                continue
            res = await asyncio.to_thread(sincronizar_universo, app.state.hub, hoy=now.date())
            state.set_novedades(res.pendientes)
            logger.info(res.resumen())
            if res.rechazo:
                await asyncio.sleep(_UNIVERSE_REINTENTO_SEC)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — una corrida caída no puede tumbar el lifespan
            logger.exception("universe loop iteration failed")
            await asyncio.sleep(_UNIVERSE_REINTENTO_SEC)
```

En el lifespan, la tupla de loops queda:

```python
            for name, fn in (
                ("refresh", _refresh_loop),
                ("options", _options_loop),
                ("bei", _bei_loop),
                ("price_history", _price_history_loop),
                ("ratings", _ratings_loop),
                ("universe", _universe_loop),
            )
```

y el comentario de arriba pasa de `# Los otros cinco son `while True`` a `# Los otros seis son `while True``.

En `/api/health` (`def health`), después de la clave `"catalog"`:

```python
        # Novedades del universo pendientes de decidir (especies nuevas en los feeds).
        # Sólo la CUENTA: los símbolos viven en el ABM, detrás de login.
        "novedades": st["novedades"],
```

y en el comentario de esa función que enumera los laterales «(ratings/bei/price_history/options)», sumar `universe`.

- [ ] **Step 5: Sumar `_universe_loop` a las listas fijas de stubs y cerrar el guard del wiring**

`tests/test_ratings_loop.py::_stub_loops` (tupla de `_noop`): agregar `"_universe_loop"`.

`tests/test_universe_service.py::_stub_loops_para_siembra` (Task 5): agregar `"_universe_loop"` a la tupla.

`tests/test_rem_R3_web_state_severity.py` (~línea 209): agregar `"_universe_loop"` a la tupla que stubea con `_dormido`.

`tests/test_aud_D1_seguridad_web.py`: (a) agregar `"_universe_loop"` a la tupla de stubs (~264); (b) en `esperados` (~277-278) agregar `"loop:universe"`; (c) renombrar `test_lifespan_arranca_los_cinco_loops_bajo_supervisor` → `test_lifespan_arranca_los_seis_loops_bajo_supervisor` y en su docstring «los 5 loops» → «los 6 loops». (Sin (b) el guard del wiring no cubre al sexto loop.)

- [ ] **Step 6: Correr y ver que pasa**

Run: `py -3.12 -m pytest tests/test_universe_loop.py tests/test_universe_service.py tests/test_ratings_loop.py tests/test_aud_D1_seguridad_web.py tests/test_rem_R3_web_state_severity.py tests/test_health_badge.py tests/test_web_app.py tests/test_fin_Z2_cableado_catalog_health.py -q --tb=short`
Expected: PASS.

- [ ] **Step 7: ruff + commit**

Run: `py -3.12 -m ruff check apps/web/app.py apps/web/state.py tests/test_universe_loop.py tests/test_universe_service.py`

```bash
git add apps/web/app.py apps/web/state.py tests/test_universe_loop.py tests/test_universe_service.py tests/test_ratings_loop.py tests/test_aud_D1_seguridad_web.py tests/test_rem_R3_web_state_severity.py
git commit -m "Novedades del universo: _universe_loop bajo el supervisor (08:00 AR, idempotente por dia, reintento horario) y contador en AppState y /api/health"
```

---

### Task 7: Badge global en el header

**Files:**
- Modify: `apps/web/templates/fragments/header_status.html` (al final), `apps/web/static/css/app.css:56-60`
- Test: `tests/test_universe_loop.py` (agregar)

- [ ] **Step 1: Test (rojo)**

Agregar al final de `tests/test_universe_loop.py`:

```python
def test_el_badge_del_header_linkea_al_abm_solo_si_hay_novedades():
    with TestClient(app_mod.app) as c:
        app_mod.app.state.app_state.set_novedades(0)
        assert "novedad" not in c.get("/health/badge").text
        app_mod.app.state.app_state.set_novedades(2)
        html = c.get("/health/badge").text
    assert "2 novedades" in html and 'href="/abm"' in html and "meta-nov" in html
    # lo de siempre sigue ahí (bajo test nunca hubo refresh → 'datos viejos' o 'sin datos')
    assert ("datos viejos" in html) or ("sin datos" in html)
```

- [ ] **Step 2: Correr y ver que falla**

Run: `py -3.12 -m pytest "tests/test_universe_loop.py::test_el_badge_del_header_linkea_al_abm_solo_si_hay_novedades" -q --tb=short`
Expected: FAIL (`"2 novedades" in html` es False).

- [ ] **Step 3: Fragment + CSS**

Al final de `apps/web/templates/fragments/header_status.html` (después del `{%- endif -%}`):

```jinja
{#- Novedades del universo (spec 2026-09-07): ADITIVO al semáforo, nunca cambia sus
    textos. Sólo si hay pendientes y el usuario tiene la pestaña ABM. -#}
{%- if st.novedades and has_tab("abm") -%}
  <a class="meta meta-nov" href="/abm" title="Especies nuevas en los feeds que el universo no tenía — pendientes de decidir en el ABM">✦ {{ st.novedades }} novedad{{ 'es' if st.novedades != 1 else '' }}</a>
{%- endif -%}
```

En el comentario de cabecera de ese mismo fragment (línea 6, «(ratings/bei/price_history/options)»), sumar `universe`.

En `apps/web/static/css/app.css`, después de `header .live-err { ... }`:

```css
/* Novedades del universo: link al ABM, al lado del semáforo. */
header .meta-nov { color: var(--accent); font-weight: 600; text-decoration: none; margin-left: 8px; }
header .meta-nov:hover { text-decoration: underline; }
```

- [ ] **Step 4: Correr y ver que pasa**

Run: `py -3.12 -m pytest tests/test_universe_loop.py tests/test_health_badge.py tests/test_rem_R3_web_state_severity.py -q --tb=short`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/web/templates/fragments/header_status.html apps/web/static/css/app.css tests/test_universe_loop.py
git commit -m "Badge global de novedades del universo en el header (aditivo al semaforo)"
```

---

### Task 8: Prefill enriquecido — Letras → Tasa Fija con clase, vencimiento de la ficha

**Files:**
- Modify: `core/infrastructure/byma/universe.py:262-300` (`prefill_for`) + helper `_clase_letra`
- Test: `tests/test_byma_universe.py` (agregar)

**Interfaces:**
- Produces: `universe._clase_letra(symbol: str) -> Optional[str]` (`S`+dígito → "LECAP", `T`+dígito → "BONCAP", else None); `prefill_for` puede devolver `sheet="Tasa_Fija"` con `fields["clase"]` y `fields["fecha_pago"]`, o `fields["fecha_vencimiento"]` en las demás hojas.

- [ ] **Step 1: Tests (rojos)**

Agregar al final de `tests/test_byma_universe.py`:

```python
# ── prefill enriquecido (novedades, spec 2026-09-07 §3) ─────────────────────
def _uni_row(symbol, **kw):
    from core.infrastructure.db.models import BymaCatalogORM
    base = dict(symbol=symbol, ticker_pesos=symbol, moneda="ARS", clase_liquidacion="primary",
                cotiza=1, categoria="Títulos Públicos", panel="Letras")
    base.update(kw)
    return BymaCatalogORM(**base)


def test_una_letra_del_panel_letras_prefillea_tasa_fija_con_clase_y_vencimiento(tmp_db):
    from core.domain.instrument_groups import is_known_type
    from core.infrastructure.db.catalog_repository import init_db
    from core.infrastructure.db.engine import SessionLocal
    init_db()
    with SessionLocal.begin() as s:
        s.add(_uni_row("S29E7", vencimiento="2027-01-29"))
        s.add(_uni_row("T15E7", vencimiento="2027-01-15"))
    pf = universe.prefill_for("S29E7")
    assert pf["sheet"] == "Tasa_Fija"
    assert pf["fields"]["clase"] == "LECAP" and is_known_type("LECAP")
    assert pf["fields"]["fecha_pago"] == "2027-01-29"
    assert pf["fields"]["ticker_ars"] == "S29E7"
    assert universe.prefill_for("T15E7")["fields"]["clase"] == "BONCAP"


def test_un_bonte_o_dual_del_panel_letras_no_recibe_una_clase_inventada(tmp_db):
    from core.infrastructure.db.catalog_repository import init_db
    from core.infrastructure.db.engine import SessionLocal
    init_db()
    with SessionLocal.begin() as s:
        s.add(_uni_row("TO26"))
        s.add(_uni_row("TTM26"))
    for sym in ("TO26", "TTM26"):
        pf = universe.prefill_for(sym)
        assert pf["sheet"] == "Soberanos" and "clase" not in pf["fields"], sym
    assert universe._clase_letra("TY30P") is None
    assert universe._clase_letra("S31G6") == "LECAP" and universe._clase_letra("T30J7") == "BONCAP"


def test_el_vencimiento_de_la_ficha_prefillea_fecha_vencimiento_en_una_on(tmp_db):
    from core.infrastructure.db.catalog_repository import init_db
    from core.infrastructure.db.engine import SessionLocal
    init_db()
    with SessionLocal.begin() as s:
        s.add(_uni_row("YMCXO", categoria="Obligaciones Negociables", panel="Oblig. Negociables",
                       isin="ARYPFS000001", emisor="YPF S.A.", vencimiento="2029-06-30"))
    pf = universe.prefill_for("YMCXO")
    assert pf["sheet"] == "Obligaciones_Negociables"
    assert pf["fields"]["fecha_vencimiento"] == "2029-06-30"
    assert pf["fields"]["tipo"] == "HARD DOLLAR" and pf["fields"]["short_name"] == "YPF S.A."
```

- [ ] **Step 2: Correr y ver que falla**

Run: `py -3.12 -m pytest tests/test_byma_universe.py -q --tb=short -k "prefillea or inventada"`
Expected: FAIL (`pf["sheet"] == "Tasa_Fija"` es False; `_clase_letra` no existe).

- [ ] **Step 3: Implementar**

En `core/infrastructure/byma/universe.py`, agregar `import re` a los imports del módulo (junto a `csv`/`logging`) y, antes de `def prefill_for(`:

```python
# Clase de una LETRA por el prefijo del ticker. Sólo cuando es inequívoco: `S`+dígito es
# una LECAP y `T`+dígito una BONCAP (T15E7). `T`+letra son BONTE/duales (TO26, TY30P,
# TTM26): no se inventa nada, la hoja queda en Soberanos y el operador decide.
_LETRA_LECAP = re.compile(r"^S\d")
_LETRA_BONCAP = re.compile(r"^T\d")


def _clase_letra(symbol: str) -> Optional[str]:
    sym = (symbol or "").upper().strip()
    if _LETRA_LECAP.match(sym):
        return "LECAP"
    if _LETRA_BONCAP.match(sym):
        return "BONCAP"
    return None
```

Y reescribir `prefill_for` así (reemplaza la función entera):

```python
def prefill_for(key: str) -> Optional[dict]:
    """Para el ＋Alta del Universo / Novedades: dado el `key` de un grupo (ISIN /
    ticker_pesos / symbol), devuelve {sheet, fields} para abrir el form prefilleado. Toma
    las patas `primary` por moneda (ARS→ticker_ars, MEP→ticker_mep, cable→ticker_ccl),
    ISIN, emisor y ley (del prefijo ISIN). La hoja se deduce de la categoría (default ON);
    una letra del panel BYMA «Letras» va a Tasa Fija con la clase por prefijo, y el
    vencimiento de la ficha (si el job lo trajo) prefillea el campo de la hoja."""
    key = (key or "").strip().upper()
    if not key:
        return None
    init_db()
    with SessionLocal() as s:
        rows = s.execute(select(BymaCatalogORM).where(or_(
            func.upper(BymaCatalogORM.isin) == key,
            func.upper(BymaCatalogORM.ticker_pesos) == key,
            func.upper(BymaCatalogORM.symbol) == key,
        ))).scalars().all()
    if not rows:
        return None
    slot_field = {"pesos": "ticker_ars", "mep": "ticker_mep", "cable": "ticker_ccl"}
    fields: dict = {}
    isin = emisor = categoria = panel = vencimiento = None
    for o in rows:
        isin = isin or o.isin
        emisor = emisor or o.emisor
        categoria = categoria or o.categoria
        panel = panel or o.panel
        vencimiento = vencimiento or o.vencimiento
        if o.clase_liquidacion != "primary":
            continue
        slot = _MONEDA_SLOT.get(o.moneda)
        f = slot_field.get(slot or "")
        if f and not fields.get(f):
            fields[f] = (o.symbol or "").upper()
    sheet = _CAT_SHEET.get(categoria or "", "Obligaciones_Negociables")
    if panel == "Letras":
        clase = _clase_letra(fields.get("ticker_ars") or key)
        if clase:
            sheet = "Tasa_Fija"
            fields["clase"] = clase
    fields["short_name"] = emisor or ""
    fields["isin"] = isin or ""
    if vencimiento:
        fields["fecha_pago" if sheet == "Tasa_Fija" else "fecha_vencimiento"] = vencimiento
    if sheet == "Obligaciones_Negociables":
        fields["tipo"] = "HARD DOLLAR"
        ley = _legislacion(isin)
        if ley:
            fields["ley_aplicable"] = "Argentina" if ley == "Ley Local" else "Extranjera"
    return {"sheet": sheet, "fields": fields}
```

- [ ] **Step 4: Correr y ver que pasa**

Run: `py -3.12 -m pytest tests/test_byma_universe.py tests/test_abm_router.py -q --tb=short`
Expected: PASS.

- [ ] **Step 5: ruff + commit**

```bash
git add core/infrastructure/byma/universe.py tests/test_byma_universe.py
git commit -m "Prefill del ABM: una letra del panel Letras abre Tasa Fija con la clase por prefijo y el vencimiento de la ficha"
```

---

### Task 9: Rutas de Novedades en el router ABM + fragment + hook en `/abm/save`

**Files:**
- Modify: `apps/web/routers/abm.py` (imports, funciones nuevas, `abm_save`, `abm_page`)
- Create: `apps/web/templates/fragments/abm_novedades.html`
- Test: `tests/test_abm_novedades.py`

**Interfaces:**
- Produces: `GET /abm/novedades` (fragment), `POST /abm/novedades/{symbol}/descartar`, `POST /abm/novedades/{symbol}/restaurar`, `POST /abm/novedades/refresh` (admin); `/abm/save` devuelve header `HX-Trigger: novedades-refresh` y marca `cargada`; ctx `novedades` en `GET /abm`.
- Consumes: `novedades.agrupadas/listar/descartar/restaurar/contar_nuevas/marcar_cargadas/leer_meta`, `universe_service.sincronizar_universo/META_ULTIMA_CORRIDA`, `deps_auth.get_admin_user_html`, `settings.timezone`.

- [ ] **Step 1: Tests (rojos)**

Crear `tests/test_abm_novedades.py`:

```python
"""Pestaña Novedades del ABM: fragment agrupado, Cargar prefillado, Descartar/Restaurar,
Refrescar (admin) y la transición nueva→cargada al guardar (spec 2026-09-07 §3)."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete

from apps.web import app as app_mod
from apps.web import universe_service as svc
from apps.web.app import app
from core.infrastructure.byma import novedades as nov
from core.infrastructure.db.catalog_repository import init_db
from core.infrastructure.db.engine import SessionLocal
from core.infrastructure.db.models import BymaCatalogORM, UniverseNovedadORM
from tests._clock import ref_date
from tests.test_abm_router import _con_preview

HOY = ref_date()
_SYMS = ("TSNV1O", "TSNVGG")


def _sembrar():
    """Dos novedades en el catálogo COMPARTIDO del sandbox: una ON (cargable) y una
    acción (sin hoja). Se limpian en `_limpiar`."""
    init_db()
    with SessionLocal.begin() as s:
        s.merge(BymaCatalogORM(symbol="TSNV1O", ticker_pesos="TSNV1O", moneda="ARS",
                               clase_liquidacion="primary", cotiza=1,
                               categoria="Obligaciones Negociables", emisor="NOVEDAD S.A.",
                               isin="ARNOVE000001", vencimiento="2028-01-31"))
        s.merge(BymaCatalogORM(symbol="TSNVGG", ticker_pesos="TSNVGG", moneda="ARS",
                               clase_liquidacion="primary", cotiza=1, categoria="Acciones"))
        nov.registrar_nuevas_en(s, [
            {"symbol": "TSNV1O", "source": "byma", "categoria": "Obligaciones Negociables"},
            {"symbol": "TSNVGG", "source": "data912", "categoria": "Acciones"},
        ], hoy=HOY)


def _limpiar():
    with SessionLocal.begin() as s:
        s.execute(delete(UniverseNovedadORM).where(UniverseNovedadORM.symbol.in_(_SYMS)))
        s.execute(delete(BymaCatalogORM).where(BymaCatalogORM.symbol.in_(_SYMS)))


@pytest.fixture
def novedades():
    _sembrar()
    try:
        yield
    finally:
        _limpiar()


def test_el_fragment_agrupa_por_categoria_y_solo_ofrece_cargar_donde_hay_hoja(novedades):
    with TestClient(app) as c:
        r = c.get("/abm/novedades")
    assert r.status_code == 200
    html = r.text
    assert "Obligaciones Negociables" in html and "Acciones" in html
    assert "TSNV1O" in html and "TSNVGG" in html and "NOVEDAD S.A." in html
    assert 'hx-get="/abm/form?prefill=TSNV1O"' in html          # ＋ Cargar prefillado
    assert 'hx-get="/abm/form?prefill=TSNVGG"' not in html      # acción: sin hoja → sin ＋
    assert 'hx-post="/abm/novedades/TSNVGG/descartar"' in html  # pero sí Descartar
    assert "2028-01-31" in html                                  # vencimiento de la ficha


def test_descartar_y_restaurar_actualizan_estado_y_contador(novedades):
    with TestClient(app) as c:
        st = app_mod.app.state.app_state
        r = c.post("/abm/novedades/TSNVGG/descartar")
        assert r.status_code == 200 and "restaurar" in r.text.lower()
        assert "TSNVGG" in [f["symbol"] for f in nov.listar("descartada")]
        assert st.novedades() == nov.contar_nuevas()
        r = c.post("/abm/novedades/TSNVGG/restaurar")
        assert r.status_code == 200
        assert "TSNVGG" not in [f["symbol"] for f in nov.listar("descartada")]


def test_la_pagina_del_abm_trae_la_pestana_novedades_primera(novedades):
    with TestClient(app) as c:
        page = c.get("/abm").text
    assert 'id="view-novedades"' in page and 'data-v="novedades"' in page
    assert page.index('data-v="novedades"') < page.index('data-v="cargados"')
    assert 'hx-get="/abm/novedades"' in page
    assert 'class="abm-seg"' in page and "Universo BYMA" in page and 'id="abm-list"' in page


def test_un_alta_desde_el_abm_marca_la_novedad_como_cargada(novedades):
    fields = {
        "sheet": "Obligaciones_Negociables",
        "ticker_ars": "TSNV1O", "ticker_mep": "", "ticker_ccl": "",
        "short_name": "NOVEDAD S.A.", "tipo": "HARD DOLLAR", "ley_aplicable": "Argentina",
        "fecha_emision": "2026-01-31", "fecha_vencimiento": "2028-01-31",
        "cupon anual %": "8", "frecuencia pagos": "2",
        "base calculo": "ACT/365", "tipo amortizacion": "bullet",
    }
    with TestClient(app) as c:
        try:
            r = c.post("/abm/save", data=_con_preview(fields))
            assert r.status_code == 200 and "No se guardó" not in r.text
            assert r.headers.get("HX-Trigger") == "novedades-refresh"
            with SessionLocal() as s:
                assert s.get(UniverseNovedadORM, "TSNV1O").estado == "cargada"
            assert app_mod.app.state.app_state.novedades() == nov.contar_nuevas()
        finally:
            c.delete("/abm/instrument/TSNV1O")


def test_refrescar_ahora_corre_la_sincronizacion_y_muestra_el_resumen(novedades, monkeypatch):
    llamadas = []

    def _sync(hub, *, hoy):
        llamadas.append(hoy)
        return svc.Resultado(vistos=1200, pendientes=2)

    monkeypatch.setattr(svc, "sincronizar_universo", _sync)
    with TestClient(app) as c:
        r = c.post("/abm/novedades/refresh")
    assert r.status_code == 200 and len(llamadas) == 1
    assert "1200 vistos" in r.text


# ── el refresh exige admin (auth real) ──────────────────────────────────────
@pytest.mark.noauth
def test_refrescar_ahora_exige_admin():
    from apps.web.routers import auth as auth_router
    from core.infrastructure.db.engine import get_engine
    from core.infrastructure.db.models import Base, UserORM
    from core.security import get_password_hash

    Base.metadata.create_all(bind=get_engine())
    auth_router._login_attempts.clear()
    with SessionLocal() as s:
        s.query(UserORM).delete()
        s.add(UserORM(username="admin", hashed_password=get_password_hash("adminpass"),
                      is_admin=True, allowed_tabs=["*"]))
        # bob TIENE la pestaña abm: así el 403 viene del check de admin, no del de pestaña
        s.add(UserORM(username="bob", hashed_password=get_password_hash("bobpass"),
                      is_admin=False, allowed_tabs=["abm"]))
        s.commit()
    try:
        with TestClient(app) as c:
            c.post("/login", data={"username": "bob", "password": "bobpass"})
            assert c.get("/abm/novedades", follow_redirects=False).status_code == 200
            r = c.post("/abm/novedades/refresh", follow_redirects=False)
            assert r.status_code == 403
        with TestClient(app) as c:
            r = c.post("/abm/novedades/refresh",
                       headers={"Origin": "http://evil.example"}, follow_redirects=False)
            assert r.status_code == 403                      # CSRF antes que todo
    finally:
        with SessionLocal() as s:
            s.query(UserORM).delete()
            s.commit()
        auth_router._login_attempts.clear()
```

- [ ] **Step 2: Correr y ver que falla**

Run: `py -3.12 -m pytest tests/test_abm_novedades.py -q --tb=short`
Expected: FAIL (404 en `/abm/novedades`, etc.).

- [ ] **Step 3: Router**

En `apps/web/routers/abm.py`, imports: después de `from apps.web.bond_detail import calculate` agregar

```python
from apps.web.deps_auth import get_admin_user_html
```

después de `from apps.web.templates import TEMPLATES as _TEMPLATES` agregar

```python
from apps.web.universe_service import META_ULTIMA_CORRIDA
from config.settings import settings
from core.infrastructure.byma import novedades as nov_store
```

Después de `abm_universe` (antes de `_live_metrics`), agregar:

```python
# ── Novedades del universo (spec 2026-09-07 §3) ───────────────────────────────
def _attach_px_novedades(grupos, hub) -> None:
    """Precio y volumen del día por símbolo desde el snapshot vivo (best-effort)."""
    try:
        snap = hub.snapshot() if hub else {}
    except Exception:  # noqa: BLE001 — el hub puede no estar listo
        snap = {}
    for g in grupos:
        for f in g["filas"]:
            row = snap.get(f["symbol"])
            c = getattr(row, "c", None) if row is not None else None
            f["px"] = float(c) if c else None
            f["vol_f"] = _fmt_monto(getattr(row, "v", None)) if row is not None else "—"


def _render_novedades(request: Request, hub, flash: str = "") -> HTMLResponse:
    grupos = nov_store.agrupadas("nueva")
    _attach_px_novedades(grupos, hub)
    return _TEMPLATES.TemplateResponse(request, "fragments/abm_novedades.html", {
        "grupos": grupos,
        "descartadas": nov_store.listar("descartada"),
        "total": sum(len(g["filas"]) for g in grupos),
        "flash": flash,
        "ultima": nov_store.leer_meta(META_ULTIMA_CORRIDA),
    })


@router.get("/abm/novedades", response_class=HTMLResponse)
def abm_novedades(request: Request, hub=Depends(get_hub)):
    """Especies nuevas en los feeds, agrupadas por tipo de activo, con Cargar/Descartar."""
    return _render_novedades(request, hub)


@router.post("/abm/novedades/{symbol}/descartar", response_class=HTMLResponse)
def abm_novedad_descartar(symbol: str, request: Request, hub=Depends(get_hub),
                          state=Depends(get_state)):
    nov_store.descartar(symbol)
    state.set_novedades(nov_store.contar_nuevas())
    return _render_novedades(request, hub)


@router.post("/abm/novedades/{symbol}/restaurar", response_class=HTMLResponse)
def abm_novedad_restaurar(symbol: str, request: Request, hub=Depends(get_hub),
                          state=Depends(get_state)):
    nov_store.restaurar(symbol)
    state.set_novedades(nov_store.contar_nuevas())
    return _render_novedades(request, hub)


@router.post("/abm/novedades/refresh", response_class=HTMLResponse)
async def abm_novedades_refresh(request: Request, hub=Depends(get_hub),
                                state=Depends(get_state),
                                _admin=Depends(get_admin_user_html)):
    """«Refrescar ahora»: la misma corrida que el loop de las 08:00, a demanda (admin)."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from apps.web.universe_service import sincronizar_universo

    hoy = datetime.now(ZoneInfo(settings.timezone)).date()
    try:
        res = await asyncio.to_thread(sincronizar_universo, hub, hoy=hoy)
        state.set_novedades(res.pendientes)
        flash = res.resumen()
    except Exception as e:  # noqa: BLE001 — el operador tiene que ver el motivo
        logger.exception("refresh manual de novedades falló")
        flash = "la corrida falló: %s: %s" % (type(e).__name__, e)
    return _render_novedades(request, hub, flash=flash)
```

En `abm_save`, reemplazar el cuerpo del `try` y el `return` final:

```python
    try:
        # to_thread: SQLite + repo.reload() (relee ~550 instrumentos con sus cashflows)
        # son ~130-200 ms de I/O+CPU sincrónico. En la corrutina frenaban el event loop
        # y con él todos los SSE. CLAUDE.md ya decía que esto corría en to_thread.
        def _save():
            res = abm_store.save_instrument(sheet, fields, cashflows)
            repo.reload()                              # refresca el cache desde SQLite
            # Si el ticker (o una pata) era una novedad pendiente, pasa a `cargada`.
            return nov_store.marcar_cargadas(res["tickers"])
        cargadas = await asyncio.to_thread(_save)
    except (ValueError, KeyError) as e:
        # NUNCA tragar el error: el operador tiene que saber que NO se guardó
        # (antes esto era `pass` y el alta "desaparecía" sin aviso).
        logger.warning("ABM save falló (%s): %s", sheet, e)
        return _render_list(request, sheet, state, error=str(e))
    if cargadas:
        state.set_novedades(await asyncio.to_thread(nov_store.contar_nuevas))
    resp = _render_list(request, sheet, state)
    # La pestaña Novedades escucha este evento y se refresca sola (la respuesta va a
    # #abm-list, que está oculto cuando el alta arranca desde Novedades).
    resp.headers["HX-Trigger"] = "novedades-refresh"
    return resp
```

En `abm_page`, agregar al dict del contexto: `"novedades": state.novedades(),`.

- [ ] **Step 4: Fragment**

Crear `apps/web/templates/fragments/abm_novedades.html`:

```jinja
{# Novedades del universo (spec 2026-09-07): especies vistas en los feeds que el universo
   no tenía, agrupadas por tipo de activo. «＋» abre el cajón prefillado (mismo camino que
   el ＋ del Universo); «✕» descarta (reversible, grupo al pie). Sólo hay ＋ donde el ABM
   tiene hoja (ON, títulos públicos): acciones/cedears/índices no se cargan por acá. #}
<div class="nov-meta">
  {% if total %}<b>{{ total }}</b> pendiente{{ 's' if total != 1 else '' }}{% else %}Sin novedades pendientes{% endif %}
  {% if ultima %} · última corrida {{ ultima }}{% endif %}
  {% if flash %}<span class="nov-flash">{{ flash }}</span>{% endif %}
</div>
{% for g in grupos %}
<details class="nov-grupo" open>
  <summary>{{ g.categoria }} <span class="badge">{{ g.filas|length }}</span></summary>
  <table class="nov-table">
    <thead><tr><th>Símbolo</th><th>Denominación</th><th>Emisor</th><th>Mon.</th><th>Vto.</th><th class="num">Precio</th><th class="num">Vol. día</th><th>Fuente</th><th>Visto</th><th></th></tr></thead>
    <tbody>
    {% for f in g.filas %}
    <tr>
      <td class="nov-sym">{{ f.symbol }}</td>
      <td>{{ f.denominacion or '—' }}</td>
      <td class="nov-emisor">{{ f.emisor or '—' }}</td>
      <td>{{ f.moneda or '—' }}</td>
      <td>{{ f.vencimiento or '—' }}</td>
      <td class="num">{{ '%.2f'|format(f.px) if f.px else '—' }}</td>
      <td class="num">{{ f.vol_f }}</td>
      <td>{{ f.source }}</td>
      <td>{{ f.first_seen }}</td>
      <td class="nov-acts">
        {% if f.cargable %}<button type="button" class="uni-plus" title="dar de alta (cajón prefillado)"
                hx-get="/abm/form?prefill={{ f.symbol|urlencode }}" hx-target="#abm-editor" hx-swap="innerHTML"
                onclick="abmOpenDrawer()">＋</button>{% else %}<span class="nov-nohoja" title="sin hoja en el ABM">—</span>{% endif %}
        <button type="button" class="nov-x" title="descartar (reversible)"
                hx-post="/abm/novedades/{{ f.symbol|urlencode }}/descartar" hx-target="#nov-results" hx-swap="innerHTML">✕</button>
      </td>
    </tr>
    {% endfor %}
    </tbody>
  </table>
</details>
{% endfor %}
{% if descartadas %}
<details class="nov-grupo nov-descartadas">
  <summary>Descartadas <span class="badge">{{ descartadas|length }}</span></summary>
  <table class="nov-table"><tbody>
  {% for f in descartadas %}
  <tr>
    <td class="nov-sym">{{ f.symbol }}</td><td>{{ f.categoria }}</td><td class="nov-emisor">{{ f.emisor or '—' }}</td><td>{{ f.first_seen }}</td>
    <td class="nov-acts"><button type="button" class="nov-undo" title="volver a pendientes"
        hx-post="/abm/novedades/{{ f.symbol|urlencode }}/restaurar" hx-target="#nov-results" hx-swap="innerHTML">restaurar</button></td>
  </tr>
  {% endfor %}
  </tbody></table>
</details>
{% endif %}
```

- [ ] **Step 5: Correr y ver que pasan las rutas (la página del ABM todavía falla)**

Run: `py -3.12 -m pytest tests/test_abm_novedades.py -q --tb=short`
Expected: PASS todos salvo `test_la_pagina_del_abm_trae_la_pestana_novedades_primera` (va en la Task 10).

- [ ] **Step 6: ruff + commit**

Run: `py -3.12 -m ruff check apps/web/routers/abm.py tests/test_abm_novedades.py`

```bash
git add apps/web/routers/abm.py apps/web/templates/fragments/abm_novedades.html tests/test_abm_novedades.py
git commit -m "ABM: rutas de novedades (fragment por categoria, descartar/restaurar, refresh admin) y nueva→cargada al guardar"
```

---

### Task 10: Pestaña «Novedades» primera en `pages/abm.html`

**Files:**
- Modify: `apps/web/templates/pages/abm.html:3-11`, `:170-186` (CSS), `:189-194` (`abmSeg`)
- Test: `tests/test_abm_novedades.py::test_la_pagina_del_abm_trae_la_pestana_novedades_primera` (ya escrito)

- [ ] **Step 1: Correr y ver que falla**

Run: `py -3.12 -m pytest "tests/test_abm_novedades.py::test_la_pagina_del_abm_trae_la_pestana_novedades_primera" -q --tb=short`
Expected: FAIL (`'id="view-novedades"' in page` es False).

- [ ] **Step 2: Template**

Reemplazar el bloque de pestañas (líneas 4-8) por:

```html
  <!-- segmented: Novedades (a decidir) / Cargados (completitud) / Universo BYMA (sin cargar) -->
  <div class="abm-seg" role="tablist">
    <button type="button" data-v="novedades" class="on" onclick="abmSeg('novedades')">Novedades <span class="badge" id="nov-badge">{{ novedades }}</span></button>
    <button type="button" data-v="cargados" onclick="abmSeg('cargados')">Cargados <span class="badge">{{ loaded_total }}</span></button>
    <button type="button" data-v="universo" onclick="abmSeg('universo')">Universo BYMA <span class="badge">{% if unloaded is not none %}{{ unloaded }} sin cargar{% else %}{{ byma_count }}{% endif %}</span></button>
  </div>

  <!-- ===== NOVEDADES: especies nuevas en los feeds, por tipo de activo ===== -->
  <section class="abm-view" id="view-novedades">
    <div class="uni-tools">
      <strong style="font-size:11px;color:var(--text-faint);letter-spacing:.4px">NOVEDADES DEL UNIVERSO</strong>
      <span style="flex:1 1 auto"></span>
      {% if current_user and current_user.is_admin %}
      <button type="button" id="nov-refresh" class="uni-refreshbtn" title="Correr ahora la detección (la misma que corre a las 08:00)"
              hx-post="/abm/novedades/refresh" hx-target="#nov-results" hx-swap="innerHTML" hx-disabled-elt="this">↻ Refrescar ahora</button>
      {% endif %}
    </div>
    {# `novedades-refresh` lo dispara /abm/save (header HX-Trigger): un alta desde el
       cajón saca la fila de acá sin recargar la página. #}
    <div id="nov-results" class="uni-results" hx-get="/abm/novedades" hx-trigger="load, novedades-refresh from:body" hx-swap="innerHTML">
      <div class="uni-empty">Cargando novedades…</div>
    </div>
  </section>
```

En la sección Cargados, cambiar `<section class="abm-view" id="view-cargados">` por `<section class="abm-view" id="view-cargados" hidden>`.

En el `<style>` del template, después de `.uni-plus:hover { filter:brightness(1.1); }`:

```css
  /* ---- Novedades ---- */
  .nov-meta { padding:8px 10px; font-size:12px; color:var(--text-dim); }
  .nov-flash { margin-left:10px; color:var(--accent); }
  .nov-grupo { border-top:1px solid var(--panel-border); }
  .nov-grupo > summary { cursor:pointer; padding:7px 10px; font-weight:600; font-size:12px; }
  .nov-table { width:100%; border-collapse:collapse; font-size:12px; }
  .nov-table th, .nov-table td { padding:4px 8px; text-align:left; white-space:nowrap; border-bottom:1px solid var(--panel-border); }
  .nov-table .num { text-align:right; font-variant-numeric:tabular-nums; }
  .nov-sym { font-weight:600; } .nov-emisor { max-width:280px; overflow:hidden; text-overflow:ellipsis; }
  .nov-acts { display:flex; gap:6px; align-items:center; }
  .nov-x { background:transparent; border:1px solid var(--panel-border); border-radius:5px; width:20px; height:20px; cursor:pointer; line-height:1; padding:0; color:var(--text-dim); }
  .nov-x:hover { color:var(--neg); border-color:var(--neg); }
  .nov-undo { background:transparent; border:1px solid var(--panel-border); border-radius:5px; padding:1px 8px; cursor:pointer; font-size:11px; color:var(--text-dim); }
  .nov-nohoja { display:inline-block; width:20px; text-align:center; color:var(--text-faint); }
  .nov-descartadas > summary { color:var(--text-faint); font-weight:500; }
```

Reemplazar `abmSeg`:

```js
  function abmSeg(v){
    document.querySelectorAll('.abm-seg button').forEach(function(b){ b.classList.toggle('on', b.dataset.v===v); });
    ['novedades','cargados','universo'].forEach(function(id){ document.getElementById('view-'+id).hidden = (v!==id); });
    if(v==='universo'){ var q=document.getElementById('uni-q'); if(q) q.focus(); }
  }
```

- [ ] **Step 3: Correr y ver que pasa (los tests viejos del ABM también)**

Run: `py -3.12 -m pytest tests/test_abm_novedades.py tests/test_abm_router.py tests/test_byma_universe.py -q --tb=short`
Expected: PASS.

- [ ] **Step 4: Smoke visual**

Invocar el skill `/smoke` con la ruta `/abm` (levanta la app en :8001, verifica `/api/health`, `/login` y `/abm`); opcionalmente `/verificar-ui` para ver la pestaña con Playwright. Verificar a ojo: la pestaña Novedades es la primera y está activa, Cargados y Universo cambian con el segmented, el botón «↻ Refrescar ahora» aparece (admin) y responde con el resumen.

- [ ] **Step 5: Commit**

```bash
git add apps/web/templates/pages/abm.html
git commit -m "ABM: pestaña Novedades primera, con grupos por tipo de activo y Refrescar ahora"
```

---

### Task 11: Docs, invariante de CLAUDE.md, comentarios «cinco loops», gate y security-review

**Files:**
- Modify: `CLAUDE.md` (bullets «Alta automática de letras», «SQLite = fuente de verdad» y uno nuevo para `ingest_byma_catalog`), `docs/flujo-web.md:8-13`, `docs/arquitectura.md:66,84-85,103` (+ sección de datos), `apps/web/supervisor.py:90` (SOLO esa: `:8-9` narra el incidente del 2026-09-01, cuando había cinco), `tests/test_aud_D2_web_supervisor.py:5` («los 5 loops reales» → 6; describe el presente)
- NO tocar (relatos históricos): `tests/test_aud_D2_web_state.py:3`, `docs/arquitectura.md:90`, `apps/web/supervisor.py:8-9`.
- Memoria: `C:\Users\david\.claude\memory-monitores\project_faltantes_eldashboard_2026_09_07.md` (marcar la directiva como implementada)

- [ ] **Step 1: CLAUDE.md**

En el bullet `**Alta automática de letras = la ÚNICA escritura automática en el catálogo**`, cambiar el inicio por:

```
- **Alta automática de letras = la ÚNICA escritura automática en `instruments`**
  (el job diario de novedades del universo —`_universe_loop`, 08:00 AR— escribe SOLO
  `byma_catalog` y `universe_novedades`, nunca `instruments`; spec
  `docs/superpowers/specs/2026-09-07-novedades-universo-design.md`)
```

y dejar el resto del bullet como está. En el bullet de SQLite/semillas, agregar al final:

```
  `byma_catalog` sigue el mismo modelo: el CSV `data/byma/titulos_final.csv` se siembra
  SOLO si la tabla está vacía (`app._seed_byma_universe`, en el lifespan antes de los
  loops); después la mantiene el job de novedades.
```

Agregar un bullet nuevo debajo del de `on_catalog.ingest()`:

```
- **`universe.ingest_byma_catalog()` es DESTRUCTIVA** (DELETE+INSERT del CSV) y desde el
  job de novedades la tabla tiene estado propio (`last_seen`, símbolos del feed, ficha):
  se rechaza con filas `last_seen` salvo `force=True` (server parado, backup previo).
```

- [ ] **Step 2: Docs y comentarios**

`docs/flujo-web.md`: título y párrafo «5 loops supervisados» → «6 loops supervisados», y agregar a la lista:

```
- `_universe_loop` — novedades del universo: 1×/día a partir de las 08:00 AR compara el
  snapshot acumulado del hub contra `byma_catalog` ∪ `instruments` ∪ patas ∪
  `universe_novedades`, registra las especies nuevas (estado `nueva`) y publica el contador
  (`AppState.novedades` → badge del header y `/api/health.novedades`). Nunca escribe
  `instruments`; guard de lectura rota (0 / <50 / <60 % de la corrida anterior / universo
  vacío / corrida en curso) → reintento horario. «Refrescar ahora» (POST admin en el ABM)
  corre la misma función. La siembra del CSV en `byma_catalog` corre en el lifespan ANTES
  de crear las tasks, sólo si la tabla está vacía.
```

`docs/arquitectura.md`: «5 loops» → «6 loops» en las líneas 66 y 84-85; en la 103 sumar `universe` a la lista de laterales; en la sección de datos agregar «`universe_novedades` (triage de especies nuevas; nunca se borra una fila) y columnas `last_seen`/`denominacion`/`vencimiento` en `byma_catalog`».

`apps/web/supervisor.py:90` («los 5 loops reales») → «los 6 loops reales». `tests/test_aud_D2_web_supervisor.py:5` («los 5 loops reales») → «los 6 loops reales». (Los comentarios de `header_status.html:6`, `app.py` health y `state.py:24-25` ya se tocaron en las Tasks 6-7.)

- [ ] **Step 3: Gate completo**

Run: `pwsh scripts/check.ps1` (timeout 300000 ms)
Expected: `=== GATE VERDE ===` (skips esperados en Windows: `time.tzset() es sólo Unix`, `requiere bash`).

- [ ] **Step 4: Security review**

Invocar `/security-review` (hay rutas POST nuevas y un POST admin). Atender hallazgos antes de commitear.

- [ ] **Step 5: Memoria y commit**

En `C:\Users\david\.claude\memory-monitores\project_faltantes_eldashboard_2026_09_07.md`, reemplazar el párrafo «Directiva de David …» por una línea: «Implementado el sistema de novedades del universo (plan `docs/superpowers/plans/2026-09-07-novedades-universo.md`); queda pendiente la migración Excel→DB (fase aparte).»

```bash
git add CLAUDE.md docs/flujo-web.md docs/arquitectura.md apps/web/supervisor.py tests/test_aud_D2_web_supervisor.py
git commit -m "Docs: sexto loop (novedades del universo), invariante de escritura automatica acotado a instruments, ingest_byma_catalog destructiva"
```

- [ ] **Step 6: Cierre**

Invocar `/compound` (checklist de cierre de fase: lección materializada, memoria, doc, alcance). Después, `superpowers:finishing-a-development-branch`.
