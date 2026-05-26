"""Shared HTTP helper with connection keep-alive + single-shot retry.

External market/reference data is critical to this project (no alternative
source for any of it), so a transient blip — a single dropped TCP packet,
a momentary 503 from BCRA — must not silently drop a refresh cycle. One
quick retry catches the common case while keeping worst-case extra latency
small (300ms with default backoff).

**Connection reuse**: a single module-level `requests.Session` with a
pooled `HTTPAdapter` keeps warm TCP+TLS connections alive across refresh
cycles. Without it, every endpoint paid a fresh TLS handshake every ~5s
(4 Data912 endpoints × 2 provider instances ≈ 8 handshakes/cycle), and on
a momentarily slow link a handshake would occasionally exceed the 4s
timeout — surfacing as `_ssl.c:993 handshake operation timed out`. Reusing
a warm connection removes the handshake from the hot path entirely.

The session is shared across threads. urllib3's connection pool is
thread-safe; we never mutate per-request session state (headers/verify are
passed per call or set once at construction), so concurrent use is safe.

Retry policy:
  - Transient = Timeout, ConnectionError, HTTPError in {408, 425, 429, 5xx}
  - Permanent = HTTPError 4xx (other), JSONDecodeError, ValueError
    → raised immediately, no retry
"""

import logging
import time

import requests
import urllib3
from requests.adapters import HTTPAdapter

logger = logging.getLogger(__name__)


_TRANSIENT_HTTP_CODES = frozenset({408, 425, 429, 500, 502, 503, 504})

# Upstreams (data912, BCRA, dolarapi) are plain public JSON APIs; we don't
# verify TLS certs (some have incomplete chains). Preserve the prior
# behavior of `ssl._create_unverified_context()` and silence the per-call
# InsecureRequestWarning that requests would otherwise emit.
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# pool_maxsize covers worst-case concurrency to a single host: up to 4
# Data912 endpoints fetched in parallel, times the main loop + BEI thread
# instances. 16 leaves headroom so urllib3 never has to discard a warm
# connection. pool_connections caches that many distinct host-pools.
_session = requests.Session()
_session.verify = False
_adapter = HTTPAdapter(pool_connections=16, pool_maxsize=16, max_retries=0)
_session.mount("https://", _adapter)
_session.mount("http://", _adapter)


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

    `source` is a short label included in retry-warning logs (e.g. "BCRA/CER")
    so operators can tell which upstream blipped without digging into URLs.
    """
    label = source or url
    attempts = retries + 1
    headers = {"User-Agent": user_agent}
    for attempt in range(attempts):
        try:
            resp = _session.get(url, headers=headers, timeout=timeout)
            resp.raise_for_status()
            return resp.json()
        except requests.exceptions.HTTPError as e:
            code = e.response.status_code if e.response is not None else None
            if code in _TRANSIENT_HTTP_CODES and attempt < attempts - 1:
                logger.warning(
                    "%s: HTTP %s, retrying in %.1fs (%d/%d)",
                    label, code, backoff_s, attempt + 1, attempts,
                )
                time.sleep(backoff_s)
                continue
            raise
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
            if attempt < attempts - 1:
                logger.warning(
                    "%s: %s, retrying in %.1fs (%d/%d)",
                    label, type(e).__name__, backoff_s, attempt + 1, attempts,
                )
                time.sleep(backoff_s)
                continue
            raise
