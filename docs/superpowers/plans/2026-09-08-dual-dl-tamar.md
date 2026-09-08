# DUAL dólar-linked / TAMAR (`DUAL_DL_TAMAR`) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Que TMVE8 (bono dual TAMAR / dólar-linked, vto 2028-01-31) se pueda cargar desde la pestaña Novedades del ABM y se precie: TEA nominal en pesos en el panel TAMAR/Dual, y en el popup las patas `_TAM` (riel TAMAR) y `_DL` (TIR en USD del riel dólar-linked).

**Architecture:** Tipo nuevo `DUAL_DL_TAMAR` en un grupo propio `DUAL_DL`; `Instrument.fx_base` desde `raw_fields["tc_inicial"]`; `DualDlTamarStrategy` que reusa `tamar_dual_payoff_at` (riel TAMAR, `tamar.py` no se toca) y hace el max contra `100 × FX / fx_base` con el dólar del settle sin proyectar; el popup resuelve `_DL` clonando el bono como `DOLAR_LINKED` zero-coupon; la hoja TAMAR del ABM suma el tipo y `tc_inicial`; el prefill de Novedades manda `TM/TT/TX`+letra a la hoja TAMAR.

**Tech Stack:** Python 3.12 (`py -3.12`, sin venv), pydantic v2 (`Instrument` frozen), FastAPI + Jinja2 (popup), SQLAlchemy 2 / SQLite (catálogo), pytest con fecha fija (`tests/_clock.py`, `MONITOR_AS_OF`).

**Spec:** `docs/superpowers/specs/2026-09-08-dual-dl-tamar-design.md`

## Global Constraints

- Intérprete siempre `py -3.12` (nunca `python`/`pytest` pelados). Gate: `pwsh scripts/check.ps1` (ruff + pytest).
- TDD: cada task escribe el test, lo ve fallar, implementa, lo ve pasar, commitea. Un test que no se pone rojo al revertir el fix es decorativo.
- **No modificar `core/domain/pricing/tamar.py`** ni el contrato de cuatro pasos del dual CER (`DualCerTamarStrategy`). La red de equivalencia (`tests/test_pricing_equivalence.py`, tolerancia 1e-7) tiene que quedar verde sin tocarla.
- Convención (spec §3): TIR = **TEA nominal en pesos** `(payoff/precio)^(1/años) − 1` con `años = inst.year_fraction_to(vto, settle)`; MD bullet `años/(1+TIR)^(1/12)`; V.Téc = `max(riel TAMAR devengado al settle, riel DL)`; `price_from_tir = payoff/(1+TIR)^años`.
- Riel DL = `100 × FX / fx_base` con FX = `fx.get_mayorista_venta()` y fallback `indices.get_a3500(settle)`; **sin proyectar el dólar**. Sin `fx_base`, sin FX o sin serie TAMAR → `None` (nunca se inventa; nunca se precia con el riel TAMAR solo).
- Un `0`/`≤0` de una fuente externa o del form es dato AUSENTE (`fx_base` 0 → None).
- Tests de pricing con fecha fija (`monkeypatch.setenv("MONITOR_AS_OF", ...)` o `tests/_clock.py`), stubs para FX/A3500/TAMAR, sin red.
- Tests de web con `TestClient(app)` sobre el catálogo compartido del sandbox: lo que se siembra se limpia en `finally` (un TMVE8 que quede en la base compartida rompe la equivalencia con el motor legacy, que no conoce el tipo).
- Commits en español, imperativo, con el trailer `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`. Mensajes largos van por archivo (`git commit -F`): el hook del harness bloquea comandos con textos largos.

---

## File Structure

| Archivo | Responsabilidad en este plan |
|---|---|
| `core/domain/instrument_groups.py` | grupo `DUAL_DL`, `BOND_TYPES`, `ANALYTIC_PAYOFF_TYPES` |
| `core/domain/models.py` | `Instrument.fx_base`, `is_dual_dl_tamar` |
| `core/infrastructure/db/catalog_repository.py` | `_orm_to_domain`: `fx_base` desde `raw_fields["tc_inicial"]` |
| `core/infrastructure/repositories.py` | `build_instrument`: `fx_base` desde `tc_inicial` (form/preview) |
| `core/domain/pricing/strategies.py` | `_fx_mayorista`, `dual_dl_tamar_payoff_at`, `DualDlTamarStrategy` |
| `core/domain/pricing/registry.py` | regla del tipo nuevo |
| `core/domain/services.py` | `FinancialEngine.projected_payoff(..., fx_provider=None)` |
| `apps/web/app.py`, `apps/web/routers/panels_schema.py`, `apps/web/routers/cartera.py` | cableado explícito del tipo (pricing loop, panel, cartera) |
| `apps/web/bond_detail.py`, `apps/web/templates/fragments/bond_detail.html` | patas `_TAM`/`_DL`, cupón, `tc_inicial`, links de patas |
| `apps/web/instruments_abm.py`, `apps/web/templates/fragments/abm_form.html` | hoja TAMAR: tipo + `tc_inicial` + validación |
| `core/infrastructure/byma/universe.py` | prefill `TM/TT/TX`+letra → hoja TAMAR |
| `docs/convenciones-financieras.md`, `CLAUDE.md` | convención documentada |
| `tests/test_dual_dl_tamar.py` | dominio + strategy + registry |
| `tests/test_bond_detail_leg_dl.py` | popup y patas |
| `tests/test_abm_dual_dl_tamar.py` | ABM + prefill |
| `tests/test_perf_W1_cashflows_ancla.py` | el test del ancla aprende el tipo nuevo |

---

### Task 1: Tipo, grupo propio y `fx_base` en el dominio

**Files:**
- Modify: `core/domain/instrument_groups.py:14` (después de `DUAL_TAMAR`), `:77-78` (`BOND_TYPES`)
- Modify: `core/domain/models.py:87` (campos de `Instrument`), `:192-195` (properties)
- Modify: `core/infrastructure/db/catalog_repository.py` (helper `_fx_base` junto a `_price_alias`; kwarg en `_orm_to_domain`)
- Modify: `core/infrastructure/repositories.py:378-413` (`build_instrument`)
- Create: `tests/test_dual_dl_tamar.py`

**Interfaces:**
- Produces: `instrument_groups.DUAL_DL == ["DUAL_DL_TAMAR"]`; `Instrument.fx_base: Optional[float]`; `Instrument.is_dual_dl_tamar -> bool`; `catalog_repository._fx_base(raw) -> Optional[float]`.
- Consumers: Task 2 (strategy lee `fx_base`), Task 3 (`_bond_metadata` muestra `fx_base`), Task 4 (el ABM lo escribe en `raw_fields["tc_inicial"]`).

- [ ] **Step 1: Escribir los tests que fallan**

Crear `tests/test_dual_dl_tamar.py`:

```python
"""DUAL dólar-linked/TAMAR (`DUAL_DL_TAMAR`, caso TMVE8).

Spec: docs/superpowers/specs/2026-09-08-dual-dl-tamar-design.md. Payoff a vencimiento =
max(riel TAMAR capitalizado mensual desde la emisión, 100 × FX / TC inicial); TEA nominal en
pesos; el dólar es el del settle, sin proyectar.
"""
from __future__ import annotations

from datetime import date

import pytest

from core.domain.instrument_groups import BOND_TYPES, DUAL_DL, DUAL_TAMAR, is_known_type
from core.domain.models import Instrument

_EMISION = date(2026, 7, 31)
_VTO = date(2028, 1, 31)
_FX_BASE = 1300.0


def _inst(**kw) -> Instrument:
    base = dict(ticker="TMVE8", short_name="Dual DL/TAMAR", instrument_type="DUAL_DL_TAMAR",
                emission_date=_EMISION, maturity_date=_VTO, day_count="30/360",
                fx_base=_FX_BASE, spread_rate=0.0, cashflows=())
    base.update(kw)
    return Instrument(**base)


# ── Task 1: tipo, grupo y campo ─────────────────────────────────────────────
def test_el_tipo_existe_en_su_grupo_propio_y_no_en_dual_tamar():
    """Grupo propio a propósito: `apps/cli/bei.py` y la curva `tamar` toman DUAL_TAMAR como
    universo de TEA en pesos pura; un riel dólar la distorsionaría (spec §2)."""
    assert DUAL_DL == ["DUAL_DL_TAMAR"]
    assert "DUAL_DL_TAMAR" in BOND_TYPES and is_known_type(" dual_dl_tamar ")
    assert "DUAL_DL_TAMAR" not in DUAL_TAMAR


def test_predicados_del_modelo():
    i = _inst()
    assert i.is_dual_dl_tamar
    assert not i.is_cer and not i.is_dolar_linked and not i.is_hard_dollar
    assert not i.is_dual_cer_tamar and not i.is_dual_tamar and not i.is_tamar_puro
    assert not _inst(instrument_type="DUAL_CER_TAMAR").is_dual_dl_tamar
    assert i.fx_base == _FX_BASE
    assert Instrument(ticker="X", short_name="X", instrument_type="BONAR").fx_base is None


def test_fx_base_sale_de_tc_inicial_en_las_dos_puertas_de_lectura():
    """Motor (`_orm_to_domain`) y form/preview (`build_instrument`) leen el mismo campo de la
    hoja Dólar Linked; coma decimal tolerada; 0 o vacío = dato ausente."""
    from core.infrastructure.db.catalog_repository import _orm_to_domain
    from core.infrastructure.db.models import InstrumentORM
    from core.infrastructure.repositories import build_instrument

    orm = InstrumentORM(ticker="TMVE8", short_name="TMVE8", instrument_type="DUAL_DL_TAMAR",
                        day_count="30/360", cer_lag=10, payment_frequency=2,
                        raw_fields={"tc_inicial": "1300,5"})
    assert _orm_to_domain(orm).fx_base == 1300.5
    orm.raw_fields = {"tc_inicial": "0"}
    assert _orm_to_domain(orm).fx_base is None
    orm.raw_fields = {"tc_inicial": ""}
    assert _orm_to_domain(orm).fx_base is None
    orm.raw_fields = None
    assert _orm_to_domain(orm).fx_base is None

    row = {"ticker": "TMVE8", "tipo": "DUAL_DL_TAMAR", "fecha_emision": "2026-07-31",
           "fecha_vencimiento": "2028-01-31", "base calculo": "30/360", "tc_inicial": 1300.5}
    inst = build_instrument(row, "TAMAR", [])
    assert inst is not None and inst.instrument_type == "DUAL_DL_TAMAR"
    assert inst.fx_base == 1300.5 and inst.day_count == "30/360"
    assert build_instrument({**row, "tc_inicial": ""}, "TAMAR", []).fx_base is None
    assert build_instrument({**row, "tc_inicial": 0}, "TAMAR", []).fx_base is None
```

- [ ] **Step 2: Verlos fallar**

Run: `py -3.12 -m pytest tests/test_dual_dl_tamar.py -q --no-header -p no:cacheprovider`
Expected: FAIL — `ImportError: cannot import name 'DUAL_DL'`.

- [ ] **Step 3: Grupo y tipo**

En `core/domain/instrument_groups.py`, debajo de la línea `DUAL_TAMAR = [...]` (línea 14):

```python
# DUAL dólar-linked/TAMAR (TMVE8): paga max(riel TAMAR capitalizado, 100 × FX / TC inicial).
# Grupo PROPIO y NO dentro de DUAL_TAMAR a propósito: `apps/cli/bei.py` y la curva `tamar`
# (`routers/curva.py`) toman DUAL_TAMAR como universo de TEA en pesos "pura" y un riel dólar
# la distorsionaría. El panel TAMAR/Dual (`routers/panels_schema.py`), `app._ALL_TYPES` y el
# grupo «TAMAR» de la cartera lo suman EXPLÍCITAMENTE (spec 2026-09-08 §2).
DUAL_DL = ["DUAL_DL_TAMAR"]
```

Y en `BOND_TYPES` (línea 77-78) sumar `*DUAL_DL` después de `*DUAL_TAMAR`:

```python
BOND_TYPES = [*SOBERANOS, *BOPREALES, *TASA_FIJA, *CER, *DOLAR_LINKED, *TAMAR,
              *DUAL_TAMAR, *DUAL_DL, *OBLIGACIONES_NEGOCIABLES, *PROVINCIALES]
```

(`ANALYTIC_PAYOFF_TYPES` se toca en la Task 2, junto con la strategy: el test del ancla exige que todo tipo analítico rutee a una strategy cerrada.)

- [ ] **Step 4: Campo y predicado en el modelo**

En `core/domain/models.py`, después de `sector_override` (línea 87) y antes del bloque `price_alias`:

```python
    # DUAL dólar-linked/TAMAR (TMVE8): tipo de cambio INICIAL del prospecto (pesos/USD).
    # Vive en raw_fields["tc_inicial"] —el mismo campo de la hoja Dólar Linked, donde es
    # inerte— y es el denominador del riel DL: 100 × FX / fx_base. Sin él no hay riel.
    fx_base: Optional[float] = None
```

Y la property, después de `is_dual_cer_tamar` (línea 192-195):

```python
    @property
    def is_dual_dl_tamar(self) -> bool:
        """TMVE8: bullet que paga max(riel TAMAR capitalizado, 100 × FX/fx_base) a vto."""
        return self.norm_type == "DUAL_DL_TAMAR"
```

- [ ] **Step 5: Las dos puertas de lectura**

En `core/infrastructure/db/catalog_repository.py`, junto a `_price_alias` (antes de `_orm_to_domain`):

```python
def _fx_base(raw) -> Optional[float]:
    """`raw_fields["tc_inicial"]` (pesos/USD, hoja Dólar Linked y hoja TAMAR) →
    `Instrument.fx_base`. Coma decimal tolerada; vacío, 0 o basura → None (un 0 no es un
    tipo de cambio: es dato ausente)."""
    v = (raw or {}).get("tc_inicial")
    if v is None or str(v).strip() == "":
        return None
    try:
        f = float(str(v).strip().replace(",", "."))
    except (ValueError, TypeError):
        return None
    return f if f > 0 else None
```

y en la llamada `Instrument(...)` de `_orm_to_domain`, después de `price_alias=...`:

```python
        fx_base=_fx_base(orm.raw_fields),           # riel DL de DUAL_DL_TAMAR
```

En `core/infrastructure/repositories.py::build_instrument`, después del bloque de `cer_spread_val` (línea 384-385):

```python
    fx_base_raw = _first_present(row, ("tc_inicial", "tc inicial", "fx_base"))
    fx_base_val = _opt_float(fx_base_raw, "tc_inicial", raw_ticker)
    if fx_base_val is not None and fx_base_val <= 0:
        fx_base_val = None          # 0 del form = dato ausente, no un tipo de cambio
```

y en el `return Instrument(...)` sumar `fx_base=fx_base_val,` (por ejemplo después de `cer_spread=cer_spread_val,`).

- [ ] **Step 6: Verlos pasar + equivalencia intacta**

Run: `py -3.12 -m pytest tests/test_dual_dl_tamar.py tests/test_pricing_equivalence.py tests/test_aud_C_catalogo_db_orphan_types.py -q --no-header -p no:cacheprovider`
Expected: PASS (la equivalencia no cambia: ningún instrumento del catálogo de pruebas es del tipo nuevo).

- [ ] **Step 7: Commit**

```powershell
git add core/domain/instrument_groups.py core/domain/models.py core/infrastructure/db/catalog_repository.py core/infrastructure/repositories.py tests/test_dual_dl_tamar.py docs/superpowers/specs/2026-09-08-dual-dl-tamar-design.md
git commit -F <archivo con el mensaje>
```

Mensaje: `DUAL_DL_TAMAR: tipo en grupo propio DUAL_DL, Instrument.fx_base desde tc_inicial e is_dual_dl_tamar` + una línea sobre el porqué del grupo propio + trailer.

---

### Task 2: `DualDlTamarStrategy`, registry, tipo analítico y cableado del panel

**Files:**
- Modify: `core/domain/pricing/strategies.py` (imports; funciones nuevas antes de `class DualCerTamarStrategy`; clase nueva al final)
- Modify: `core/domain/pricing/registry.py:10-32`
- Modify: `core/domain/instrument_groups.py:69` (`ANALYTIC_PAYOFF_TYPES`)
- Modify: `apps/web/app.py:52-61` (`_ALL_TYPES`), `apps/web/routers/panels_schema.py:151`, `apps/web/routers/cartera.py:25-36`
- Modify: `tests/test_perf_W1_cashflows_ancla.py:128-143`
- Modify: `tests/test_dual_dl_tamar.py` (sección strategy)
- Modify: `docs/convenciones-financieras.md` (sección nueva después de «Bonos TAMAR», índice), `CLAUDE.md` (Pricing)

**Interfaces:**
- Consumes: `Instrument.fx_base`, `is_dual_dl_tamar` (Task 1); `tamar.tamar_dual_payoff_at(instrument, ref_date, indices_provider, *, tamar_forecast=None, to_date=None, cer_settle_lag=None)`; `PricingContext(settle, indices, fx, tamar_forecast, settle_lag)`; `_fx_offer(fx, method)` (ya existe en `strategies.py`).
- Produces: `strategies._fx_mayorista(ctx) -> Optional[float]`; `strategies.dual_dl_tamar_payoff_at(inst, ref, ctx, *, to_date=None, fx_rate=None) -> Optional[float]`; `strategies.DualDlTamarStrategy`. Task 3 usa `dual_dl_tamar_payoff_at` desde `FinancialEngine.projected_payoff`.

- [ ] **Step 1: Tests de la strategy (fallan)**

Agregar al final de `tests/test_dual_dl_tamar.py`:

```python
# ── Task 2: strategy ────────────────────────────────────────────────────────
from core.domain.conventions import days_30_360, tamar_tem  # noqa: E402
from core.domain.models import MarketSnapshot  # noqa: E402
from core.domain.pricing.context import PricingContext  # noqa: E402
from core.domain.pricing.registry import strategy_for  # noqa: E402
from core.domain.pricing.strategies import (  # noqa: E402
    DolarLinkedStrategy, DualCerTamarStrategy, DualDlTamarStrategy, TamarStrategy,
    dual_dl_tamar_payoff_at,
)
from core.domain.pricing.tamar import tamar_dual_payoff_at  # noqa: E402
from core.domain.services import FinancialEngine  # noqa: E402

_SETTLE = date(2026, 9, 8)
_HOY = "2026-09-07"
_TNA = 30.0            # TAMAR constante (TNA %)


class _Idx:
    """TAMAR constante; sin CER (este tipo no lo pide) y SIN get_a3500."""
    def __init__(self, tna=_TNA):
        self._tna = tna

    def get_tamar(self, d=None):
        return self._tna

    @property
    def _cache_tamar(self):
        return {date(2026, 9, 1): self._tna}

    def get_cer(self, d):
        return None


class _IdxConA3500(_Idx):
    def get_a3500(self, d=None):
        return 1500.0


class _Fx:
    def __init__(self, v):
        self._v = v

    def get_mayorista_venta(self):
        return self._v


class _FxCaido:
    def get_mayorista_venta(self):
        return None


@pytest.fixture(autouse=True)
def _freeze(monkeypatch):
    monkeypatch.setenv("MONITOR_AS_OF", _HOY)


def _snap(inst, price):
    return MarketSnapshot(instrument=inst, price=price)


def _riel_tamar_a_mano():
    """100 × (1 + TEM)^N con N = días 30/360 / 30 — la fórmula BONTE TAMAR, sin pasar por
    tamar.py, para que el test no dependa de lo que pinea."""
    return 100.0 * (1.0 + tamar_tem(_TNA / 100.0)) ** (days_30_360(_EMISION, _VTO) / 30.0)


def test_registry_rutea_el_tipo_nuevo_sin_mover_a_los_vecinos():
    assert isinstance(strategy_for(_inst()), DualDlTamarStrategy)
    assert isinstance(strategy_for(_inst(instrument_type="PURO")), TamarStrategy)
    assert isinstance(strategy_for(_inst(instrument_type="DUAL")), TamarStrategy)
    assert isinstance(strategy_for(_inst(instrument_type="DUAL_CER_TAMAR", cer_base=700.0)),
                      DualCerTamarStrategy)
    assert isinstance(strategy_for(_inst(instrument_type="DOLAR_LINKED")), DolarLinkedStrategy)


def test_gana_el_riel_tamar_cuando_el_dolar_no_alcanza():
    """FX 1500 / 1300 = 115,4 < riel TAMAR (≈156): paga el riel TAMAR, exactamente el de
    la fórmula BONTE."""
    ctx = PricingContext(settle=_SETTLE, indices=_Idx(), fx=_Fx(1500.0))
    payoff = dual_dl_tamar_payoff_at(_inst(), _SETTLE, ctx)
    assert payoff == pytest.approx(_riel_tamar_a_mano(), rel=1e-12)
    assert payoff == pytest.approx(tamar_dual_payoff_at(_inst(), _SETTLE, _Idx(), to_date=_VTO))
    assert payoff > 100.0 * 1500.0 / _FX_BASE


def test_gana_el_riel_dl_cuando_el_dolar_supera_la_capitalizacion():
    """FX 2600 / 1300 = 200 > riel TAMAR (≈156): paga el riel DL, sin proyectar el dólar."""
    ctx = PricingContext(settle=_SETTLE, indices=_Idx(), fx=_Fx(2600.0))
    assert dual_dl_tamar_payoff_at(_inst(), _SETTLE, ctx) == pytest.approx(200.0)


def test_vtec_es_el_max_de_rieles_devengado_al_settle():
    """Al settle el riel TAMAR lleva ~1,3 meses (≈103) y el DL vale 115,4: manda el DL.
    Antes de la emisión, 100."""
    fx = _Fx(1500.0)
    vt = FinancialEngine.calculate_technical_value(_snap(_inst(), 100.0), _Idx(), fx,
                                                   ref_date=_SETTLE)
    tamar_al_settle = tamar_dual_payoff_at(_inst(), _SETTLE, _Idx(), to_date=_SETTLE)
    assert tamar_al_settle is not None and 100.0 < tamar_al_settle < 110.0
    assert vt == pytest.approx(max(tamar_al_settle, 100.0 * 1500.0 / _FX_BASE))
    assert vt == pytest.approx(100.0 * 1500.0 / _FX_BASE)
    pre = FinancialEngine.calculate_technical_value(_snap(_inst(), 100.0), _Idx(), fx,
                                                    ref_date=date(2026, 7, 1))
    assert pre == 100.0


def test_tir_es_tea_nominal_contra_el_payoff_y_md_usa_m12():
    inst = _inst()
    fx = _Fx(1500.0)
    tir = FinancialEngine.calculate_tir(_snap(inst, 120.0), _Idx(), fx, settle_date=_SETTLE)
    years = inst.year_fraction_to(_VTO, _SETTLE)
    assert tir == pytest.approx((_riel_tamar_a_mano() / 120.0) ** (1.0 / years) - 1.0, rel=1e-9)
    md = FinancialEngine.calculate_duration(_snap(inst, 120.0), tir, settle_date=_SETTLE)
    assert md == pytest.approx(years / (1.0 + tir) ** (1.0 / 12.0), rel=1e-9)


@pytest.mark.parametrize("fx_val", [1500.0, 2600.0])
@pytest.mark.parametrize("price", [90.0, 120.0, 150.0])
def test_round_trip_precio_tir_precio(fx_val, price):
    inst = _inst()
    fx = _Fx(fx_val)
    tir = FinancialEngine.calculate_tir(_snap(inst, price), _Idx(), fx, settle_date=_SETTLE)
    assert tir is not None
    back = FinancialEngine.price_from_tir(_snap(inst, price), tir, _Idx(), fx,
                                          settle_date=_SETTLE)
    assert back == pytest.approx(price, rel=1e-9)


def test_sin_tc_inicial_no_se_inventa_ni_se_precia_con_el_riel_tamar_solo():
    inst = _inst(fx_base=None)
    fx = _Fx(1500.0)
    assert FinancialEngine.calculate_tir(_snap(inst, 120.0), _Idx(), fx, settle_date=_SETTLE) is None
    assert FinancialEngine.calculate_technical_value(_snap(inst, 120.0), _Idx(), fx,
                                                     ref_date=_SETTLE) is None
    assert FinancialEngine.price_from_tir(_snap(inst, 120.0), 0.3, _Idx(), fx,
                                          settle_date=_SETTLE) is None


def test_sin_dolar_vivo_cae_al_a3500_del_bcra_y_sin_ninguno_devuelve_none():
    inst = _inst()
    con_a3500 = FinancialEngine.calculate_tir(_snap(inst, 120.0), _IdxConA3500(), _FxCaido(),
                                              settle_date=_SETTLE)
    vivo = FinancialEngine.calculate_tir(_snap(inst, 120.0), _Idx(), _Fx(1500.0),
                                         settle_date=_SETTLE)
    assert con_a3500 == pytest.approx(vivo)             # mismo dólar (1500) por otra vía
    assert FinancialEngine.calculate_tir(_snap(inst, 120.0), _Idx(), _FxCaido(),
                                         settle_date=_SETTLE) is None
    assert FinancialEngine.calculate_tir(_snap(inst, 120.0), _Idx(), None,
                                         settle_date=_SETTLE) is None


def test_el_forecast_tamar_mueve_solo_el_riel_tamar():
    inst = _inst()
    manda_tamar = _Fx(1500.0)
    base = FinancialEngine.calculate_tir(_snap(inst, 120.0), _Idx(), manda_tamar,
                                         settle_date=_SETTLE)
    alto = FinancialEngine.calculate_tir(_snap(inst, 120.0), _Idx(), manda_tamar,
                                         settle_date=_SETTLE, tamar_forecast=0.60)
    assert alto > base
    manda_dl = _Fx(2600.0)
    assert (FinancialEngine.calculate_tir(_snap(inst, 120.0), _Idx(), manda_dl,
                                          settle_date=_SETTLE, tamar_forecast=0.60)
            == pytest.approx(FinancialEngine.calculate_tir(_snap(inst, 120.0), _Idx(),
                                                           manda_dl, settle_date=_SETTLE)))


def test_vencido_cae_al_camino_general():
    inst = _inst(maturity_date=date(2026, 6, 30))
    assert FinancialEngine.calculate_tir(_snap(inst, 120.0), _Idx(), _Fx(1500.0),
                                         settle_date=_SETTLE) is None   # sin flujos: vanilla → None


def test_el_tipo_es_analitico():
    from core.domain.instrument_groups import ANALYTIC_PAYOFF_TYPES, has_closed_form_payoff
    assert "DUAL_DL_TAMAR" in ANALYTIC_PAYOFF_TYPES and has_closed_form_payoff("dual_dl_tamar")


def test_el_tipo_entra_al_panel_tamar_al_pricing_y_a_la_cartera_pero_no_a_bei_ni_curva():
    from apps.cli import bei
    from apps.web.app import _ALL_TYPES
    from apps.web.routers import cartera, curva
    from apps.web.routers.panels_schema import PANELS
    assert "DUAL_DL_TAMAR" in _ALL_TYPES
    assert "DUAL_DL_TAMAR" in PANELS["tamar"][1]
    assert cartera._GRUPO["DUAL_DL_TAMAR"] == "TAMAR"
    assert "DUAL_DL_TAMAR" not in curva._CURVA_GROUPS["tamar"][1]
    assert "DUAL_DL_TAMAR" not in bei.DUAL_TAMAR
```

Y en `tests/test_perf_W1_cashflows_ancla.py::test_analytic_payoff_types_coincide_con_el_registry` reemplazar las tres líneas:

```python
    from core.domain.pricing.strategies import (
        DualCerTamarStrategy, DualDlTamarStrategy, TamarStrategy,
    )

    assert ANALYTIC_PAYOFF_TYPES == frozenset({"PURO", "DUAL", "DUAL_CER_TAMAR", "DUAL_DL_TAMAR"})

    cerradas = (TamarStrategy, DualCerTamarStrategy, DualDlTamarStrategy)
```

- [ ] **Step 2: Verlos fallar**

Run: `py -3.12 -m pytest tests/test_dual_dl_tamar.py tests/test_perf_W1_cashflows_ancla.py -q --no-header -p no:cacheprovider`
Expected: FAIL — `ImportError: cannot import name 'DualDlTamarStrategy'`.

- [ ] **Step 3: Strategy**

En `core/domain/pricing/strategies.py`:

1. Imports: agregar `from datetime import date` (arriba de `from typing import Optional`).
2. Actualizar la tabla del docstring del módulo con una fila:
   `| DualDlTamarStrategy  | DUAL_DL_TAMAR        | payoff max(TAMAR, 100×FX/fx_base); TIR cerrada; m=12 |`
3. Antes de `class DualCerTamarStrategy`, agregar:

```python
def _fx_mayorista(ctx: PricingContext) -> Optional[float]:
    """Dólar del riel dólar-linked: mayorista venta VIVO (dolarapi, el mismo que usa
    `DolarLinkedStrategy`); si no responde, el fixing A3500 del BCRA al settle (forward-fill
    del provider); si tampoco, None. Nunca 0: un 0 de una fuente externa es dato ausente."""
    rate = _fx_offer(ctx.fx, "get_mayorista_venta")
    if rate is None and ctx.indices is not None:
        fn = getattr(ctx.indices, "get_a3500", None)
        rate = fn(ctx.settle) if callable(fn) else None
    return rate if (rate is not None and rate > 0) else None


def dual_dl_tamar_payoff_at(inst, ref: date, ctx: PricingContext, *,
                            to_date: Optional[date] = None,
                            fx_rate: Optional[float] = None) -> Optional[float]:
    """Payoff per-100 de un DUAL_DL_TAMAR a `to_date` (default: vencimiento):
    ``max(riel TAMAR, 100 × FX / fx_base)``.

    El riel TAMAR es `tamar_dual_payoff_at` TAL CUAL: para este tipo devuelve la
    capitalización mensual desde la emisión (sin floor —no es DUAL— y sin riel CER —no es
    DUAL_CER_TAMAR—), así que `tamar.py` y su contrato de cuatro pasos no se tocan. El riel
    DL usa el dólar del settle SIN proyectarlo (decisión de David, spec §1); `fx_rate`
    permite inyectarlo (popup/tests). Sin `fx_base`, sin dólar o sin serie TAMAR → None:
    no se inventa y no se precia con el riel TAMAR solo (un dual sin su segundo riel es un
    dato incompleto; la ABM exige `tc_inicial`)."""
    if not inst.fx_base or inst.fx_base <= 0:
        return None
    rate = fx_rate if fx_rate is not None else _fx_mayorista(ctx)
    if rate is None or rate <= 0:
        return None
    end = to_date if to_date is not None else inst.maturity_date
    riel_tamar = tamar_dual_payoff_at(inst, ref, ctx.indices,
                                      tamar_forecast=ctx.tamar_forecast, to_date=end)
    if riel_tamar is None:
        return None
    return max(riel_tamar, 100.0 * rate / inst.fx_base)
```

4. Al final del archivo:

```python
class DualDlTamarStrategy(VanillaStrategy):
    """DUAL dólar-linked/TAMAR (TMVE8). Bullet que paga a vencimiento
    ``max(riel TAMAR capitalizado mensual desde la emisión, 100 × FX / fx_base)``
    (`dual_dl_tamar_payoff_at`).

    Misma convención que `DualCerTamarStrategy`: **TIR nominal (TEA)** contra el payoff
    proyectado, `(payoff / precio)^(1/años) − 1`; **V.Téc = max de rieles devengado al
    settle** (100 antes de la emisión); **MD bullet con m=12**; `price_from_tir` es la
    inversa exacta (round-trip por construcción). El riel DL toma el dólar del settle y no lo
    proyecta (spec 2026-09-08 §1). Sin `fx_base` o sin dólar NO precia (None). Vencido →
    camino general (`VanillaStrategy`)."""

    def technical_value(self, inst, ctx: PricingContext):
        ref = ctx.settle
        if not inst.emission_date or inst.emission_date >= ref:
            return 100.0
        return dual_dl_tamar_payoff_at(inst, ref, ctx, to_date=ref)

    @staticmethod
    def _vivo(inst, ctx: PricingContext) -> bool:
        return bool(inst.emission_date and inst.maturity_date and inst.maturity_date > ctx.settle)

    def tir(self, inst, price, ctx: PricingContext):
        if not self._vivo(inst, ctx):
            return super().tir(inst, price, ctx)
        payoff = dual_dl_tamar_payoff_at(inst, ctx.settle, ctx)
        if payoff is None or payoff <= 0 or price is None or price <= 0:
            return None
        years = inst.year_fraction_to(inst.maturity_date, ctx.settle)
        if years <= 0:
            return None
        try:
            return (payoff / price) ** (1.0 / years) - 1.0
        except (ValueError, OverflowError, ZeroDivisionError):
            return None

    def duration(self, inst, tir, ctx: PricingContext):
        if tir is None or not np.isfinite(tir) or tir <= -1.0:
            return None
        if self._vivo(inst, ctx):
            years = inst.year_fraction_to(inst.maturity_date, ctx.settle)
            return years / (1 + tir) ** (1.0 / 12.0)
        return super().duration(inst, tir, ctx)

    def price_from_tir(self, inst, tir, ctx: PricingContext):
        if not self._vivo(inst, ctx):
            return super().price_from_tir(inst, tir, ctx)
        payoff = dual_dl_tamar_payoff_at(inst, ctx.settle, ctx)
        if payoff is None:
            return None
        years = inst.year_fraction_to(inst.maturity_date, ctx.settle)
        return payoff / (1 + tir) ** years
```

- [ ] **Step 4: Registry, tipo analítico y cableado**

`core/domain/pricing/registry.py`: sumar `DualDlTamarStrategy` al import y la regla PRIMERA de `_RULES` (disjunta de todas):

```python
_RULES: List[Tuple[Callable, PricingStrategy]] = [
    (lambda i: i.is_dual_dl_tamar, DualDlTamarStrategy()),
    (lambda i: i.is_dual_cer_tamar, DualCerTamarStrategy()),
    ...
```

`core/domain/instrument_groups.py:69`:

```python
ANALYTIC_PAYOFF_TYPES = frozenset({*TAMAR, *DUAL_TAMAR, *DUAL_DL})   # PURO, DUAL, DUAL_CER_TAMAR, DUAL_DL_TAMAR
```

`apps/web/app.py:52-61`: sumar `DUAL_DL` al import de `instrument_groups` y `*DUAL_DL` a `_ALL_TYPES` después de `*DUAL_TAMAR`.

`apps/web/routers/panels_schema.py:151`:

```python
    "tamar": ("TAMAR / DUAL", {"PURO", "DUAL", "DUAL_CER_TAMAR", "DUAL_DL_TAMAR"}, _TAMAR_COLS),
```

`apps/web/routers/cartera.py:25-36`: sumar `DUAL_DL` al import y `("TAMAR", TAMAR + DUAL_TAMAR + DUAL_DL)`.

`apps/web/routers/curva.py` y `apps/cli/bei.py`: **sin cambios** (el test lo pinea).

- [ ] **Step 5: Verlos pasar + la red de siempre**

Run: `py -3.12 -m pytest tests/test_dual_dl_tamar.py tests/test_perf_W1_cashflows_ancla.py tests/test_pricing_equivalence.py tests/test_aud_B_financiero_dual_cer_tamar.py tests/test_metric_guards.py tests/test_panels_router.py -q --no-header -p no:cacheprovider`
Expected: PASS.

- [ ] **Step 6: Documentar la convención**

En `docs/convenciones-financieras.md`, después de la sección «## Bonos TAMAR (PURO, DUAL, DUAL_CER_TAMAR)» (termina en la línea `---` previa a «## Bonos DOLAR LINKED») insertar:

```markdown
## Bonos DUAL DÓLAR-LINKED / TAMAR (`DUAL_DL_TAMAR`)

- Caso: TMVE8 (emisión 2026-07-31, vto 2028-01-31). Ficha BYMA: a vencimiento paga el máximo
  entre el VN al **tipo de cambio aplicable** y el VN al **tipo de cambio inicial** más TAMAR
  TEM capitalizable mensual. Spec: `docs/superpowers/specs/2026-09-08-dual-dl-tamar-design.md`.
- **Payoff** (`pricing/strategies.py::dual_dl_tamar_payoff_at`): `max(riel TAMAR, 100 × FX /
  fx_base)`. Riel TAMAR = `tamar_dual_payoff_at` sin modificar (capitalización mensual 30/360
  desde la emisión, `spread_rate` sumado a la TNA). Riel DL: FX = mayorista venta vivo
  (`fx.get_mayorista_venta`, el mismo de Dólar Linked), fallback A3500 BCRA al settle; **sin
  proyectar el dólar** (decisión 2026-09-08). `fx_base` = `raw_fields["tc_inicial"]`.
- **TIR**: TEA nominal en pesos `(payoff/precio)^(1/años) − 1`, `años = year_fraction_to`
  (30/360). **V.Téc** = max de rieles devengado al settle; 100 antes de la emisión. **MD**
  bullet m=12. `price_from_tir` inversa exacta. Sin `fx_base`/FX/TAMAR → None (no se precia
  con el riel TAMAR solo).
- Grupo propio `instrument_groups.DUAL_DL` (fuera de BEI y de la curva `tamar`; adentro del
  panel TAMAR/Dual, `_ALL_TYPES` y la cartera). Tipo analítico: fila ancla, sin schedule.
- Popup: `<T>_TAM` (TEA del riel TAMAR, clon PURO) y `<T>_DL` (TIR en USD del riel DL: clon
  `DOLAR_LINKED` zero-coupon de 100 USD a vto, precio ÷ mayorista).
- Guardianes: `tests/test_dual_dl_tamar.py` (rieles, V.Téc, round-trip, m=12, None),
  `tests/test_bond_detail_leg_dl.py`, `tests/test_abm_dual_dl_tamar.py`. Sin golden externo
  todavía (inventario).

---
```

Y sumar la entrada correspondiente en el «## Índice» del documento (misma forma que las entradas vecinas).

En `CLAUDE.md`, en **Pricing**, después del bullet «V.Téc / payoff DUAL_CER_TAMAR» agregar:

```markdown
- **DUAL_DL_TAMAR (TMVE8)**: `max(riel TAMAR, 100 × FX/fx_base)` con el dólar del settle, sin
  lag ni spread en el riel DL y **sin proyectar**; reusa `tamar_dual_payoff_at` sin tocarlo.
  Grupo propio `DUAL_DL` (fuera de BEI/curva). Sin `tc_inicial` no precia.
```

- [ ] **Step 7: Commit**

```powershell
git add core/domain/pricing/strategies.py core/domain/pricing/registry.py core/domain/instrument_groups.py apps/web/app.py apps/web/routers/panels_schema.py apps/web/routers/cartera.py tests/test_perf_W1_cashflows_ancla.py tests/test_dual_dl_tamar.py docs/convenciones-financieras.md CLAUDE.md
git commit -F <archivo con el mensaje>
```

Mensaje: `DualDlTamarStrategy: max(riel TAMAR, 100×FX/TC inicial), TEA nominal, m=12; tipo analítico y cableado al panel TAMAR/Dual` + trailer.

---

### Task 3: Popup — patas `_TAM`/`_DL`, `projected_payoff` con FX, cupón y links

**Files:**
- Modify: `core/domain/services.py:121-133` (`projected_payoff`)
- Modify: `apps/web/bond_detail.py:65-67` (`_TAMAR_TYPES`, `_VALID_LEGS`), `:110-128` (`_apply_leg`), `:131-158` (`_resolve_instrument_and_leg`), `:161-190` (`_cupon_label`), `:222-270` (`_bond_metadata`), `:273-303` (`_cashflows_all`) y la llamada en `get_bond_detail`
- Modify: `apps/web/templates/fragments/bond_detail.html:9-21` (botones de patas), `:33-37` (TC inicial)
- Create: `tests/test_bond_detail_leg_dl.py`

**Interfaces:**
- Consumes: `strategies.dual_dl_tamar_payoff_at` (Task 2), `Instrument.fx_base`/`is_dual_dl_tamar` (Task 1).
- Produces: `FinancialEngine.projected_payoff(instrument, indices_provider, tamar_forecast=None, ref_date=None, fx_provider=None)` (firma extendida con kwarg opcional; los callers viejos siguen iguales); `_bond_metadata` devuelve `meta["tc_inicial"]` y `meta["legs"] = [(label, ticker), ...]` para el tipo.

- [ ] **Step 1: Tests (fallan)**

Crear `tests/test_bond_detail_leg_dl.py`:

```python
"""Popup de un DUAL_DL_TAMAR: TEA en pesos en la vista base, pata `_TAM` (riel TAMAR como
PURO) y pata `_DL` (TIR en USD del riel dólar-linked como DOLAR_LINKED zero-coupon).
Spec 2026-09-08 §4. Providers stub, sin red; TMVE8 no está en el catálogo de pruebas: el
repo es un doble; el test de router siembra y limpia en el catálogo compartido."""
from __future__ import annotations

from datetime import date

import pytest
from fastapi.testclient import TestClient

from apps.web import bond_detail
from apps.web.app import app
from core.domain.models import Cashflow, Instrument, MarketSnapshot
from core.domain.services import FinancialEngine

_EMISION = date(2026, 7, 31)
_VTO = date(2028, 1, 31)
_FX_BASE = 1300.0
_FX = 1500.0
_PRICE = 120.0
_HOY = "2026-09-07"


def _inst(**kw) -> Instrument:
    base = dict(ticker="TMVE8", short_name="Dual DL/TAMAR", instrument_type="DUAL_DL_TAMAR",
                emission_date=_EMISION, maturity_date=_VTO, day_count="30/360",
                fx_base=_FX_BASE, spread_rate=0.0, cashflows=())
    base.update(kw)
    return Instrument(**base)


class _Repo:
    def __init__(self, *insts):
        self._por_ticker = {i.ticker: i for i in insts}

    def get_instrument_by_ticker(self, t):
        return self._por_ticker.get(t)


class _Prov:
    def fetch_snapshots(self, tickers):
        return {t: MarketSnapshot(instrument=None, price=_PRICE) for t in tickers}

    def fetch_historical_prices(self, t, d):
        return {}


class _Idx:
    def get_tamar(self, d=None):
        return 30.0

    @property
    def _cache_tamar(self):
        return {date(2026, 9, 1): 30.0}

    def get_cer(self, d=None):
        return None


class _Fx:
    def get_mayorista_venta(self):
        return _FX


@pytest.fixture(autouse=True)
def _freeze(monkeypatch):
    monkeypatch.setenv("MONITOR_AS_OF", _HOY)


def _detail(ticker, repo=None):
    return bond_detail.get_bond_detail(ticker, repo or _Repo(_inst()), _Prov(), _Idx(), _Fx(),
                                       settlement_lag=1)


def test_la_vista_base_publica_tea_en_pesos_tc_inicial_y_las_dos_patas():
    d = _detail("TMVE8")
    assert d is not None and d["ticker"] == "TMVE8"
    settle = bond_detail._resolve_ref(1)
    esperado = FinancialEngine.calculate_tir(MarketSnapshot(instrument=_inst(), price=_PRICE),
                                             _Idx(), _Fx(), settle_date=settle)
    assert d["metrics"]["tir"] == pytest.approx(esperado)
    assert d["meta"]["tc_inicial"] == _FX_BASE
    assert d["meta"]["is_tamar_family"] is True
    assert d["meta"]["cupon"] == "max(TAMAR + 0.000%, dólar-linked)"
    assert d["meta"]["legs"] == [("Riel TAMAR", "TMVE8_TAM"), ("Riel dólar-linked", "TMVE8_DL")]
    # El payback proyectado de la tabla de flujos es el max de rieles (necesita el FX).
    amorts = [r["amortization"] for r in d["cashflows"] if r["amortization"]]
    payoff = FinancialEngine.projected_payoff(_inst(), _Idx(), ref_date=settle, fx_provider=_Fx())
    assert payoff is not None and amorts == [pytest.approx(payoff)]
    assert FinancialEngine.projected_payoff(_inst(), _Idx(), ref_date=settle) is None  # sin FX: None


def test_la_pata_dl_es_la_tir_en_usd_de_un_dolar_linked_zero_coupon():
    d = _detail("TMVE8_DL")
    assert d is not None and d["ticker"] == "TMVE8_DL" and d["meta"]["ticker"] == "TMVE8_DL"
    assert "legs" not in d["meta"]                       # las patas no anidan
    oraculo = Instrument(ticker="TMVE8", short_name="x", instrument_type="DOLAR_LINKED",
                         emission_date=_EMISION, maturity_date=_VTO, day_count="30/360",
                         cashflows=(Cashflow(date=_VTO, amortization=100.0, interest=0.0),))
    settle = bond_detail._resolve_ref(1)
    esperado = FinancialEngine.calculate_tir(MarketSnapshot(instrument=oraculo, price=_PRICE),
                                             _Idx(), _Fx(), settle_date=settle)
    assert esperado is not None
    assert d["metrics"]["tir"] == pytest.approx(esperado)
    assert d["metrics"]["technical_value"] == pytest.approx(100.0 * _FX)   # V.Téc en pesos


def test_la_pata_tam_es_la_tea_del_riel_tamar_solo():
    d = _detail("TMVE8_TAM")
    assert d is not None and d["ticker"] == "TMVE8_TAM"
    puro = _inst(instrument_type="PURO")
    settle = bond_detail._resolve_ref(1)
    esperado = FinancialEngine.calculate_tir(MarketSnapshot(instrument=puro, price=_PRICE),
                                             _Idx(), _Fx(), settle_date=settle)
    assert d["metrics"]["tir"] == pytest.approx(esperado)


def test_dl_fuera_del_tipo_y_tf_sobre_el_tipo_son_tickers_inexistentes():
    al30 = Instrument(ticker="AL30", short_name="AL30", instrument_type="BONAR",
                      maturity_date=date(2030, 7, 9),
                      cashflows=(Cashflow(date=date(2030, 7, 9), amortization=100.0, interest=0.5),))
    repo = _Repo(_inst(), al30)
    assert bond_detail.get_bond_detail("AL30_DL", repo, _Prov(), _Idx(), _Fx()) is None
    assert bond_detail.get_bond_detail("TMVE8_TF", repo, _Prov(), _Idx(), _Fx()) is None
    assert bond_detail.get_bond_detail("TMVE8_DL", _Repo(_inst(maturity_date=None)),
                                       _Prov(), _Idx(), _Fx()) is None


def test_calculadora_y_tir_nominal_m12():
    r = bond_detail.calculate("TMVE8_DL", _Repo(_inst()), _Prov(), _Idx(), _Fx(),
                              mode="from_price", price=_PRICE)
    assert r is not None
    assert bond_detail._nominal_tna(_inst(), 0.30) == pytest.approx(
        FinancialEngine.tea_to_tna_monthly(0.30))


def test_router_renderiza_la_pata_dl_y_los_botones():
    """Siembra TMVE8 en el catálogo compartido y lo limpia SIEMPRE (un DUAL_DL_TAMAR que
    quede ahí rompe la equivalencia con el motor legacy, que no conoce el tipo)."""
    from apps.web.deps import get_repo
    from core.infrastructure.db.catalog_repository import init_db
    from core.infrastructure.db.engine import SessionLocal
    from core.infrastructure.db.models import CashflowORM, InstrumentORM

    init_db()
    with SessionLocal.begin() as s:
        orm = InstrumentORM(ticker="TMVE8", short_name="TMVE8", instrument_type="DUAL_DL_TAMAR",
                            sheet="TAMAR", emission_date=_EMISION, maturity_date=_VTO,
                            day_count="30/360", raw_fields={"tipo": "DUAL_DL_TAMAR",
                                                             "tc_inicial": _FX_BASE})
        orm.cashflows = [CashflowORM(ticker="TMVE8", fecha_pago=_VTO, amortizacion=0.0,
                                     cupon_interes=0.0, es_ancla=True)]
        s.add(orm)
    try:
        get_repo().reload()
        with TestClient(app) as c:
            r = c.get("/bond/TMVE8/detail")
            assert r.status_code == 200
            assert "/bond/TMVE8_DL/detail" in r.text and "/bond/TMVE8_TAM/detail" in r.text
            assert "TC inicial" in r.text
            r = c.get("/bond/TMVE8_DL/detail")
            assert r.status_code == 200 and "TMVE8_DL" in r.text
            assert "Riel dólar-linked" not in r.text          # dentro de una pata no se anidan
    finally:
        with SessionLocal.begin() as s:
            s.query(CashflowORM).filter_by(ticker="TMVE8").delete()
            s.query(InstrumentORM).filter_by(ticker="TMVE8").delete()
        get_repo().reload()
```

- [ ] **Step 2: Verlos fallar**

Run: `py -3.12 -m pytest tests/test_bond_detail_leg_dl.py -q --no-header -p no:cacheprovider`
Expected: FAIL (el `_DL` no resuelve → `None`; `projected_payoff` no acepta `fx_provider`).

- [ ] **Step 3: `projected_payoff` con FX**

En `core/domain/services.py` reemplazar `projected_payoff`:

```python
    @staticmethod
    def projected_payoff(instrument, indices_provider, tamar_forecast: Optional[float] = None,
                         ref_date: Optional[date] = None, fx_provider=None) -> Optional[float]:
        """Payback proyectado per-100 a vencimiento para bonos TAMAR-family. Un
        DUAL_DL_TAMAR necesita además el dólar (`fx_provider`): sin él devuelve None, nunca
        el riel TAMAR solo."""
        if instrument is None or indices_provider is None:
            return None
        if not instrument.emission_date or not instrument.maturity_date:
            return None
        settle = ref_date if ref_date is not None else date.today()
        if instrument.maturity_date <= settle:
            return None
        if instrument.is_dual_dl_tamar:
            ctx = PricingContext(settle=settle, indices=indices_provider, fx=fx_provider,
                                 tamar_forecast=tamar_forecast)
            return dual_dl_tamar_payoff_at(instrument, settle, ctx)
        return tamar_dual_payoff_at(instrument, settle, indices_provider,
                                    tamar_forecast=tamar_forecast, to_date=instrument.maturity_date)
```

con los imports arriba del archivo: `from core.domain.pricing.context import PricingContext` y `from core.domain.pricing.strategies import dual_dl_tamar_payoff_at` (si `PricingContext` ya está importado, no duplicar).

- [ ] **Step 4: bond_detail**

En `apps/web/bond_detail.py`:

1. Import: sumar `Cashflow` al import de `core.domain.models`.
2. Constantes (líneas 65-67):

```python
_TAMAR_TYPES = frozenset({"PURO", "DUAL", "DUAL_CER_TAMAR", "DUAL_DL_TAMAR"})

_VALID_LEGS = frozenset({"TF", "TAM", "CER", "DL"})
```

3. `_apply_leg`: agregar antes del `return instrument, None` final:

```python
    if leg == "DL":
        # Riel dólar-linked SOLO: el mismo papel como DOLAR_LINKED zero-coupon de 100 USD a
        # vencimiento → DolarLinkedStrategy publica la TIR en USD (precio ÷ mayorista) y el
        # V.Téc en pesos (100 × FX). Sin código nuevo de pricing.
        return instrument.model_copy(update={
            "instrument_type": "DOLAR_LINKED", "cer_base": 1.0, "floor_rate_monthly": None,
            "spread_rate": None,
            "cashflows": (Cashflow(date=instrument.maturity_date, amortization=100.0,
                                   interest=0.0),),
        }), None
```

y en el docstring de `_apply_leg` una línea: `- DL:  clona como DOLAR_LINKED zero-coupon (sólo DUAL_DL_TAMAR): TIR en USD del riel dólar-linked.`

4. `_resolve_instrument_and_leg`: después del guard de `TF` (línea 151-152):

```python
    # DL sólo existe donde hay riel dólar-linked (y un vencimiento para el flujo único).
    if leg == "DL" and not (instrument.is_dual_dl_tamar and instrument.maturity_date):
        return None
```

5. `_cupon_label`: antes de `if itype == "DUAL_CER_TAMAR":`:

```python
    if itype == "DUAL_DL_TAMAR":
        return f"max(TAMAR + {(sp or 0)*100:.3f}%, dólar-linked)"
```

6. `_bond_metadata`: después de `is_dual = itype == "DUAL"` agregar `is_dual_dl = itype == "DUAL_DL_TAMAR"`; y antes del `return meta`:

```python
    # DUAL_DL_TAMAR: el denominador del riel DL, y las dos patas del popup (sólo en la vista
    # base: dentro de una pata no se anidan).
    if is_dual_dl:
        meta["tc_inicial"] = _safe(instrument.fx_base)
        if leg is None:
            meta["legs"] = [("Riel TAMAR", f"{instrument.ticker}_TAM"),
                            ("Riel dólar-linked", f"{instrument.ticker}_DL")]
```

7. `_cashflows_all(instrument, ref_date, indices=None, tamar_forecast=None, fx=None)`: sumar el parámetro `fx=None` y pasar `fx_provider=fx` en la llamada a `FinancialEngine.projected_payoff(...)` (línea 301-303). En `get_bond_detail`, la llamada pasa a `_cashflows_all(instrument, ref_date, indices=indices_eff, tamar_forecast=tamar_forecast, fx=fx)`.

8. Template `apps/web/templates/fragments/bond_detail.html`: en `<div class="toggle">`, antes del botón `T+0`:

```html
        {% for label, tk in meta.legs|default([]) %}
        <button type="button" hx-get="/bond/{{ tk }}/detail?lag={{ d.settlement_lag }}"
                hx-target="#modal" hx-swap="innerHTML">{{ label }}</button>
        {% endfor %}
```

y después de la fila `Cupón` de la descripción:

```html
            {% if meta.tc_inicial is defined %}<dt>TC inicial</dt><dd>{{ num(meta.tc_inicial, 4) }}</dd>{% endif %}
```

- [ ] **Step 5: Verlos pasar + los guardianes del popup**

Run: `py -3.12 -m pytest tests/test_bond_detail_leg_dl.py tests/test_bond_detail_leg_tf.py tests/test_bond_detail.py tests/test_price_alias.py tests/test_pricing_equivalence.py -q --no-header -p no:cacheprovider`
Expected: PASS. Si `test_router_renderiza_la_pata_dl_y_los_botones` deja rastro (fallo antes del `finally`), verificar que `TMVE8` no quedó en la base del sandbox: `test_pricing_equivalence` es el que lo delataría.

- [ ] **Step 6: Commit**

```powershell
git add core/domain/services.py apps/web/bond_detail.py apps/web/templates/fragments/bond_detail.html tests/test_bond_detail_leg_dl.py
git commit -F <archivo con el mensaje>
```

Mensaje: `Popup DUAL_DL_TAMAR: patas _TAM y _DL, payback proyectado con FX, TC inicial y botones de riel` + trailer.

---

### Task 4: ABM (hoja TAMAR) y prefill desde Novedades

**Files:**
- Modify: `apps/web/instruments_abm.py:287-312` (hoja TAMAR), `:805-809` (validación en `save_instrument`)
- Modify: `apps/web/templates/fragments/abm_form.html:51` (nota de tipos analíticos)
- Modify: `core/infrastructure/byma/universe.py:287-297` (regex), `:335-340` (`prefill_for`)
- Create: `tests/test_abm_dual_dl_tamar.py`

**Interfaces:**
- Consumes: `Instrument.fx_base` (Task 1), tipo analítico (Task 2), `save_instrument(sheet, fields, cashflows=None)`, `get_instrument(ticker)`, `preview_cashflows(fields, sheet)`, `prefill_for(key)`.
- Produces: form key `tc_inicial` en la hoja TAMAR; `prefill_for` devuelve `{"sheet": "TAMAR", ...}` para títulos públicos `TM/TT/TX`+letra.

- [ ] **Step 1: Tests (fallan)**

Crear `tests/test_abm_dual_dl_tamar.py`:

```python
"""Alta de un DUAL_DL_TAMAR por la hoja TAMAR del ABM (fila ancla, `tc_inicial` en
raw_fields → `fx_base`), rechazo sin TC inicial, preview vacío (analítico) y prefill desde
Novedades para títulos públicos TM/TT/TX+letra. Spec 2026-09-08 §5."""
from __future__ import annotations

from datetime import date

import pytest

from apps.web.instruments_abm import (
    SHEET_SCHEMAS, get_instrument, preview_cashflows, save_instrument,
)
from config.settings import settings
from core.infrastructure.db import engine as db_engine
from core.infrastructure.db.catalog_repository import CatalogRepository, init_db
from core.infrastructure.db.engine import SessionLocal
from core.infrastructure.db.models import BymaCatalogORM, CashflowORM, InstrumentORM

_FIELDS = {
    "ticker_ars": "TMVE8", "tipo": "DUAL_DL_TAMAR", "fecha_emision": "2026-07-31",
    "fecha_vencimiento": "2028-01-31", "base calculo": "30/360", "spread": "0",
    "tc_inicial": "1300.5",
}


@pytest.fixture
def tmp_catalog(tmp_path):
    """Base temporal VACÍA (no combinar con TestClient(app): ver test_perf_W1_cashflows_ancla)."""
    db_engine.configure(tmp_path / "dual_dl.db")
    init_db()
    try:
        yield
    finally:
        db_engine.configure(settings.catalog_db)


def test_la_hoja_tamar_ofrece_el_tipo_y_el_tc_inicial():
    tamar = SHEET_SCHEMAS["TAMAR"]
    tipo = next(f for f in tamar["fields"] if f["key"] == "tipo")
    assert "DUAL_DL_TAMAR" in tipo["options"]
    tc = next(f for f in tamar["fields"] if f["key"] == "tc_inicial")
    assert tc["type"] == "number" and "DUAL_DL_TAMAR" in tc["help"]


def test_el_alta_guarda_solo_el_ancla_y_el_motor_ve_fx_base(tmp_catalog):
    res = save_instrument("TAMAR", dict(_FIELDS), cashflows=None)
    assert res["action"] == "created" and res["ticker"] == "TMVE8"
    with SessionLocal() as s:
        orm = s.get(InstrumentORM, "TMVE8")
        cfs = s.query(CashflowORM).filter_by(ticker="TMVE8").all()
    assert orm.instrument_type == "DUAL_DL_TAMAR" and orm.sheet == "TAMAR"
    assert float(orm.raw_fields["tc_inicial"]) == 1300.5 and orm.day_count == "30/360"
    assert [(c.fecha_pago, c.amortizacion, c.es_ancla) for c in cfs] == [(date(2028, 1, 31), 0.0, True)]
    inst = CatalogRepository(auto_seed=False).get_instrument_by_ticker("TMVE8")
    assert inst is not None and inst.fx_base == 1300.5 and inst.cashflows == ()
    # round-trip del form
    form = get_instrument("TMVE8")
    assert form["sheet"] == "TAMAR" and float(form["fields"]["tc_inicial"]) == 1300.5
    assert form["fields"]["tipo"] == "DUAL_DL_TAMAR" and form["cashflows_source"] == "analitico"


def test_sin_tc_inicial_se_rechaza_con_el_motivo(tmp_catalog):
    for vacio in ("", "0", None):
        campos = {**_FIELDS, "tc_inicial": vacio}
        with pytest.raises(ValueError, match="TC INICIAL"):
            save_instrument("TAMAR", campos, cashflows=None)
    with SessionLocal() as s:
        assert s.get(InstrumentORM, "TMVE8") is None


def test_el_preview_no_propone_schedule_porque_es_analitico():
    out = preview_cashflows(dict(_FIELDS), "TAMAR")
    assert out["cashflows"] == [] and "fórmula cerrada" in out["nota"]


def test_un_schedule_del_form_se_descarta_y_queda_el_ancla(tmp_catalog):
    save_instrument("TAMAR", dict(_FIELDS),
                    cashflows=[{"date": "2028-01-31", "amortization": 150.0, "interest": 0.0}])
    with SessionLocal() as s:
        cfs = s.query(CashflowORM).filter_by(ticker="TMVE8").all()
    assert len(cfs) == 1 and cfs[0].es_ancla


def _fila_universo(symbol, categoria="Títulos Públicos", vencimiento=None):
    return BymaCatalogORM(symbol=symbol, ticker_pesos=symbol, moneda="ARS", cotiza=1,
                          clase_liquidacion="primary", categoria=categoria,
                          security_type="GO", emisor="Gobierno Nacional",
                          vencimiento=vencimiento)


def test_prefill_manda_tm_tt_tx_mas_letra_a_la_hoja_tamar_sin_elegir_tipo(tmp_catalog):
    from core.infrastructure.byma.universe import prefill_for
    with SessionLocal.begin() as s:
        s.add_all([_fila_universo("TMVE8", vencimiento="2028-01-31"),
                   _fila_universo("TTJ26"), _fila_universo("TXMJ8"),
                   _fila_universo("TO26"), _fila_universo("TY30P"),
                   _fila_universo("T15E7"), _fila_universo("S29E7")])
    for sym in ("TMVE8", "TTJ26", "TXMJ8"):
        p = prefill_for(sym)
        assert p is not None and p["sheet"] == "TAMAR", sym
        assert "tipo" not in p["fields"] and p["fields"]["ticker_ars"] == sym
    assert prefill_for("TMVE8")["fields"]["fecha_vencimiento"] == "2028-01-31"
    assert prefill_for("TO26")["sheet"] != "TAMAR"        # BONOFIJA: TO + digito
    assert prefill_for("TY30P")["sheet"] != "TAMAR"       # TY no es TM/TT/TX
    assert prefill_for("T15E7")["sheet"] == "Tasa_Fija"   # BONCAP: T + digito, como antes
    assert prefill_for("S29E7")["sheet"] == "Tasa_Fija"
```

- [ ] **Step 2: Verlos fallar**

Run: `py -3.12 -m pytest tests/test_abm_dual_dl_tamar.py -q --no-header -p no:cacheprovider`
Expected: FAIL (la hoja no tiene `tc_inicial`; el prefill manda TMVE8 a la hoja de ON).

- [ ] **Step 3: Hoja TAMAR + validación**

`apps/web/instruments_abm.py`, hoja `"TAMAR"`:

```python
    "TAMAR": {
        "label": "TAMAR (PURO / DUAL / DUAL_CER_TAMAR / DUAL_DL_TAMAR)",
        "fields": [
            ...
            {"key": "tipo",            "label": "Tipo",                 "type": "select", "required": True,
             "options": ["PURO", "DUAL", "DUAL_CER_TAMAR", "DUAL_DL_TAMAR"]},
            ...
            {"key": "cer_spread",      "label": "Spread CER (decimal)", "type": "number",
             "step": "0.0001", "help": "Solo DUAL_CER_TAMAR"},
            {"key": "tc_inicial",      "label": "TC inicial (pesos/USD)", "type": "number",
             "step": "0.0001",
             "help": "Solo DUAL_DL_TAMAR — tipo de cambio inicial del prospecto; denominador del riel dólar-linked"},
        ],
    },
```

En `save_instrument`, después del `raise ValueError(... necesita fecha de VENCIMIENTO ...)` (línea 805-809):

```python
        if itype == "DUAL_DL_TAMAR" and not (inst.fx_base and inst.fx_base > 0):
            raise ValueError(
                f"{primary}: un DUAL_DL_TAMAR necesita el TC INICIAL (pesos/USD) del prospecto: "
                f"es el denominador del riel dólar-linked y sin él el bono no se preciaría. "
                f"Completá «TC inicial» y guardá de nuevo.")
```

Y en el docstring de `save_instrument` sumar `DUAL_DL_TAMAR` a la lista de tipos analíticos. En `apps/web/templates/fragments/abm_form.html:51` la nota pasa a decir `DUAL / DUAL_CER_TAMAR / DUAL_DL_TAMAR`.

- [ ] **Step 4: Prefill**

`core/infrastructure/byma/universe.py`, junto a `_LETRA_BONCAP` (línea 288):

```python
# Títulos públicos de la familia TAMAR por prefijo: TM (TMF27, TMVE8), TT (TTJ26), TX (TXMJ8)
# seguidos de LETRA. T+dígito es BONCAP y va antes; TO26/TY30P (BONOFIJA) no matchean.
_TAMAR_PREFIJO = re.compile(r"^T[MTX][A-Z]")
```

y en `prefill_for`, el bloque de títulos públicos:

```python
    if categoria == "Títulos Públicos":
        ticker = (fields.get("ticker_ars") or key or "").upper()
        clase = _clase_letra(ticker)
        if clase:
            sheet = "Tasa_Fija"
            fields["clase"] = clase
        elif _TAMAR_PREFIJO.match(ticker):
            sheet = "TAMAR"          # el tipo (PURO/DUAL/…) lo elige el operador: el prefijo no lo distingue
```

Actualizar el docstring de `prefill_for` con la regla nueva (una línea).

- [ ] **Step 5: Verlos pasar + vecinos**

Run: `py -3.12 -m pytest tests/test_abm_dual_dl_tamar.py tests/test_byma_universe.py tests/test_abm_router.py tests/test_perf_W1_cashflows_ancla.py tests/test_abm_novedades.py -q --no-header -p no:cacheprovider`
Expected: PASS.

- [ ] **Step 6: Commit**

```powershell
git add apps/web/instruments_abm.py apps/web/templates/fragments/abm_form.html core/infrastructure/byma/universe.py tests/test_abm_dual_dl_tamar.py
git commit -F <archivo con el mensaje>
```

Mensaje: `ABM: DUAL_DL_TAMAR en la hoja TAMAR con TC inicial obligatorio; Novedades prefill TM/TT/TX+letra a la hoja TAMAR` + trailer.

---

### Task 5: Gate completo y cierre

**Files:** ninguno nuevo. Verificación de punta a punta.

- [ ] **Step 1: Gate**

Run: `pwsh scripts/check.ps1`
Expected: `=== GATE VERDE ===` (ruff limpio; skips esperados: 3 de `tzset` en Windows, +5 «requiere bash» si el harness no tiene bash).

- [ ] **Step 2: Smoke de la ruta nueva**

Invocar `/smoke /bond/TMVE8_DL/detail` — sin cookie se espera **302** a `/login` (la ruta existe; si TMVE8 no está cargado en la base local el 404 recién aparece logueado).

- [ ] **Step 3: Cierre**

Sin commit propio. Reportar: gate, smoke, y que `tc_inicial` de TMVE8 lo carga David desde el prospecto (spec §7).
