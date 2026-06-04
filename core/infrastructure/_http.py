"""Shared HTTP helper (sync) con keep-alive + retry single-shot.

Usa `httpx.Client` (pooled) — se migró desde `requests` para unificar el stack
HTTP con el resto de la reingeniería (la capa async usa `httpx.AsyncClient` en
`async_http.ResilientClient`). El path sync sigue acá porque los providers se
llaman sincrónicamente (ThreadPool del refresh, bond_detail, indices); el cutover
de los providers a `ResilientClient` async es un refactor posterior.

External market/reference data es crítico (sin fuente alternativa), así que un
blip transitorio — un 503 momentáneo de BCRA — no debe tirar un ciclo: un retry
rápido cubre el caso común con latencia extra chica (300ms).

`httpx.Client` es seguro para requests concurrentes desde múltiples threads (su
connection pool tiene locking). No mutamos estado per-request del cliente
(headers/timeout van por llamada), así que el uso concurrente es seguro.

Retry policy:
  - Transitorio = Timeout/ConnectError (RequestError) + HTTPStatusError en {408,425,429,5xx}
  - Permanente  = HTTPStatusError 4xx (otros), JSON inválido → se propaga sin retry
"""

import logging
import time

import httpx

from core.infrastructure._http_constants import TRANSIENT_HTTP_CODES as _TRANSIENT_HTTP_CODES

logger = logging.getLogger(__name__)

# Upstreams (data912, BCRA, dolarapi) son APIs JSON públicas con cadenas TLS a
# veces incompletas → verify=False (igual que el comportamiento previo).
# Pool: hasta 4 endpoints Data912 en paralelo × (main loop + BEI) → 16 con headroom.
_client = httpx.Client(
    verify=False,
    timeout=httpx.Timeout(10.0),
    limits=httpx.Limits(max_connections=16, max_keepalive_connections=16, keepalive_expiry=30),
    follow_redirects=True,
)


def http_get_json(
    url: str,
    *,
    timeout: float = 10.0,
    user_agent: str = "monitor/1.0",
    retries: int = 1,
    backoff_s: float = 0.3,
    source: str = "",
):
    """GET `url`, parse JSON, return Python object. Retries once on transient.

    `source` es una etiqueta corta para los logs de retry (e.g. "BCRA/CER")."""
    label = source or url
    attempts = retries + 1
    headers = {"User-Agent": user_agent}
    for attempt in range(attempts):
        try:
            resp = _client.get(url, headers=headers, timeout=timeout)
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPStatusError as e:
            code = e.response.status_code if e.response is not None else None
            if code in _TRANSIENT_HTTP_CODES and attempt < attempts - 1:
                logger.warning(
                    "%s: HTTP %s, retrying in %.1fs (%d/%d)",
                    label, code, backoff_s, attempt + 1, attempts,
                )
                time.sleep(backoff_s)
                continue
            raise
        except httpx.RequestError as e:
            if attempt < attempts - 1:
                logger.warning(
                    "%s: %s, retrying in %.1fs (%d/%d)",
                    label, type(e).__name__, backoff_s, attempt + 1, attempts,
                )
                time.sleep(backoff_s)
                continue
            raise
