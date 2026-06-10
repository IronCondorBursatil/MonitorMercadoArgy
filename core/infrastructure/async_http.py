"""ResilientClient: httpx.AsyncClient con pool + circuit breaker + semáforo por
host (deudas #5, #6). Reemplaza `requests.Session` de `_http.py`.

- Pool keep-alive compartido (un cliente para todo el proceso).
- Un CircuitBreaker y un Semaphore por host: un upstream caído abre su breaker
  sin afectar a los demás.
- Retry sólo en status transitorios / errores de conexión.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Dict, Optional

import httpx

from core.infrastructure._http_constants import TRANSIENT_HTTP_CODES as _TRANSIENT
from core.infrastructure.circuit_breaker import CircuitBreaker

logger = logging.getLogger(__name__)


class ResilientClient:
    def __init__(
        self,
        *,
        fail_max: int = 4,
        reset_timeout: float = 30.0,
        per_host_concurrency: int = 8,
        timeout: float = 4.0,
        transport: Optional[httpx.AsyncBaseTransport] = None,
        user_agent: str = "monitor/2.0",
    ):
        self._client = httpx.AsyncClient(
            limits=httpx.Limits(max_keepalive_connections=20, max_connections=100,
                                keepalive_expiry=30),
            timeout=httpx.Timeout(timeout, connect=2.0, pool=2.0),
            transport=transport or httpx.AsyncHTTPTransport(retries=1, verify=False),
            headers={"User-Agent": user_agent},
        )
        self._breakers: Dict[str, CircuitBreaker] = {}
        self._sems: Dict[str, asyncio.Semaphore] = {}
        self._fail_max = fail_max
        self._reset_timeout = reset_timeout
        self._per_host = per_host_concurrency

    def _host_guard(self, url: str):
        host = httpx.URL(url).host or url
        if host not in self._breakers:
            self._breakers[host] = CircuitBreaker(
                fail_max=self._fail_max, reset_timeout=self._reset_timeout, name=host)
            self._sems[host] = asyncio.Semaphore(self._per_host)
        return self._breakers[host], self._sems[host]

    async def get_json(self, url: str, *, source: Optional[str] = None, retries: int = 1,
                       headers: Optional[dict] = None, timeout: Optional[float] = None):
        """GET → JSON con breaker + semáforo + retry transitorio. Propaga
        CircuitOpenError si el breaker del host está abierto (caller usa stale)."""
        return await self._request_json("GET", url, retries=retries, headers=headers,
                                        timeout=timeout)

    async def post_json(self, url: str, *, json=None, source: Optional[str] = None,
                        retries: int = 1, headers: Optional[dict] = None,
                        timeout: Optional[float] = None):
        """POST (body JSON) → JSON con breaker + semáforo + retry transitorio.
        Mismo contrato que get_json (BYMA usa POST en casi todos sus endpoints)."""
        return await self._request_json("POST", url, json=json, retries=retries,
                                        headers=headers, timeout=timeout)

    async def _request_json(self, method: str, url: str, *, json=None, retries: int = 1,
                            headers: Optional[dict] = None, timeout: Optional[float] = None):
        breaker, sem = self._host_guard(url)
        async with breaker:
            async with sem:
                for attempt in range(retries + 1):
                    try:
                        resp = await self._client.request(
                            method, url, json=json, headers=headers, timeout=timeout)
                        resp.raise_for_status()
                        return resp.json()
                    except httpx.HTTPStatusError as e:
                        status = e.response.status_code
                        if attempt < retries and status in _TRANSIENT:
                            await asyncio.sleep(0.3 * (attempt + 1))
                            continue
                        raise
                    except httpx.HTTPError:
                        # error de conexión/timeout → retry
                        if attempt < retries:
                            await asyncio.sleep(0.3 * (attempt + 1))
                            continue
                        raise
                # inalcanzable: el for (retries>=0) siempre sale por return o raise.

    async def aclose(self) -> None:
        await self._client.aclose()
