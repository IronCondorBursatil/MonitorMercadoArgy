"""Auditoría D2 — el strategy builder valuaba las patas a un ask/bid que puede ser 0.

`drawPayoff`/`renderStrategy` (pages/options.html) tomaban `l.qty > 0 ? o.ask : o.bid`
SIN fallback. Una serie sin punta llega al cliente con bid/ask = 0.00 (chain.py) y
nada la filtra, así que el panel mostraba Costo 0,00 / Máx pérdida 0,00 / sin
break-evens y ganancia ilimitada —una estrategia que aparenta no poder perder—
mientras el bloque P(profit)/EV del MISMO panel (POST /options/analytics) usaba el
premio bien resuelto por `analytics._resolve_premium` (mid → last → 0.01).

Caso real medido: GGAL vto 'SE', preset `long_call` → GFGC7200SE con ask=0.00,
bid=150.00, last=210.00, mid=210.00.

El test corre la función JS de verdad (extraída del template) en node, y la compara
contra `_resolve_premium`, que es la regla canónica del servidor.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import pytest

from core.domain.options.analytics import _resolve_premium

_TPL = Path(__file__).resolve().parent.parent / "apps" / "web" / "templates" / "pages" / "options.html"

# (nombre, bid, ask, last, mid) — el 1º es el contrato real del incidente.
# OJO: los cinco primeros tienen mid == last, así que POR SÍ SOLOS no distinguen el
# ORDEN del fallback (borrar `if (o.mid > 0) return o.mid;` los dejaba a todos en
# verde). Los tres últimos rompen ese empate y pinean mid → last → 0.01.
_CONTRACTS = [
    ("GFGC7200SE (sin oferta)", 150.0, 0.0, 210.0, 210.0),
    ("sin demanda", 0.0, 95.0, 90.0, 90.0),
    ("dos puntas", 100.0, 110.0, 105.0, 105.0),
    ("sin puntas, con last", 0.0, 0.0, 42.0, 42.0),
    ("sin nada", 0.0, 0.0, 0.0, None),
    # mid ≠ last: el long (entra al ask=0) tiene que caer al MID (120), no al last.
    ("mid distinto de last", 100.0, 0.0, 80.0, 120.0),
    # sin mid (serie con una sola punta histórica): recién ahí manda el last.
    ("sin mid, con last", 0.0, 0.0, 55.0, None),
    # mid en 0 (no None): tampoco puede ganarle al last.
    ("mid cero, con last", 0.0, 0.0, 70.0, 0.0),
]


@dataclass
class _Item:
    bid: float
    ask: float
    last: float
    mid: Optional[float]


def _fn_source(name: str) -> str:
    """Código de la función `name` del <script> del template (llaves balanceadas)."""
    src = _TPL.read_text(encoding="utf-8")
    i = src.find("function " + name + "(")
    assert i >= 0, f"el template no define {name}()"
    depth, started = 0, False
    for j in range(i, len(src)):
        if src[j] == "{":
            depth, started = depth + 1, True
        elif src[j] == "}":
            depth -= 1
            if started and depth == 0:
                return src[i:j + 1]
    raise AssertionError(f"{name}(): llaves sin cerrar")


def test_el_cliente_usa_un_helper_de_premio_y_no_el_ask_pelado():
    """Ni el payoff ni la curva ni el `px` de la leg pueden leer ask/bid sin fallback."""
    src = _TPL.read_text(encoding="utf-8")
    assert "function optEntryPx(" in src, "falta el helper de premio en el cliente"
    assert "l.qty > 0 ? o.ask : o.bid" not in src, \
        "el payoff sigue valuando la pata al ask/bid pelado (puede ser 0)"
    assert "isLong ? o.ask : o.bid" not in src, \
        "el `px` que muestra la leg sigue siendo el ask/bid pelado"
    # las tres llamadas: px de la leg, costo/griegos y curva de PnL
    assert src.count("optEntryPx(") >= 4


@pytest.mark.skipif(shutil.which("node") is None, reason="node no disponible")
def test_optEntryPx_espeja_resolve_premium_del_servidor(tmp_path):
    harness = tmp_path / "premium.js"
    cases = [{"name": n, "o": {"bid": b, "ask": a, "last": la, "mid": m}, "qty": q}
             for (n, b, a, la, m) in _CONTRACTS for q in (1, -1)]
    harness.write_text(
        _fn_source("optEntryPx")
        + "\nconst cases = " + json.dumps(cases) + ";\n"
        + "console.log(JSON.stringify(cases.map(c => optEntryPx(c.o, c.qty))));\n",
        encoding="utf-8")
    out = subprocess.run(["node", str(harness)], capture_output=True, text=True,
                         timeout=60)
    assert out.returncode == 0, out.stderr
    got = json.loads(out.stdout.strip().splitlines()[-1])

    expected = [_resolve_premium(_Item(**c["o"]), c["qty"]) for c in cases]
    assert got == pytest.approx(expected), list(zip([c["name"] for c in cases],
                                                    got, expected))
    # el caso del incidente: long sobre la serie sin oferta NO entra a 0
    assert got[0] == 210.0

    # ── El ORDEN del fallback, explícito (mid → last → 0.01) ────────────────
    # `cases` va (contrato, qty=1), (contrato, qty=-1) por cada contrato.
    def _px(nombre, qty):
        return got[next(i for i, c in enumerate(cases)
                        if c["name"] == nombre and c["qty"] == qty)]

    # ask = 0 y mid(120) ≠ last(80): el long entra al MID. Si el fallback saltea el
    # mid, acá da 80 (y si respeta la punta primero, la short da 100).
    assert _px("mid distinto de last", 1) == 120.0, \
        "el fallback se saltea el mid y entra al last"
    assert _px("mid distinto de last", -1) == 100.0, \
        "la punta que SÍ existe (bid) tiene que ganarle a mid/last"
    # sin mid: recién ahí manda el last (si el orden fuera last→mid daría lo mismo,
    # pero combinado con el caso de arriba queda pineado el orden completo).
    assert _px("sin mid, con last", 1) == 55.0
    assert _px("mid cero, con last", 1) == 70.0, "un mid en 0 no puede ganarle al last"
    # sin punta, sin mid y sin last: nominal positivo (nunca 0 → nunca "gratis").
    assert _px("sin nada", 1) == 0.01


@pytest.mark.skipif(shutil.which("node") is None, reason="node no disponible")
def test_una_pata_sin_punta_ya_no_sale_gratis_en_el_payoff(tmp_path):
    """Reproduce el `long_call` del preset sobre GFGC7200SE: con el bug, el costo era
    0 y el PnL era ≥ 0 para todo S (ganancia ilimitada, pérdida imposible)."""
    o = {"ticker": "GFGC7200SE", "kind": "C", "strike": 7200.0,
         "bid": 150.0, "ask": 0.0, "last": 210.0, "mid": 210.0}
    harness = tmp_path / "payoff.js"
    harness.write_text(
        _fn_source("optEntryPx")
        + "\nconst o = " + json.dumps(o) + ", qty = 1, S = 6000;\n"
        + "const entry = optEntryPx(o, qty);\n"
        + "const intr = Math.max(0, S - o.strike);\n"
        + "console.log(JSON.stringify({cost: qty * entry, pnl: qty * (intr - entry)}));\n",
        encoding="utf-8")
    out = subprocess.run(["node", str(harness)], capture_output=True, text=True,
                         timeout=60)
    assert out.returncode == 0, out.stderr
    res = json.loads(out.stdout.strip().splitlines()[-1])
    assert res["cost"] == 210.0, "la pata seguía entrando a 0"
    assert res["pnl"] < 0, "con el subyacente lejos del strike la call TIENE que perder"


def test_la_leg_sin_punta_queda_marcada_en_la_ui():
    """Si el lado que se usa vale 0, el usuario tiene que ver que el premio salió de
    un fallback (mid/last) y no de la punta que muestra la chain."""
    src = _TPL.read_text(encoding="utf-8")
    assert "sin punta" in src, "la leg valuada por fallback no se marca en la UI"
