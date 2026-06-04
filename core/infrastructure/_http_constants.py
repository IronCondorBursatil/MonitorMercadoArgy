"""Constantes HTTP compartidas entre el cliente sync (`_http.py`) y el async
(`async_http.py` / `ResilientClient`).

Vive aparte a propósito: `_http.py` crea un `httpx.Client` a nivel de módulo (side
effect de import), así que `async_http.py` no puede importar la constante desde ahí
sin instanciar ese cliente. Este módulo no tiene side effects.
"""

from __future__ import annotations

# Códigos HTTP transitorios que ameritan retry (timeout, rate-limit, 5xx upstream).
# Fuente única — antes estaba duplicada en _http.py y async_http.py (riesgo de drift).
TRANSIENT_HTTP_CODES = frozenset({408, 425, 429, 500, 502, 503, 504})
