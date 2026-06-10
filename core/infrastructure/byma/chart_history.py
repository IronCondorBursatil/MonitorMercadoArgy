"""Cierres diarios de BYMA open vía el endpoint **chart** (TradingView-style).

Drop-in de `series_historicas.fetch_history` (misma firma) pero MUCHO mejor para
el backfill de `price_history`:

  GET /chart/historical-series/history?symbol=TICKER 24HS&resolution=D&from=&to=

devuelve OHLCV completo (`{s, t[], o[], h[], l[], c[], v[]}`) de un rango largo en
**UNA sola llamada**, sin token y sin el tope ~30pt/llamada del POST
`seriesHistoricas/especies` (que obliga a paginar en ventanas de 25d). Cubre las
patas D/C (MEP/CABLE) y las letras que Data912 `/historical/bonds` deja en `{}`.
Verificado en vivo: AL30D/GD30C = 246 ruedas (1 año) en un GET.

Formato del símbolo: BYMA exige el sufijo ` 24HS` (con espacio). Sin sufijo → 400;
`CI` → 400; `48HS` → 200 con 0 puntos. Las claves del store son los tickers exactos
(AL30, AL30D, AL30C) → se les añade el sufijo acá.
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Dict, Optional

import httpx

from core.infrastructure.byma.sources import BymaOpenSource

logger = logging.getLogger(__name__)

_URL = BymaOpenSource.BASE + "/chart/historical-series/history"
# Índices (MERVAL=M, BURCAP=G): mismo schema, endpoint y símbolo distintos (sin ' 24HS').
_INDEX_URL = BymaOpenSource.BASE + "/chart/index-historical-series/history"
_HEADERS = {
    "User-Agent": "Mozilla/5.0", "Accept": "application/json",
    "Content-Type": "application/json",
}


def _unix(d: dt.date) -> int:
    """Fecha → unix timestamp en segundos UTC (lo que pide el endpoint)."""
    return int(dt.datetime(d.year, d.month, d.day, tzinfo=dt.timezone.utc).timestamp())


def _request_closes(url: str, symbol: str, *, max_days: int, resolution: str,
                    client: Optional[httpx.Client]) -> Dict[dt.date, float]:
    """GET a un endpoint chart de BYMA → `{date: close}` parseando `{s,t[],c[]}`.
    Best-effort: {} ante error de red/parseo o status != 'ok'."""
    today = dt.date.today()
    since = today - dt.timedelta(days=max(1, max_days))
    own = client is None
    # verify=False: el host BYMA usa una cadena que httpx no valida out-of-the-box
    # (igual que BymaOpenSource / series_historicas). Data pública read-only.
    cli = client or httpx.Client(verify=False, timeout=40.0)
    try:
        r = cli.get(url, params={"symbol": symbol, "resolution": resolution,
                                 "from": _unix(since), "to": _unix(today)},
                    headers=_HEADERS)
        if r.status_code != 200:
            return {}
        payload = r.json()
    except Exception as e:  # noqa: BLE001 — transporte / 200 con cuerpo no-JSON
        logger.debug("byma chart %s falló: %s", symbol, e)
        return {}
    finally:
        if own:
            cli.close()

    if not isinstance(payload, dict) or payload.get("s") != "ok":
        return {}
    ts = payload.get("t") or []
    cs = payload.get("c") or []
    out: Dict[dt.date, float] = {}
    for t, c in zip(ts, cs):
        if c is None:
            continue
        try:
            d = dt.datetime.fromtimestamp(int(t), tz=dt.timezone.utc).date()
            cv = float(c)
        except (TypeError, ValueError, OSError, OverflowError):
            continue
        if cv > 0:
            out[d] = cv
    return out


def fetch_history(symbol: str, *, max_days: int = 400, resolution: str = "D",
                  client: Optional[httpx.Client] = None) -> Dict[dt.date, float]:
    """Cierres diarios `{date: close}` de `symbol` (hasta `max_days` atrás) vía el
    endpoint chart de BYMA open. Best-effort. `client` inyectable.

    Misma firma que `series_historicas.fetch_history` → intercambiable como `fetch`
    en `price_history.prime_from_byma_historico`."""
    sym = (symbol or "").upper().strip()
    if not sym:
        return {}
    chart_sym = sym if sym.endswith(" 24HS") else f"{sym} 24HS"
    return _request_closes(_URL, chart_sym, max_days=max_days, resolution=resolution,
                           client=client)


def fetch_index_history(code: str, *, max_days: int = 180, resolution: str = "D",
                        client: Optional[httpx.Client] = None) -> Dict[dt.date, float]:
    """Cierres diarios de un índice BYMA (M=S&P MERVAL, G=BURCAP) vía el endpoint
    index-historical-series. El símbolo es el CÓDIGO de letra (sin ' 24HS')."""
    c = (code or "").upper().strip()
    if not c:
        return {}
    return _request_closes(_INDEX_URL, c, max_days=max_days, resolution=resolution,
                           client=client)
