"""Mitigación del self-XSS del layout default (M3b / S4).

_read_default_layout devolvía el JSON crudo del archivo, inyectado con |safe en un
<script> de index.html. Un POST a /panels/layout con un string JSON que contenga
</script> rompía el contexto y ejecutaba script. El fix escapa < > & (y los
separadores de línea U+2028/9) a \\uXXXX — válidos en JSON, inertes en HTML."""

from __future__ import annotations

import json

from apps.web.routers import panels


def test_read_default_layout_escapes_script_breakout(tmp_path, monkeypatch):
    payload = {"hidden": ["</script><script>alert(1)</script>"]}
    f = tmp_path / "layout.json"
    f.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(panels, "_LAYOUT_FILE", str(f))

    out = panels._read_default_layout()

    assert "</script>" not in out, "no debe poder romper el contexto <script>"
    assert "<script>" not in out
    assert "\\u003c" in out                       # '<' quedó escapado
    assert json.loads(out) == payload             # sigue siendo el MISMO objeto


def test_read_default_layout_escapes_ampersand_and_separators(tmp_path, monkeypatch):
    payload = {"note": "a & b" + " " + "c" + " " + "d"}
    f = tmp_path / "layout.json"
    f.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(panels, "_LAYOUT_FILE", str(f))

    out = panels._read_default_layout()
    assert "\\u0026" in out and "\\u2028" in out and "\\u2029" in out
    assert json.loads(out) == payload


def test_read_default_layout_missing_or_invalid_returns_null(tmp_path, monkeypatch):
    monkeypatch.setattr(panels, "_LAYOUT_FILE", str(tmp_path / "nope.json"))
    assert panels._read_default_layout() == "null"

    bad = tmp_path / "bad.json"
    bad.write_text("{ truncated", encoding="utf-8")
    monkeypatch.setattr(panels, "_LAYOUT_FILE", str(bad))
    assert panels._read_default_layout() == "null"
