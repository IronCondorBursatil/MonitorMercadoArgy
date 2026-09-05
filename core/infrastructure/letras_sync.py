"""Plan de sincronización del catálogo de letras contra ArgentinaDatos.

PURO: no toca red ni base. Recibe el payload de la API y una foto del catálogo, y
devuelve QUÉ habría que hacer. Quien decide hacerlo es `scripts/sync_letras.py` (a
mano, dry-run por default) o el loop diario, y quien escribe es siempre
`instruments_abm.save_instrument`, con todos sus guards.

POR QUÉ ESTE MODULO EXISTE. `argentinadatos_provider.fetch_letras()` ya pegaba a
`/v1/finanzas/letras` desde hace tiempo, pero **no lo llamaba nadie**: cada LECAP
nueva que licita el Tesoro había que cargarla a mano por la ABM. Eso es el trabajo
repetitivo que esto saca del medio.

QUÉ TAN BUENA ES LA FUENTE (medido contra la API viva, 2026-09-04, 18 letras):

* `vpv` y `fechaVencimiento`: **excelentes**. De las 16 letras que ya estaban en el
  catálogo, las 16 coincidían al centavo. Y son justo los dos datos que precian una
  letra capitalizable, que paga todo junto al vencimiento en un único flujo.
* `fechaEmision` y `tem`: **pobres**. Vienen en 6 de 18; en el resto llegan `""` y
  `0`. Eso es dato AUSENTE, no "emitida en el año 0 al 0%" — el mismo error que en
  `fci_history` fabricaba suscripciones fantasma leyendo `ccp<=0` como circulación
  cero. Un `tem` en 0 no se persiste: se deja vacío.
* La lista incluye letras **ya vencidas** (S17A6 y S30A6 vencieron en abril y
  seguían en el payload en septiembre). "Está en la API" no implica "hay que darla
  de alta".

LAS TRES REGLAS DURAS, que salen de lo de arriba:

1. **Sólo altas.** Una letra que ya está en el catálogo NUNCA se pisa: sus datos
   salen de IAMC/BYMA y son más ricos que los de la API. Las diferencias se
   REPORTAN para que las mire una persona.
2. **Nunca se borra.** Que la API deje de listar una letra no significa que haya que
   sacarla (dejó de listar S12J6 y el catálogo la conserva, bien).
3. **Sólo con dato completo.** Sin `fechaEmision` no hay alta: la ABM la exige, no
   se puede deducir, y fabricarla sería meterle un dato inventado a la fuente de
   verdad. Esas letras se reportan para carga manual.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any, Dict, List, Optional

# El prefijo del ticker decide el tipo. Los dos pertenecen a `instrument_groups`
# (TASA_FIJA): un tipo de fantasía dejaría el bono invisible en todos los paneles.
_CLASE_POR_PREFIJO = {"S": "LECAP", "T": "BONCAP"}

# Rango sano del pago final por cada 100 de VN. Las letras vivas van de ~103 a ~161;
# los topes son holgados a propósito, sólo atajan un payload corrupto.
_VPV_MIN, _VPV_MAX = 50.0, 500.0

# Un vencimiento demasiado lejos es casi seguro un error de la fuente: el Tesoro no
# emite letras capitalizables a más de unos años.
_MAX_ANIOS = 8

# El catálogo usa esta base para TODAS las letras (verificado sobre las 20 filas).
_BASE_CALCULO = "ACT/365.25"

# Guard del payload entero, mismo criterio que el de ratings: si la fuente devuelve
# muchas menos letras vivas de las que ya tenemos, es un corte roto y no que se hayan
# extinguido. Debajo de este piso NO se decide nada sobre el catálogo.
_PISO_RELATIVO = 0.6
_MINIMO_PARA_APLICAR_EL_GUARD = 5


@dataclass
class Plan:
    """Qué haría la sincronización. Nada de esto se ejecutó todavía."""

    altas: List[dict] = field(default_factory=list)
    incompletas: List[dict] = field(default_factory=list)
    diferencias: List[dict] = field(default_factory=list)
    vencidas: List[dict] = field(default_factory=list)
    invalidas: List[dict] = field(default_factory=list)
    solo_en_catalogo: List[str] = field(default_factory=list)
    sin_cambios: int = 0
    rechazado: Optional[str] = None
    # Lo que REALMENTE se escribió. Vacío mientras el plan no se aplique — que es
    # el caso por default: `planificar` sola nunca toca la base.
    aplicadas: List[str] = field(default_factory=list)

    @property
    def hay_altas(self) -> bool:
        return bool(self.altas)

    def resumen(self) -> str:
        """Una línea, para un log de servidor. Si no cabe, nadie la mira."""
        if self.rechazado:
            return "letras: payload RECHAZADO (%s)" % self.rechazado
        return ("letras: %d alta(s), %d incompleta(s), %d diferencia(s), "
                "%d vencida(s), %d invalida(s), %d sin cambios"
                % (len(self.altas), len(self.incompletas), len(self.diferencias),
                   len(self.vencidas), len(self.invalidas), self.sin_cambios))


def _fecha(valor) -> Optional[date]:
    """`"2026-12-30"` → date, o None. Un `""` es dato ausente, no un error."""
    if not valor:
        return None
    try:
        return date.fromisoformat(str(valor).strip()[:10])
    except (ValueError, TypeError):
        return None


def _clase_de(ticker: str) -> Optional[str]:
    return _CLASE_POR_PREFIJO.get(ticker[:1].upper()) if ticker else None


def _motivo_invalida(ticker: str, vto: Optional[date], vpv, hoy: date) -> Optional[str]:
    """El motivo por el que la fila NO puede entrar al catálogo, o None si puede.

    Se valida en el BORDE: cada fila que pase termina siendo una fila de la fuente
    de verdad, y un bono con datos absurdos se precia igual —mal— sin avisar."""
    if not ticker:
        return "sin ticker"
    if _clase_de(ticker) is None:
        return ("el ticker %r no arranca con S (LECAP) ni T (BONCAP): no se le puede "
                "asignar un tipo sin inventarlo" % ticker)
    if vto is None:
        return "vencimiento ausente o no parseable"
    if vto.year - hoy.year > _MAX_ANIOS:
        return "vencimiento a más de %d años (%s)" % (_MAX_ANIOS, vto)
    try:
        v = float(vpv)
    except (TypeError, ValueError):
        return "vpv no numérico (%r)" % (vpv,)
    if not (_VPV_MIN <= v <= _VPV_MAX):
        return "vpv fuera de [%g, %g]: %g" % (_VPV_MIN, _VPV_MAX, v)
    return None


def _alta_desde(ticker: str, emision: date, vto: date, vpv: float,
                tem) -> Dict[str, Any]:
    """Los `fields` que espera `save_instrument(sheet="Tasa_Fija", ...)`, más el
    único flujo. El nombre de las claves es el del `SHEET_SCHEMAS` del ABM."""
    # `tem` en 0 es dato ausente: se deja vacío en vez de persistir una tasa de 0%,
    # que después se lee como si el dato existiera.
    try:
        tem_val = float(tem) or None
    except (TypeError, ValueError):
        tem_val = None
    return {
        "ticker": ticker,
        "ticker_ars": ticker,
        "clase": _clase_de(ticker),
        "fecha_emision": emision.isoformat(),
        "fecha_pago": vto.isoformat(),
        "tem_licit": tem_val,
        "base calculo": _BASE_CALCULO,
        "frecuencia pagos": 1,
        "pago": float(vpv),
        # Un solo flujo al vencimiento: una letra capitalizable no paga cupones. Va
        # todo en amortización — es lo que tienen las 20 letras del catálogo.
        #
        # Las claves son las que parsea `instruments_abm._parse_cashflows`
        # (`date`/`amortization`/`interest`, en inglés, porque vienen del form del
        # ABM), NO las de la tabla. Con los nombres de la tabla el parser devuelve
        # una lista vacía y `save_instrument` rechaza el alta por "sin flujo de
        # fondos" — que es exactamente lo que hizo en el primer intento.
        "cashflows": [{"date": vto.isoformat(),
                       "amortization": float(vpv),
                       "interest": 0.0}],
    }


def planificar(api_rows, catalogo: Dict[str, dict], *, hoy: date) -> Plan:
    """Compara el payload de la API con el catálogo y devuelve el plan.

    `catalogo` es `{ticker: {"vto", "emi", "pago"}}` con lo que ya hay en la base.
    NO se muta nada de lo que entra: el payload sale del cache del provider, que es
    compartido entre hilos.
    """
    plan = Plan()
    filas = list(api_rows or [])

    # Primera pasada: separar lo que ni siquiera es una fila usable.
    vivas = []
    for fila in filas:
        ticker = str((fila or {}).get("ticker") or "").strip().upper()
        vto = _fecha((fila or {}).get("fechaVencimiento"))
        motivo = _motivo_invalida(ticker, vto, (fila or {}).get("vpv"), hoy)
        if motivo:
            plan.invalidas.append({"ticker": ticker or "?", "motivo": motivo})
            continue
        if vto < hoy:                  # el DIA del vencimiento todavía cuenta
            plan.vencidas.append({"ticker": ticker, "vto": vto.isoformat()})
            continue
        vivas.append((ticker, vto, fila))

    # Guard del payload entero, ANTES de decidir nada. Un corte roto de la fuente no
    # puede convertirse en decisiones sobre la fuente de verdad.
    catalogo_vivo = [t for t, d in (catalogo or {}).items()
                     if (_fecha(d.get("vto")) or hoy) >= hoy]
    if (len(catalogo_vivo) >= _MINIMO_PARA_APLICAR_EL_GUARD
            and len(vivas) < _PISO_RELATIVO * len(catalogo_vivo)):
        plan.rechazado = ("la API devolvió %d letras vivas y el catálogo tiene %d: "
                          "debajo del %d%% se asume corte roto"
                          % (len(vivas), len(catalogo_vivo), _PISO_RELATIVO * 100))
        return plan

    for ticker, vto, fila in vivas:
        actual = (catalogo or {}).get(ticker)
        if actual is not None:
            # REGLA 1: lo que ya está no se pisa. Sólo se reporta lo que difiere.
            difs = []
            if (_fecha(actual.get("vto")) or vto) != vto:
                difs.append("vto %s -> %s" % (actual.get("vto"), vto))
            pago = actual.get("pago")
            vpv = float(fila.get("vpv"))
            if pago is not None and abs(float(pago) - vpv) > 0.01:
                difs.append("pago %.4f -> %.4f" % (float(pago), vpv))
            if difs:
                plan.diferencias.append({"ticker": ticker, "difs": difs})
            else:
                plan.sin_cambios += 1
            continue

        emision = _fecha(fila.get("fechaEmision"))
        if emision is None:
            # REGLA 3: sin emisión no hay alta. La ABM la exige, no se deduce, y
            # fabricarla sería inventarle un dato a la fuente de verdad.
            plan.incompletas.append({
                "ticker": ticker, "vto": vto.isoformat(),
                "falta": "fecha de emisión (la API la manda vacía)"})
            continue
        plan.altas.append(_alta_desde(ticker, emision, vto, float(fila["vpv"]),
                                      fila.get("tem")))

    # REGLA 2: nunca se borra. Sólo se deja constancia.
    en_api = {t for t, _, _ in vivas} | {v["ticker"] for v in plan.vencidas}
    plan.solo_en_catalogo = sorted(set(catalogo or {}) - en_api)
    return plan
