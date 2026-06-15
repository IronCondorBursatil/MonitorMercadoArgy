/* ON mockups — librería compartida. Namespace global `ON`.
   Datos reales en window.ON_DATA (on_data.js); sectores en window.ON_SECTORS (sectors.js).
   Diseñada para que los 20 mockups consuman la MISMA API y se vean consistentes. */
(function () {
  "use strict";
  var D = window.ON_DATA || { bonds: [], sectors: [], meta: {} };
  var SECTORS = window.ON_SECTORS || [];
  var SMAP = window.ON_SECTOR_MAP || {};

  // ---- acceso a datos -------------------------------------------------------
  function legs(ccy) {                       // bonos de una moneda (default MEP)
    ccy = ccy || "MEP";
    return D.bonds.filter(function (b) { return b.ccy === ccy; });
  }
  function byLey(list) {                      // {AR:[...], EXT:[...]}
    var o = { AR: [], EXT: [] };
    list.forEach(function (b) { (o[b.ley] || (o[b.ley] = [])).push(b); });
    return o;
  }
  function bySector(list) {                   // Map sector(orden canónico) -> [bonos]
    var m = new Map();
    SECTORS.forEach(function (s) { m.set(s.key, []); });
    list.forEach(function (b) {
      if (!m.has(b.sector)) m.set(b.sector, []);
      m.get(b.sector).push(b);
    });
    // descartar sectores vacíos preservando orden
    var out = new Map();
    m.forEach(function (v, k) { if (v.length) out.set(k, v); });
    return out;
  }
  function sectorColor(key) { return (SMAP[key] && SMAP[key].color) || "#8993B8"; }
  function sectorMeta(key) { return SMAP[key] || { key: key, short: key, color: "#8993B8", icon: "•" }; }

  // ---- formato --------------------------------------------------------------
  function pct(v, dec) { if (v == null || isNaN(v)) return "—"; return v.toFixed(dec == null ? 2 : dec) + "%"; }
  function pctSigned(v, dec) { if (v == null || isNaN(v)) return "—"; return (v >= 0 ? "+" : "") + v.toFixed(dec == null ? 2 : dec) + "%"; }
  function num(v, dec) { if (v == null || isNaN(v)) return "—"; return v.toLocaleString("es-AR", { minimumFractionDigits: dec == null ? 2 : dec, maximumFractionDigits: dec == null ? 2 : dec }); }
  function vol(v) {
    if (v == null || isNaN(v)) return "—";
    if (v >= 1e9) return (v / 1e9).toFixed(1) + "B";
    if (v >= 1e6) return (v / 1e6).toFixed(1) + "M";
    if (v >= 1e3) return (v / 1e3).toFixed(1) + "K";
    return String(Math.round(v));
  }
  function date(iso) {                        // "2026-06-30" -> "30/06/26"
    if (!iso) return "—";
    var p = iso.split("-");
    return p[2] + "/" + p[1] + "/" + p[0].slice(2);
  }
  function sign(v) { return v == null || isNaN(v) ? "" : (v >= 0 ? "pos" : "neg"); }

  // ---- escape HTML (anti-XSS) ----------------------------------------------
  // Usado para inyectar strings editables del ABM (emisor, clase, sector.short)
  // en innerHTML/title. Escapa los 5 caracteres peligrosos en HTML/atributos.
  function esc(s) {
    if (s == null) return "";
    return String(s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  // ---- estadística ----------------------------------------------------------
  function avg(arr) { var a = arr.filter(function (x) { return x != null && !isNaN(x); }); return a.length ? a.reduce(function (s, x) { return s + x; }, 0) / a.length : null; }
  function median(arr) { var a = arr.filter(function (x) { return x != null && !isNaN(x); }).sort(function (x, y) { return x - y; }); if (!a.length) return null; var m = Math.floor(a.length / 2); return a.length % 2 ? a[m] : (a[m - 1] + a[m]) / 2; }
  function sum(arr) { return arr.reduce(function (s, x) { return s + (x || 0); }, 0); }

  // ---- ajuste log: TIR = a + b·ln(MD) --------------------------------------
  function logFit(points) {                   // points: [{x,y}] con x=MD>0
    var pts = points.filter(function (p) { return p.x > 0 && p.y != null && !isNaN(p.y); });
    if (pts.length < 3) return null;
    var n = pts.length, sx = 0, sy = 0, sxx = 0, sxy = 0;
    pts.forEach(function (p) { var lx = Math.log(p.x); sx += lx; sy += p.y; sxx += lx * lx; sxy += lx * p.y; });
    var b = (n * sxy - sx * sy) / (n * sxx - sx * sx);
    var a = (sy - b * sx) / n;
    return {
      a: a, b: b,
      predict: function (x) { return a + b * Math.log(x); },
      curve: function (xmin, xmax, steps) {
        steps = steps || 40; var out = [];
        for (var i = 0; i <= steps; i++) { var x = xmin + (xmax - xmin) * i / steps; if (x <= 0) continue; out.push({ x: x, y: a + b * Math.log(x) }); }
        return out;
      },
    };
  }

  // ---- helpers de chart -----------------------------------------------------
  function cssVar(n) { return getComputedStyle(document.documentElement).getPropertyValue(n).trim(); }
  function chartTheme() {
    return {
      accent: cssVar("--accent") || "#3a5fcf",
      txt: cssVar("--text-dim") || "#4a5780",
      grid: cssVar("--panel-border") || "#d2d8e6",
      panel: cssVar("--panel-bg") || "#fff",
      pos: cssVar("--pos") || "#0d6e3a",
      neg: cssVar("--neg") || "#a8242b",
    };
  }
  // Opciones base para un scatter TIR(y) vs MD(x). Extender/override según el mockup.
  function baseScatterOpts(o) {
    o = o || {};
    var t = chartTheme();
    return {
      responsive: true, maintainAspectRatio: false,
      scales: {
        x: { title: { display: true, text: o.xLabel || "Modified Duration (años)", color: t.txt }, ticks: { color: t.txt }, grid: { color: t.grid } },
        y: { title: { display: true, text: o.yLabel || "TIR (%)", color: t.txt }, ticks: { color: t.txt }, grid: { color: t.grid } },
      },
      plugins: {
        legend: { labels: { color: t.txt, usePointStyle: true, boxWidth: 8 } },
        tooltip: { callbacks: { label: function (c) { var r = c.raw || {}; return (r.t ? r.t + " · " : "") + (r.e || "") + "  MD " + (r.x != null ? r.x.toFixed(2) : "?") + " · TIR " + (r.y != null ? r.y.toFixed(2) : "?") + "%"; } } },
      },
    };
  }
  // Convierte una lista de bonos a puntos de scatter TIR/MD (descarta sin MD/TIR).
  function scatterPoints(list) {
    return list.filter(function (b) { return b.md != null && b.md > 0 && b.tir != null; })
      .map(function (b) { return { x: b.md, y: b.tir, t: b.ticker, e: b.emisor, sector: b.sector, ley: b.ley, vol: b.volume, paridad: b.paridad }; });
  }

  // ---- tema (light/dark) ----------------------------------------------------
  function initTheme() { var t = localStorage.getItem("on_theme") || localStorage.getItem("theme"); if (t) document.documentElement.setAttribute("data-theme", t); }
  function toggleTheme() {
    var h = document.documentElement;
    var t = h.getAttribute("data-theme") === "dark" ? "light" : "dark";
    h.setAttribute("data-theme", t); localStorage.setItem("on_theme", t);
    window.dispatchEvent(new Event("on:themechange"));
  }

  // Hidrata SECTORS/SMAP desde el payload del server (ON.DATA.sectors_meta), que es
  // el espejo de la fuente Python on_classification.SECTORS — así cliente y SSR pintan
  // el mismo sector/color sin re-sincronizar a mano la copia horneada (sectors.js).
  // MUTA in-place (no reasigna): util captura SECTORS/SMAP por referencia en el init y
  // los exporta como ON.SECTORS; reasignar dejaría stale el closure. Si meta viene vacío
  // (offline / payload viejo) NO toca nada → conserva la copia horneada como fallback.
  function syncSectors(meta) {
    if (!meta || !meta.length) return;
    SECTORS.splice(0, SECTORS.length);
    Object.keys(SMAP).forEach(function (k) { delete SMAP[k]; });
    meta.forEach(function (s) { SECTORS.push(s); SMAP[s.key] = s; });
    window.ON_SECTORS = SECTORS; window.ON_SECTOR_MAP = SMAP;
  }

  window.ON = {
    DATA: D, SECTORS: SECTORS,
    legs: legs, byLey: byLey, bySector: bySector,
    sectorColor: sectorColor, sectorMeta: sectorMeta,
    esc: esc,
    pct: pct, pctSigned: pctSigned, num: num, vol: vol, date: date, sign: sign,
    avg: avg, median: median, sum: sum,
    logFit: logFit, cssVar: cssVar, chartTheme: chartTheme,
    baseScatterOpts: baseScatterOpts, scatterPoints: scatterPoints,
    initTheme: initTheme, toggleTheme: toggleTheme, syncSectors: syncSectors,
  };
  initTheme();
})();
