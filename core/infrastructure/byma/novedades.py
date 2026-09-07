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
