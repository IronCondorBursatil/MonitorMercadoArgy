"""E2 — fci.js escapa los campos de texto externos (CAFCI: f.fondo/f.soc/f.obj) y NO
tiene comillas tipográficas (que rompían el parse JS). Test estático sobre el archivo
(fci.js NO es auto-generado, se edita directo)."""

from pathlib import Path

_FCI_JS = Path(__file__).resolve().parent.parent / "apps" / "web" / "static" / "js" / "fci.js"


def test_fci_js_has_no_smart_quotes():
    """Comillas tipográficas (U+201C/U+201D/U+2018/U+2019) son un error de sintaxis JS:
    rompen todo el bundle. El archivo debe usar solo comillas ASCII rectas."""
    text = _FCI_JS.read_text(encoding="utf-8")
    smart = {"“", "”", "‘", "’"}
    offenders = [(i + 1, ln) for i, ln in enumerate(text.splitlines())
                 if any(ch in ln for ch in smart)]
    assert not offenders, offenders


def test_fci_js_defines_and_uses_esc():
    """esc() está definida y los campos de texto externos (nombre/gestora/objetivo) se
    inyectan a través de ella (no crudos en innerHTML)."""
    text = _FCI_JS.read_text(encoding="utf-8")
    assert "function esc(" in text
    # objetivo del fondo (texto libre de CAFCI) escapado
    assert "esc(f.obj)" in text
    # al menos un campo de nombre de fondo escapado en la app
    assert "esc(f.fondo)" in text or "esc(f.soc)" in text
