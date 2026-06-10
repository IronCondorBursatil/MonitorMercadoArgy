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
    """Ningún router debe embeber JSON en <script> con json.dumps pelado. Guard
    robusto (verificación adversarial): regex multiline sobre TODOS los .py de
    apps/web — cualquier clave `*_json` (la convención de los embeds con |safe)
    asignada con json.dumps, aunque el formatter parta la línea, es un offender."""
    import re
    from pathlib import Path

    # `"algo_json": json.dumps(` con whitespace/newlines arbitrarios en el medio.
    pattern = re.compile(r'"(\w*_json)"\s*:\s*json\.dumps\(')
    offenders = []
    for f in Path("apps/web").rglob("*.py"):
        src = f.read_text(encoding="utf-8")
        for m in pattern.finditer(src):
            line_no = src[:m.start()].count("\n") + 1
            offenders.append(f"{f}:{line_no}: {m.group(1)}")
    assert not offenders, (
        "embeds JSON con json.dumps pelado (usar apps.web.json_script.json_for_script):\n"
        + "\n".join(offenders))
