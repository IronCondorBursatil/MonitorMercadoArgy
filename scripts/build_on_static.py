"""Genera apps/web/static/js/on.js (app cliente de la página /on) desde las fuentes
en `apps/web/on_src/` (sectors.js + util.js + unified.js + on_app.html).

La página real reusa el diseño de `apps/web/on_src/on_app.html` (el ex-mock de la
galería de diseño), pero alimentado por `/on/data` (no por un snapshot congelado) y
dentro del chrome de base.html (header/nav/tema reales). Este script arma on.js de
forma determinística:

  on.js = sectors.js  (window.ON_SECTORS / ON_SECTOR_MAP)
        + util.js     (librería ON, sin el initTheme() del final — el tema lo maneja base.html)
        + unified.js  (herramienta Sector›Emisor›Título)
        + IIFE de on_app.html (la app de 3 subpestañas), con dos cambios:
            · `on:themechange` → MutationObserver sobre documentElement[data-theme]
            · init directo → fetch('/on/data') y luego boot()

Read-only sobre las fuentes; solo escribe on.js. NO editar on.js a mano (invariante).
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "apps" / "web" / "on_src"
OUT = ROOT / "apps" / "web" / "static" / "js" / "on.js"

THEME_OLD = """  window.addEventListener("on:themechange", function () {
    cardBgCache = {};
    renderKPIs(filteredList());
    renderActiveTab();
  });"""

THEME_NEW = """  // tema: base.html togglea data-theme en <html> (sin custom event) → observar y rebuildear
  new MutationObserver(function () {
    if (!ON.DATA.bonds || !ON.DATA.bonds.length) return;
    cardBgCache = {};
    renderKPIs(filteredList());
    renderActiveTab();
  }).observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });"""

INIT_OLD = """  buildSectorFacet();
  wireSidebar();
  wireTabsAndControls();
  refresh();"""

INIT_NEW = """  function onBoot() { buildSectorFacet(); wireSidebar(); wireTabsAndControls(); refresh(); }
  function onFetch(then) {
    fetch("/on/data").then(function (r) { return r.json(); })
      .then(function (d) { Object.assign(ON.DATA, d); ON.syncSectors(d.sectors_meta); then(); })
      .catch(function () {
        var fn = document.getElementById("footer-note");
        if (fn) fn.textContent = "No se pudo cargar /on/data.";
      });
  }
  onFetch(onBoot);
  // Refresco vivo: re-fetch de /on/data ante cada ciclo del server (precios + ediciones
  // del ABM) y re-render de la pestaña activa; preserva filtros/orden/sectores.
  // `force` saltea el throttle (para el refresco al volver a la pestaña).
  var _onCooldown = false;
  function onLiveRefresh(force) {
    if (_onCooldown && !force) return;
    _onCooldown = true;
    setTimeout(function () { _onCooldown = false; }, 10000);
    onFetch(function () { if (ON.DATA.bonds && ON.DATA.bonds.length) refresh(); });
  }
  try { new EventSource("/stream").addEventListener("refresh", function () { onLiveRefresh(false); }); } catch (e) {}
  setInterval(function () { onLiveRefresh(false); }, 20000);
  // Volver a la pestaña / foco → refrescar YA: si editaste en el ABM y volvés a /on,
  // ves el cambio al instante (sin esperar el próximo ciclo). Dedupe 800ms.
  var _onFocusGuard = false;
  function onFocusRefresh() {
    if (_onFocusGuard) return;
    _onFocusGuard = true;
    setTimeout(function () { _onFocusGuard = false; }, 800);
    onLiveRefresh(true);
  }
  document.addEventListener("visibilitychange", function () { if (!document.hidden) onFocusRefresh(); });
  window.addEventListener("focus", onFocusRefresh);"""


def _require(haystack: str, needle: str, what: str) -> None:
    if needle not in haystack:
        raise SystemExit(f"build_on_static: no encontré {what} (¿cambió el mock 21?)")


def main() -> int:
    sectors = (SRC / "sectors.js").read_text(encoding="utf-8")
    util = (SRC / "util.js").read_text(encoding="utf-8")
    unified = (SRC / "unified.js").read_text(encoding="utf-8")
    html = (SRC / "on_app.html").read_text(encoding="utf-8")

    # util.js: sacar el initTheme() del final (el tema lo aplica base.html antes del paint).
    _require(util, "\n  initTheme();\n", "el initTheme() final de util.js")
    util = util.replace("\n  initTheme();\n", "\n", 1)

    # IIFE de la app = el último <script>...</script> SIN atributos de on_app.html.
    scripts = re.findall(r"<script>(.*?)</script>", html, re.S)
    if not scripts:
        raise SystemExit("build_on_static: no encontré el <script> de la app en on_app.html")
    app = max(scripts, key=len).strip()

    _require(app, THEME_OLD, "el handler on:themechange")
    app = app.replace(THEME_OLD, THEME_NEW, 1)
    _require(app, INIT_OLD, "el bloque de init")
    app = app.replace(INIT_OLD, INIT_NEW, 1)

    out = (
        "/* AUTO-GENERADO por scripts/build_on_static.py — NO editar a mano.\n"
        "   App cliente de /on (porteada de apps/web/on_src/on_app.html).\n"
        "   Datos en vivo desde /on/data; tema y chrome los da base.html. */\n\n"
        "/* ---- sectores (window.ON_SECTORS / ON_SECTOR_MAP) ---- */\n"
        + sectors.strip() + "\n\n"
        "/* ---- librería ON (de on_src/util.js, sin manejo de tema/header) ---- */\n"
        + util.strip() + "\n\n"
        "/* ---- herramienta unificada Sector›Emisor›Título (de on_src/unified.js) ---- */\n"
        + unified.strip() + "\n\n"
        "/* ---- app de la página (3 subpestañas), boot por fetch('/on/data') ---- */\n"
        + app + "\n"
    )
    OUT.write_text(out, encoding="utf-8")
    print(f"OK -> {OUT}  ({OUT.stat().st_size / 1000:.0f} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
