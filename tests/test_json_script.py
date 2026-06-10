"""F8 (review): json_for_script como helper COMPARTIDO — el fix XSS S4 quedaba a
media altura aplicado solo al layout; los embeds hermanos (panel_share, panel_chart,
futuros_share, options_chain, options_smile) usaban json.dumps pelado con |safe."""

from __future__ import annotations

import json

from apps.web.json_script import json_for_script


def test_script_breakout_neutralized():
    payload = {"x": "</script><script>alert(1)</script>"}
    out = json_for_script(payload)
    assert "</script" not in out and "<script" not in out
    assert json.loads(out) == payload          # mismo objeto al parsear


def test_ampersand_escaped_roundtrip():
    payload = {"name": "S&P Merval <test>"}
    out = json_for_script(payload)
    assert "&" not in out and "<" not in out and ">" not in out
    assert json.loads(out) == payload


def test_all_embed_sites_use_the_shared_helper():
    """Ningún router debe embeber JSON en <script> con json.dumps pelado: los 6
    sitios (chain/pts_call/pts_put/datasets×2/contracts) usan json_for_script."""
    from pathlib import Path

    routers = Path("apps/web/routers")
    offenders = []
    for f in ("options.py", "panels.py"):
        src = (routers / f).read_text(encoding="utf-8")
        for var in ("chain_json", "pts_call_json", "pts_put_json",
                    "datasets_json", "contracts_json"):
            for line in src.splitlines():
                if f'"{var}"' in line and "json.dumps(" in line:
                    offenders.append(f"{f}: {line.strip()}")
    assert not offenders, "embeds con json.dumps pelado:\n" + "\n".join(offenders)
