"""Un `0`, un `≤0`, un vacío o un NaN que llega de una fuente externa es DATO AUSENTE.

Regla del repo (CLAUDE.md › Invariantes › Pricing; agents.md §0.1.14) que se redescubrió
tres veces antes de tener un nombre, cada vez con un bug distinto:

  · `provider_hub._apply_floor`: la fuente activa publica precio 0 para especies ilíquidas
    o esqueleto — "un 0 no es dato" y el floor Data912 lo pisa con un cierre real;
  · `fci_history.net_flow_series`: ArgentinaDatos publica `ccp = 0` cuando no trae el dato
    (45 % del corte); leerlo como circulación cero fabricaba suscripciones y rescates
    fantasma por el patrimonio ENTERO del fondo;
  · `letras_sync`: la API manda `tem: 0` cuando no tiene la tasa; persistirlo dejaba una
    LECAP "al 0 %" que después se leía como si el dato existiera.

Los tres bordes usan estas dos funciones (tests/test_missing.py lo fija por texto). Un
borde nuevo que reciba números de terceros también: no reescribir la regla a mano.
"""

from __future__ import annotations

import math
from typing import Any, Optional


def valor_o_none(x: Any) -> Optional[float]:
    """`float(x)` si `x` es un número finito y > 0; `None` en cualquier otro caso
    (None, cadena vacía, no numérico, NaN/inf, cero o negativo, contenedores)."""
    if x is None or isinstance(x, (list, tuple, set, dict)):
        return None
    try:
        v = float(x.strip()) if isinstance(x, str) else float(x)
    except (TypeError, ValueError):
        return None
    if math.isnan(v) or math.isinf(v) or v <= 0:
        return None
    return v


def es_dato_ausente(x: Any) -> bool:
    """True si `x` NO es un valor utilizable (ver `valor_o_none`)."""
    return valor_o_none(x) is None
