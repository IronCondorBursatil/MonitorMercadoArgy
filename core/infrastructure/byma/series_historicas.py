"""Series históricas de BYMA (open.bymadata.com.ar, free) — cierre diario por especie.

Complementa el priming de Data912 `/historical/bonds` (price_history.py): ese
endpoint devuelve {} para bopreales, letras, ON y las patas MEP/CABLE, así que sus
variaciones (Sem/1M/3M/YTD/1A) quedaban en '—' hasta que la acumulación del feed
las llenara de a una rueda por día. BYMA open SÍ las tiene (~1 año hacia atrás),
y por especie exacta (incluida la pata C/D), sin credenciales.

Endpoint: POST /bnown/seriesHistoricas/especies (mismo host/token que BymaOpenSource).
  body: {symbol, maturity, fromDate, toDate, homogeneous, period, marketType}
    - maturity: 1=CI · 2=24hs · 3=48hs   (los bonos/legs cotizan a 24hs; CI = fallback)
    - period  : 1=diaria
  cols JSON: fecha, PrecioAper, precioMaximo, precioMinimo, precioCierre, volumenNominal

LÍMITES del server (heredados de la referencia BYMADATA_EXCEL/series_historicas.py):
  - "wheel": ~30 puntos por llamada → se pagina en ventanas de ~25 días.
  - "years": la historia llega ~1 año hacia atrás → al tocar fondo, frena.
"""

from __future__ import annotations

import datetime as dt
import logging
import time
from typing import Dict, List, Optional, Tuple

import httpx

from core.infrastructure.byma.sources import BymaOpenSource

logger = logging.getLogger(__name__)

_BASE = BymaOpenSource.BASE + "/bnown/seriesHistoricas/especies"
_TOKEN = BymaOpenSource.APP_TOKEN          # clave pública de la app open
_WINDOW_DAYS = 25                          # ~17 ruedas, bajo el tope ~30 del server
_MATURITIES = ("2", "1")                   # 24hs primero, CI de fallback (letras/TP)
_EMPTY_STREAK_STOP = 6                     # ventanas vacías seguidas → cortar
_THROTTLE_S = 0.03                         # cortesía entre ventanas (no martillar BYMA)


def _headers() -> dict:
    return {
        "Token": _TOKEN, "Options": "technical-details",
        "Accept": "application/json", "Content-Type": "application/json",
        "Origin": "https://open.bymadata.com.ar",
        "Referer": "https://open.bymadata.com.ar/", "User-Agent": "Mozilla/5.0",
    }


def _post(client: httpx.Client, body: dict) -> Tuple[Optional[List[dict]], Optional[str]]:
    """POST una ventana; devuelve (filas, None) o (None, mensaje_de_error).

    Cualquier error de transporte (ConnectError/ReadTimeout) o de parseo del cuerpo
    200 se devuelve como (None, msg): el caller lo trata como corte best-effort y
    conserva las ventanas YA bajadas (no las tira)."""
    try:
        r = client.post(_BASE, json=body, headers=_headers())
        if r.status_code == 200:
            return (r.json().get("data") or []), None
        try:
            return None, r.json().get("message", r.text[:60])
        except Exception:  # noqa: BLE001 — cuerpo no-JSON
            return None, f"HTTP {r.status_code}"
    except Exception as e:  # noqa: BLE001 — transporte / 200 con cuerpo no-JSON
        return None, f"{type(e).__name__}: {e}"


def _fetch_at_maturity(client: httpx.Client, symbol: str, maturity: str,
                       since: dt.date, today: dt.date) -> Dict[dt.date, float]:
    """{date: cierre} de `symbol` al plazo `maturity`, paginando desde hoy hacia
    `since`. Ajusta la ventana ante 'wheel' y frena en el fondo ('years')."""
    out: Dict[dt.date, float] = {}
    win_to = today
    empty_streak = 0
    while win_to >= since:
        span = _WINDOW_DAYS
        data = None
        while True:                                    # ajusta la ventana ante 'wheel'
            win_from = max(since, win_to - dt.timedelta(days=span))
            body = {"symbol": symbol, "maturity": maturity,
                    "fromDate": win_from.isoformat(), "toDate": win_to.isoformat(),
                    "homogeneous": "1", "period": "1", "marketType": "MC"}
            data, err = _post(client, body)
            if err == "internal_historical_series_years":
                return out                             # fondo de la historia disponible
            if err and "wheel" in str(err) and span > 6:
                span //= 2
                continue                               # ventana muy grande → achicar
            if err:
                return out                             # otro error → cortar best-effort
            break
        n = 0
        for row in (data or []):
            f = str(row.get("fecha", ""))[:10]
            c = row.get("precioCierre")
            if not f or c is None:
                continue
            try:
                out[dt.date.fromisoformat(f)] = float(c)
                n += 1
            except (TypeError, ValueError):
                continue
        empty_streak = empty_streak + 1 if n == 0 else 0
        if empty_streak >= _EMPTY_STREAK_STOP:
            break
        win_to = win_from - dt.timedelta(days=1)
        if _THROTTLE_S:
            time.sleep(_THROTTLE_S)
    return out


def fetch_history(symbol: str, *, max_days: int = 400,
                  client: Optional[httpx.Client] = None) -> Dict[dt.date, float]:
    """Cierres diarios `{date: close}` de `symbol` (hasta `max_days` atrás).

    Prueba 24hs y, si no trae nada, CI (letras / títulos públicos que solo cotizan
    CI). Best-effort: devuelve {} ante cualquier error de red/parseo. `client`
    inyectable (tests / reuso de conexión)."""
    sym = (symbol or "").upper().strip()
    if not sym:
        return {}
    today = dt.date.today()
    since = today - dt.timedelta(days=max(1, max_days))
    own = client is None
    # verify=False: el host BYMA usa una cadena que httpx no valida out-of-the-box
    # (igual que BymaRealtimeSource). Es data pública read-only.
    cli = client or httpx.Client(verify=False, timeout=40.0)
    try:
        for mat in _MATURITIES:
            try:
                pts = _fetch_at_maturity(cli, sym, mat, since, today)
            except Exception as e:  # noqa: BLE001 — best-effort por plazo
                logger.debug("byma seriesHistoricas %s mat=%s falló: %s", sym, mat, e)
                pts = {}
            if pts:
                return pts
        return {}
    finally:
        if own:
            cli.close()
