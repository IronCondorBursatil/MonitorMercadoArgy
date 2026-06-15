"""Genera una versión STANDALONE de un mockup ON: con los assets de `_shared/` embebidos
inline (theme.css como <style>, on_data/sectors/util/chart como <script>), para abrirlo
directo con doble clic o compartirlo como un único archivo, sin la galería ni `_shared/`.

A diferencia de build_on_mockup_single.py (que mete los 21 mockups en iframes), acá el mock
ES el documento principal, así que los assets se inyectan en su lugar (no hace falta base64).

Uso:
    py -3.12 scripts/build_on_mockup_one.py                       # default: 21 → mesa-on-3-vistas.html
    py -3.12 scripts/build_on_mockup_one.py 12-liga-sectores liga.html

Read-only sobre la fuente; solo escribe el archivo standalone en docs/mockups/.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "docs" / "mockups" / "on"
SHARED_JS = ["on_data.js", "sectors.js", "util.js", "chart.umd.min.js"]


def _safe_js(text: str) -> str:
    # un literal "</script>" dentro del JS cerraría el bloque inline: escaparlo a "<\/script"
    return re.sub(r"</script", r"<\\/script", text, flags=re.I)


def inline(slug: str, out_name: str) -> int:
    src = SRC / (slug + ".html")
    if not src.exists():
        raise SystemExit(f"No existe {src}")
    html = src.read_text(encoding="utf-8")

    theme = (SRC / "_shared" / "theme.css").read_text(encoding="utf-8")
    theme_tag = '<link rel="stylesheet" href="_shared/theme.css">'
    if theme_tag not in html:
        raise SystemExit(f"[{slug}] no encontré el <link theme.css>")
    # .replace() literal (NO regex): el contenido tiene '\' que romperían un template re.sub
    html = html.replace(theme_tag, "<style>\n" + theme + "\n</style>")

    for name in SHARED_JS:
        tag = '<script src="_shared/' + name + '"></script>'
        if tag not in html:
            raise SystemExit(f"[{slug}] no encontré el <script src=_shared/{name}>")
        js = _safe_js((SRC / "_shared" / name).read_text(encoding="utf-8"))
        html = html.replace(tag, "<script>\n" + js + "\n</script>")

    if "_shared/" in html:
        leftover = [ln for ln in html.splitlines() if "_shared/" in ln]
        raise SystemExit(f"[{slug}] quedaron refs a _shared: {leftover}")

    out = ROOT / "docs" / "mockups" / out_name
    out.write_text(html, encoding="utf-8")
    print(f"OK -> {out}")
    print(f"  1 archivo standalone, {out.stat().st_size / 1e6:.2f} MB · doble clic para abrirlo.")
    return 0


def main() -> int:
    slug = sys.argv[1] if len(sys.argv) > 1 else "21-mesa-3-vistas"
    out_name = sys.argv[2] if len(sys.argv) > 2 else "mesa-on-3-vistas.html"
    return inline(slug, out_name)


if __name__ == "__main__":
    raise SystemExit(main())
