"""Sincronización del catálogo de letras contra ArgentinaDatos: lectura y escritura.

El QUÉ hacer lo decide `core/infrastructure/letras_sync.planificar`, que es puro y
está testeado aparte. Acá vive lo que toca el mundo: leer la foto del catálogo,
pedirle el payload al provider y —sólo si se lo pide explícitamente— aplicar las
altas por el camino de escritura de la ABM.

POR QUE LA ESCRITURA VA POR `save_instrument` Y NO POR SQL. Ese es el único borde
con los guards que protegen la fuente de verdad: valida que el `instrument_type`
pertenezca a `instrument_groups` (un tipo huérfano deja el bono invisible en TODOS
los paneles, que fue el bug original del catálogo), rechaza un tipo normal sin
flujos (que quedaría impriceable), y hace todo en una transacción. Escribir el INSERT
a mano sería empezar una segunda puerta de escritura que se desincroniza de la
primera.

POR QUE VIVE EN `apps/web` Y NO EN `core`. Porque necesita `instruments_abm`, que es
de la capa web. El planificador —lo que tiene reglas de negocio y merece tests— está
en `core` y no sabe que esto existe.
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Dict, Optional

from sqlalchemy import select

from core.domain.instrument_groups import TASA_FIJA
from core.infrastructure.db.engine import SessionLocal
from core.infrastructure.db.models import CashflowORM, InstrumentORM
from core.infrastructure.letras_sync import Plan, planificar

logger = logging.getLogger(__name__)
_audit = logging.getLogger("monitor.audit")

# Sólo los dos tipos que emite la API. `BONOFIJA` también está en TASA_FIJA pero no
# es una letra capitalizable (TO26, TY30P pagan cupones): la API no los lista y este
# módulo no tiene por qué opinar sobre ellos.
_TIPOS = tuple(t for t in TASA_FIJA if t in ("LECAP", "BONCAP"))


def foto_del_catalogo() -> Dict[str, dict]:
    """`{ticker: {"vto", "emi", "pago"}}` de las letras que ya están en la base.

    `pago` es la SUMA del schedule, no el primer flujo: si alguna vez una letra
    quedara con más de una fila, comparar sólo la primera daría una diferencia
    fantasma contra el `vpv`, que es el pago total."""
    foto: Dict[str, dict] = {}
    with SessionLocal() as s:
        filas = s.execute(
            select(InstrumentORM).where(InstrumentORM.instrument_type.in_(_TIPOS))
        ).scalars().all()
        for orm in filas:
            cfs = s.execute(
                select(CashflowORM).where(CashflowORM.ticker == orm.ticker)
            ).scalars().all()
            # El ancla (`es_ancla=1`) no es un pago: es la fila que hace auditable un
            # bono de payoff analítico. No aplica a letras, pero si alguna la tuviera,
            # sumarla daría un total falso.
            pago = sum((c.amortizacion or 0) + (c.cupon_interes or 0)
                       for c in cfs if not getattr(c, "es_ancla", 0))
            foto[orm.ticker.upper()] = {
                "vto": orm.maturity_date,
                "emi": orm.emission_date,
                "pago": pago if cfs else None,
            }
    return foto


def _iso(v) -> Optional[str]:
    return v.isoformat() if hasattr(v, "isoformat") else (str(v) if v else None)


def sincronizar(*, aplicar: bool = False, hoy: Optional[date] = None,
                payload=None) -> Plan:
    """Compara la API con el catálogo y devuelve el plan.

    `aplicar=False` (default) NO escribe nada: es el modo que corre el loop y el que
    corre el script sin `--apply`. Es deliberado que haya que pedir la escritura:
    esto le agrega filas a la fuente de verdad desde una fuente de terceros.
    """
    hoy = hoy or date.today()
    if payload is None:
        from core.infrastructure.argentinadatos_provider import get_provider
        payload = get_provider().fetch_letras()

    foto = {t: {"vto": _iso(d["vto"]), "emi": _iso(d["emi"]), "pago": d["pago"]}
            for t, d in foto_del_catalogo().items()}
    plan = planificar(payload, foto, hoy=hoy)

    if plan.rechazado:
        logger.warning("letras: %s", plan.rechazado)
        return plan
    if not aplicar or not plan.altas:
        return plan

    from apps.web.instruments_abm import save_instrument

    aplicadas = []
    for alta in plan.altas:
        campos = {k: v for k, v in alta.items() if k not in ("cashflows", "pago")}
        try:
            save_instrument("Tasa_Fija", campos, alta["cashflows"])
        except Exception as e:                       # noqa: BLE001
            # Una letra que no entra no puede tumbar a las demás: cada alta es
            # independiente y `save_instrument` es transaccional por bono.
            logger.warning("letras: no se pudo dar de alta %s: %s", alta["ticker"], e)
            alta["error"] = str(e)
            continue
        aplicadas.append(alta["ticker"])
        # `console=True` para que salga del proceso y llegue a journald: un alta
        # automática en la fuente de verdad tiene que dejar rastro donde se lo busca.
        _audit.info("letras action=alta ticker=%s clase=%s vto=%s pago=%.4f fuente=argentinadatos",
                    alta["ticker"], alta["clase"], alta["fecha_pago"], alta["pago"],
                    extra={"console": True})
    plan.aplicadas = aplicadas
    if aplicadas:
        # El repo cachea el catálogo en memoria: sin esto las altas no se ven hasta
        # el próximo arranque, aunque estén en la base.
        try:
            from apps.web.deps import get_repo
            get_repo().reload()
        except Exception:                            # noqa: BLE001
            logger.warning("letras: altas guardadas pero falló el reload del repo",
                           exc_info=True)
    return plan
