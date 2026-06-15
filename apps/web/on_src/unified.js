/* ============================================================
   Herramienta unificada ON — Sector › Emisor › Título en UNA tabla con
   agregados por grupo. Reemplaza "Liga de sectores" (agregado por sector) +
   "Heatmap de emisores" (agregado por emisor) + drill-down (por título):
   los tres niveles, colapsables, en una sola herramienta.

   Consume window.ON / ON.DATA (util.js debe cargar antes). Namespace global Unified.

   API:
     Unified.render(mountEl, { onRender, defaultCcy })   // monta toolbar + tabla
     Unified.visibleBonds()      -> bonos visibles (post-filtro, moneda activa)
     Unified.sectorAggs()        -> [{key,label,short,color,icon,count,tir_avg,md_avg,n_ar,n_ext,vol}]
     Unified.highlight(key)      -> resalta un sector (atenúa el resto); null = limpia
     Unified.on(evt, cb)         -> 'render'(={bonds,aggs}) | 'sector'(key) | 'bond'(tk) | 'hover'(tk)
   `onRender(bonds, aggs)` se llama tras cada (re)render → el host sincroniza su gráfico.
   ============================================================ */
(function () {
  "use strict";
  var ON = window.ON;

  // columnas de datos (el ticker va en la 1ª columna de grupo/etiqueta)
  // De Clase en adelante todo centrado. `num` = fuente monoespaciada (tabular).
  var COLS = [
    { k: "clase",      label: "Clase",      align: "center" },
    { k: "ley",        label: "Ley",        align: "center" },
    { k: "tipo",       label: "Tipo",       align: "center" },
    { k: "emision",    label: "Emisión",    align: "center", num: true },
    { k: "vto",        label: "Vto",        align: "center", num: true },
    { k: "cupon",      label: "Cupón",      align: "center", num: true },
    { k: "frec",       label: "Frec.",      align: "center" },
    { k: "dias_cupon", label: "Días cupón", align: "center", num: true },
    { k: "price",      label: "Precio",     align: "center", num: true },
    { k: "paridad",    label: "Paridad",    align: "center", num: true },
    { k: "tir",        label: "TIR",        align: "center", num: true },
    { k: "cy",         label: "CY",         align: "center", num: true },
    { k: "md",         label: "MD",         align: "center", num: true },
    { k: "convex",     label: "Convex.",    align: "center", num: true },
    { k: "change_pct", label: "%Día",       align: "center", num: true },
    { k: "volume",     label: "Vol",        align: "center", num: true },
  ];
  var FREC = { 1: "Anual", 2: "Semestral", 3: "Cuatrim.", 4: "Trimestral", 6: "Bimestral", 12: "Mensual" };

  var state = {
    ccy: "MEP", q: "", tipo: { HD: true, DL: true },
    sortKey: "tir", sortDir: -1,
    colSec: new Set(), colEmi: new Set(),
    highlight: null,
  };
  var mountEl = null, hostOpts = {}, listeners = {};

  function emit(evt, p) { (listeners[evt] || []).forEach(function (cb) { cb(p); }); }

  // ---- agregados / agrupación ----
  function agg(bonds) {
    return {
      count: bonds.length,
      tir_avg: ON.avg(bonds.map(function (b) { return b.tir; })),
      md_avg: ON.avg(bonds.map(function (b) { return b.md; })),
      n_ar: bonds.filter(function (b) { return b.ley === "AR"; }).length,
      n_ext: bonds.filter(function (b) { return b.ley === "EXT"; }).length,
      vol: ON.sum(bonds.map(function (b) { return b.volume || 0; })),
    };
  }

  function visible() {
    var q = state.q.trim().toUpperCase();
    // Si el host provee bondsFn (ej. el sidebar de facetas global de /on), usamos ESA
    // lista ya filtrada y no aplicamos el ccy/tipo interno (los maneja el host). Si no,
    // filtramos internamente por moneda + tipo (modo standalone de los mockups).
    var src = hostOpts.bondsFn
      ? (hostOpts.bondsFn() || [])
      : ON.DATA.bonds.filter(function (b) {
          return b.ccy === state.ccy && !(b.tipo && !state.tipo[b.tipo]);
        });
    if (!q) return src;
    return src.filter(function (b) {
      return (b.ticker + " " + (b.emisor || "") + " " + (b.clase || "")).toUpperCase().indexOf(q) >= 0;
    });
  }

  function tree(bonds) {                       // sector -> emisor -> [bonos]
    var m = new Map();
    bonds.forEach(function (b) {
      var sk = b.sector || "Otros", ek = b.emisor || "—";
      if (!m.has(sk)) m.set(sk, new Map());
      var em = m.get(sk);
      if (!em.has(ek)) em.set(ek, []);
      em.get(ek).push(b);
    });
    return m;
  }

  function sectorAggs() {
    var t = tree(visible()), out = [];
    function push(sk) {
      var all = []; t.get(sk).forEach(function (arr) { all = all.concat(arr); });
      var m = ON.sectorMeta(sk), a = agg(all);
      a.key = sk; a.label = sk; a.short = m.short; a.color = m.color; a.icon = m.icon;
      out.push(a);
    }
    ON.SECTORS.forEach(function (s) { if (t.has(s.key)) push(s.key); });
    t.forEach(function (_v, sk) { if (!out.some(function (o) { return o.key === sk; })) push(sk); });
    out.sort(function (a, b) { return (b.tir_avg || 0) - (a.tir_avg || 0); });  // liga: TIR prom desc
    return out;
  }

  function sortBonds(bonds) {
    var k = state.sortKey, d = state.sortDir;
    return bonds.slice().sort(function (a, b) {
      var x = a[k], y = b[k];
      if (x == null && y == null) return 0;
      if (x == null) return 1; if (y == null) return -1;
      if (typeof x === "string") return d * x.localeCompare(y);
      return d * (x - y);
    });
  }

  // ---- formato de celdas ----
  function cell(k, b) {
    var v = b[k];
    if (k === "clase") return b.clase ? '<span class="uni-cl">' + ON.esc(b.clase) + '</span>' : '<span class="dim">—</span>';
    if (k === "ley") return '<span class="uni-ley ' + b.ley + '">' + b.ley + '</span>';
    if (k === "tipo") return b.tipo ? '<span class="uni-tipo ' + b.tipo + '">' + b.tipo + '</span>' : '—';
    if (k === "emision") return ON.date(b.emision);
    if (k === "vto") return ON.date(b.vto);
    if (k === "cupon") return v != null ? ON.pct(v) : '<span class="dim">—</span>';
    if (k === "frec") return v != null ? (FREC[v] || (v + "×/año")) : "—";
    if (k === "dias_cupon") return v != null ? v : "—";
    if (k === "price") return ON.num(v);
    if (k === "paridad") return ON.pct(v, 1);
    if (k === "tir") return ON.pct(v);
    if (k === "cy") return ON.pct(v);
    if (k === "md") return v != null ? ON.num(v) : "—";
    if (k === "convex") return v != null ? ON.num(v) : "—";
    if (k === "change_pct") return '<span class="' + ON.sign(v) + '">' + ON.pctSigned(v) + '</span>';
    if (k === "volume") return ON.vol(v);
    return v == null ? "—" : v;
  }
  function bar(v, max, color) {
    var w = (max > 0 && v != null) ? Math.max(3, Math.min(100, v / max * 100)) : 0;
    return '<span class="uni-barwrap"><span class="uni-bar" style="width:' + w + '%;background:' + color + '"></span></span>';
  }
  // celdas de agregado alineadas a COLS (centradas). `show` = qué métricas mostrar:
  // sector = {tir, md, vol, bar}; emisor = {vol} (sin promedio de TIR ni MD — sólo rótulo).
  function aggCells(a, max, color, show) {
    var out = "";
    COLS.forEach(function (c) {
      if (show.tir && c.k === "tir") out += '<td class="num tir-cell" style="text-align:center">' + (show.bar ? bar(a.tir_avg, max, color) : "") + '<b>' + ON.pct(a.tir_avg) + '</b></td>';
      else if (show.md && c.k === "md") out += '<td class="num" style="text-align:center">' + (a.md_avg != null ? ON.num(a.md_avg) + '<span class="faint">a</span>' : "—") + '</td>';
      else if (show.vol && c.k === "volume") out += '<td class="num dim" style="text-align:center">' + ON.vol(a.vol) + '</td>';
      else out += "<td></td>";
    });
    return out;
  }

  // ---- render ----
  function render() {
    if (!mountEl) return;
    var bonds = visible(), t = tree(bonds), aggs = sectorAggs();
    var max = Math.max.apply(null, aggs.map(function (a) { return a.tir_avg || 0; }).concat([1]));

    var h = '<table class="uni"><thead><tr><th class="uni-grp-h uni-sort" data-k="ticker">Sector › Emisor › Título</th>';
    COLS.forEach(function (c) {
      var srt = c.k === state.sortKey ? (state.sortDir > 0 ? " sorted-asc" : " sorted-desc") : "";
      h += '<th data-k="' + c.k + '" class="uni-sort' + srt + '" style="text-align:' + c.align + '">' + c.label + '</th>';
    });
    h += '</tr></thead><tbody>';

    aggs.forEach(function (a) {
      var col = a.color, cS = state.colSec.has(a.key);
      var dim = state.highlight && state.highlight !== a.key;
      var label = '<span class="uni-caret">' + (cS ? "▶" : "▼") + '</span><span class="uni-ic">' + a.icon + '</span>' +
        '<b style="color:' + col + '">' + a.short + '</b><span class="uni-n">' + a.count + '</span>' +
        '<span class="uni-leysplit">' + (a.n_ar ? '<span class="ar">AR ' + a.n_ar + '</span>' : '') +
        (a.n_ext ? '<span class="ext">EXT ' + a.n_ext + '</span>' : '') + '</span>';
      var tr = '<tr class="uni-sector' + (cS ? "" : " open") + (dim ? " dimmed" : "") + '" data-sec="' + encodeURIComponent(a.key) + '" style="--sc:' + col + '">';
      if (cS) {
        // colapsado: etiqueta a la izquierda + agregados (vista overview)
        h += tr + '<td class="uni-grp">' + label + '</td>' +
          aggCells(a, max, col, { tir: true, md: true, vol: true, bar: true }) + '</tr>';
      } else {
        // desplegado: banner a TODO el ancho con el nombre centrado en la mitad del listado
        h += tr + '<td class="uni-grp uni-grp-banner" colspan="' + (COLS.length + 1) + '">' + label + '</td></tr>';
      }
      if (cS) return;

      var emis = Array.from(t.get(a.key).entries()).map(function (e) { return { name: e[0], bonds: e[1], a: agg(e[1]) }; });
      emis.sort(function (x, y) { return (y.a.tir_avg || 0) - (x.a.tir_avg || 0); });
      emis.forEach(function (e) {
        var ek = a.key + "||" + e.name, cE = state.colEmi.has(ek);
        h += '<tr class="uni-emisor" data-emi="' + encodeURIComponent(ek) + '" style="--sc:' + col + '">' +
          '<td class="uni-grp uni-grp2"><span class="uni-caret">' + (cE ? "▶" : "▼") + '</span>' +
          '<span class="uni-em" title="' + ON.esc(e.name) + '">' + ON.esc(e.name) + '</span><span class="uni-n">' + e.a.count + '</span></td>' +
          aggCells(e.a, max, col, { vol: true }) + '</tr>';
        if (cE) return;
        sortBonds(e.bonds).forEach(function (b) {
          h += '<tr class="uni-bond" data-tk="' + b.ticker + '" style="--sc:' + col + '">' +
            '<td class="uni-grp uni-grp3"><span class="uni-tk">' + ON.esc(b.ticker) + '</span></td>';
          COLS.forEach(function (c) {
            h += '<td class="' + (c.num ? "num" : "") + (c.k === "tir" ? " tir-cell" : "") +
              '" style="text-align:' + c.align + '">' + cell(c.k, b) + '</td>';
          });
          h += '</tr>';
        });
      });
    });
    h += '</tbody></table>';
    mountEl.innerHTML = h;
    wire();
    emit("render", { bonds: bonds, aggs: aggs });
    if (hostOpts.onRender) hostOpts.onRender(bonds, aggs);
  }

  function wire() {
    mountEl.querySelectorAll("th.uni-sort").forEach(function (th) {
      th.onclick = function () {
        var k = th.dataset.k;
        if (state.sortKey === k) state.sortDir = -state.sortDir;
        else { state.sortKey = k; state.sortDir = (k === "ticker" || k === "clase") ? 1 : -1; }
        render();
      };
    });
    mountEl.querySelectorAll("tr.uni-sector").forEach(function (tr) {
      tr.onclick = function () {
        var k = decodeURIComponent(tr.dataset.sec);
        if (state.colSec.has(k)) state.colSec.delete(k); else state.colSec.add(k);
        render(); emit("sector", k);
      };
    });
    mountEl.querySelectorAll("tr.uni-emisor").forEach(function (tr) {
      tr.onclick = function (ev) {
        ev.stopPropagation();
        var k = decodeURIComponent(tr.dataset.emi);
        if (state.colEmi.has(k)) state.colEmi.delete(k); else state.colEmi.add(k);
        render();
      };
    });
    mountEl.querySelectorAll("tr.uni-bond").forEach(function (tr) {
      tr.onclick = function () { emit("bond", tr.dataset.tk); };
      tr.onmouseenter = function () { emit("hover", tr.dataset.tk); };
    });
  }

  function toolbar() {
    var el = document.createElement("div");
    el.className = "uni-toolbar";
    var h = '<input class="uni-search" type="search" placeholder="Buscar ticker / emisor / clase…">';
    if (!hostOpts.bondsFn) {                  // standalone: el tool maneja moneda + tipo
      h += '<div class="uni-seg" data-grp="ccy">' + ["ARS", "MEP", "CABLE"].map(function (c) {
        return '<button data-v="' + c + '"' + (c === state.ccy ? ' class="on"' : '') + '>' + c + '</button>';
      }).join("") + '</div>';
      h += '<div class="uni-seg" data-grp="tipo">' + ["HD", "DL"].map(function (c) {
        return '<button data-v="' + c + '" class="' + (state.tipo[c] ? "on" : "") + '">' + c + '</button>';
      }).join("") + '</div>';
    }
    h += '<button class="uni-btn" data-act="expand">Expandir</button>' +
      '<button class="uni-btn" data-act="collapse">Colapsar sectores</button>';
    var actions = hostOpts.actions || [];
    if (actions.length) {
      h += '<span style="margin-left:auto"></span>';
      actions.forEach(function (a, i) { h += '<button class="uni-btn uni-action" data-action="' + i + '">' + a.label + '</button>'; });
    }
    el.innerHTML = h;
    el.querySelector(".uni-search").oninput = function () { state.q = this.value; render(); };
    el.querySelectorAll('[data-grp="ccy"] button').forEach(function (b) {
      b.onclick = function () { state.ccy = b.dataset.v; el.querySelectorAll('[data-grp="ccy"] button').forEach(function (x) { x.classList.toggle("on", x === b); }); render(); };
    });
    el.querySelectorAll('[data-grp="tipo"] button').forEach(function (b) {
      b.onclick = function () {
        state.tipo[b.dataset.v] = !state.tipo[b.dataset.v];
        if (!state.tipo.HD && !state.tipo.DL) state.tipo[b.dataset.v] = true;
        b.classList.toggle("on", state.tipo[b.dataset.v]); render();
      };
    });
    el.querySelector('[data-act="expand"]').onclick = function () { state.colSec.clear(); state.colEmi.clear(); render(); };
    el.querySelector('[data-act="collapse"]').onclick = function () { sectorAggs().forEach(function (a) { state.colSec.add(a.key); }); render(); };
    el.querySelectorAll(".uni-action").forEach(function (b) {
      b.onclick = function () { var a = actions[+b.dataset.action]; if (a && a.onClick) a.onClick(); };
    });
    return el;
  }

  window.Unified = {
    render: function (mount, options) {
      hostOpts = options || {};
      if (hostOpts.defaultCcy) state.ccy = hostOpts.defaultCcy;
      mount.classList.add("uni-wrap");
      var body = document.createElement("div"); body.className = "uni-body";
      mount.innerHTML = ""; mount.appendChild(toolbar()); mount.appendChild(body);
      mountEl = body;
      render();
    },
    visibleBonds: visible,
    sectorAggs: sectorAggs,
    highlight: function (k) { state.highlight = k; render(); },
    openSector: function (key) {            // expandir un sector y hacer scroll hasta él
      state.colSec.delete(key);
      render();
      var row = mountEl && mountEl.querySelector('tr.uni-sector[data-sec="' + encodeURIComponent(key) + '"]');
      if (row) row.scrollIntoView({ behavior: "smooth", block: "center" });
    },
    on: function (evt, cb) { (listeners[evt] = listeners[evt] || []).push(cb); },
    rerender: render,
    state: state,
  };
})();
