/* Filtro por columna del Universo BYMA (estilo blotter). Client-side, sobre las filas
   ya cargadas (con una categoría elegida el server trae TODO el set → filtra completo).
   Cada control vive en la fila `tr.uni-flt`; el índice de columna sale del cellIndex, así
   que la columna opcional "Legislación" no rompe el mapeo. Tres modos:
     · text   → la celda contiene el texto (case-insensitive)
     · select → igualdad exacta; las opciones se auto-pueblan con los valores distintos
     · num    → la celda (data-v cruda) es ≥ al valor ingresado
   Una celda puede exponer `data-f` (valor a filtrar) distinto del texto visible (ej. la
   columna Alta? muestra ✓/— pero filtra Sí/No). */
(function () {
  function dataRows(table) {
    return Array.prototype.filter.call(
      table.querySelectorAll("tbody tr"),
      function (tr) { return !tr.classList.contains("uni-more"); });
  }
  function cellText(cell) {
    if (!cell) return "";
    var t = (cell.dataset && cell.dataset.f != null) ? cell.dataset.f : cell.textContent;
    return (t || "").trim();
  }
  function colOf(ctrl) {
    var th = ctrl.closest("th,td");
    return th ? th.cellIndex : -1;
  }
  function rebuildSelects(table, fltRow) {
    fltRow.querySelectorAll('select[data-mode="select"]').forEach(function (sel) {
      var col = colOf(sel), cur = sel.value, seen = {};
      if (col < 0) return;
      dataRows(table).forEach(function (tr) {
        var t = cellText(tr.cells[col]);
        if (t && t !== "—") seen[t] = 1;
      });
      var opts = Object.keys(seen).sort();
      sel.innerHTML = '<option value="">▼</option>' + opts.map(function (v) {
        var e = v.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/"/g, "&quot;");
        return '<option value="' + e + '">' + e + "</option>";
      }).join("");
      if (opts.indexOf(cur) >= 0) sel.value = cur;   // preservar selección
    });
  }
  function apply(table, fltRow) {
    var filters = Array.prototype.map.call(
      fltRow.querySelectorAll("[data-mode]"), function (c) {
        return { col: colOf(c), mode: c.dataset.mode, val: c.value.trim() };
      }).filter(function (f) { return f.val !== "" && f.col >= 0; });
    var shown = 0;
    dataRows(table).forEach(function (tr) {
      var show = true;
      for (var i = 0; i < filters.length; i++) {
        var f = filters[i], cell = tr.cells[f.col];
        if (f.mode === "num") {
          var dv = parseFloat((cell && cell.dataset.v) || "0") || 0, need = parseFloat(f.val);
          if (!isNaN(need) && !(dv >= need)) { show = false; break; }
        } else if (f.mode === "select") {
          if (cellText(cell) !== f.val) { show = false; break; }
        } else {
          if (cellText(cell).toLowerCase().indexOf(f.val.toLowerCase()) < 0) { show = false; break; }
        }
      }
      tr.style.display = show ? "" : "none";
      if (show) shown++;
    });
    var meta = document.querySelector(".uni-fltcount");
    if (meta) meta.textContent = filters.length ? "· " + shown + " filtrados" : "";
  }
  // ---- Orden por columna de monto (clic en el título ARS/MEP/CABLE) ----
  function cellNum(tr, cls) {
    var c = tr.querySelector("." + cls);
    return c ? (parseFloat(c.dataset.v || "0") || 0) : 0;
  }
  function applySort(table) {
    var cls = table.dataset.sortCls;
    if (!cls) return;                       // sin orden activo → orden natural
    var dir = table.dataset.sortDir === "asc" ? 1 : -1;
    var tbody = table.querySelector("tbody");
    if (!tbody) return;
    var sentinel = tbody.querySelector(".uni-more");
    dataRows(table)
      .sort(function (a, b) { return (cellNum(a, cls) - cellNum(b, cls)) * dir; })
      .forEach(function (tr) { tbody.appendChild(tr); });
    if (sentinel) tbody.appendChild(sentinel);   // el centinela del scroll queda al final
  }
  function updateSortArrows(table) {
    table.querySelectorAll("th.uni-sortable").forEach(function (th) {
      var arr = th.querySelector(".uni-sortarr");
      if (arr) arr.textContent = th.dataset.sort === table.dataset.sortCls
        ? (table.dataset.sortDir === "asc" ? " ▲" : " ▼") : "";
    });
  }
  function wireSort(table) {
    table.querySelectorAll("th.uni-sortable").forEach(function (th) {
      if (th.dataset.sortWired) return;
      th.dataset.sortWired = "1";
      th.addEventListener("click", function () {
        if (table.dataset.sortCls === th.dataset.sort) {
          table.dataset.sortDir = table.dataset.sortDir === "asc" ? "desc" : "asc";
        } else {
          table.dataset.sortCls = th.dataset.sort;
          table.dataset.sortDir = "desc";    // 1er clic: mayor → menor
        }
        applySort(table);
        updateSortArrows(table);
      });
    });
  }
  // ---- Refresh manual (#uni-refresh): preservar filtros + orden ----
  // El botón ↻ reemplaza la tabla entera (montos frescos del hub) con el MISMO
  // q/cat → mismas columnas. Capturamos los valores del blotter y el orden antes
  // del swap y los restauramos en el init siguiente (solo si la forma coincide).
  var savedState = null;
  document.addEventListener("htmx:beforeRequest", function (e) {
    var src = e.detail && e.detail.elt;
    if (!src || src.id !== "uni-refresh") return;
    var table = document.querySelector(".uni-table");
    var fltRow = table && table.querySelector("tr.uni-flt");
    if (!fltRow) return;
    savedState = {
      vals: Array.prototype.map.call(
        fltRow.querySelectorAll("[data-mode]"), function (c) { return c.value; }),
      sortCls: table.dataset.sortCls || "",
      sortDir: table.dataset.sortDir || "",
    };
  });
  // Un ↻ fallido (timeout / non-2xx) no swapea #uni-results → nadie consumiría el
  // estado y el PRÓXIMO swap legítimo (cambio de categoría) lo restauraría sobre
  // un dataset distinto. Descartarlo apenas el request del botón falla.
  document.addEventListener("htmx:responseError", function (e) {
    var src = e.detail && e.detail.elt;
    if (src && src.id === "uni-refresh") savedState = null;
  });
  document.addEventListener("htmx:sendError", function (e) {
    var src = e.detail && e.detail.elt;
    if (src && src.id === "uni-refresh") savedState = null;
  });
  function restoreState(table, fltRow) {
    if (!savedState) return;
    var st = savedState;
    savedState = null;                     // se consume una sola vez
    var ctrls = fltRow.querySelectorAll("[data-mode]");
    if (ctrls.length !== st.vals.length) return;   // columnas distintas → no mapear
    Array.prototype.forEach.call(ctrls, function (c, i) { c.value = st.vals[i]; });
    if (st.sortCls) {
      table.dataset.sortCls = st.sortCls;
      table.dataset.sortDir = st.sortDir;
    }
  }
  function init(evt) {
    // Gate por target, AL TOPE: solo nos interesan los settles que tocan la tabla
    // (#uni-results: swap completo, lotes del scroll). Sin esto, CUALQUIER request
    // HTMX del documento (badge de salud cada 15s, cards del header, indices del
    // catálogo) re-escaneaba la tabla entera, reconstruía los selects y
    // re-appendeaba todas las filas (reflow) en idle — CPU proporcional al tamaño
    // de la tabla. El camino DOMContentLoaded llama init() SIN evt y sigue de largo.
    if (evt) {
      var tgt = evt.target;
      if (!tgt || !(tgt.id === "uni-results" || (tgt.closest && tgt.closest("#uni-results"))))
        return;
    }
    var table = document.querySelector(".uni-table");
    if (!table) return;
    var fltRow = table.querySelector("tr.uni-flt");
    if (!fltRow) return;
    if (!fltRow.dataset.wired) {
      fltRow.dataset.wired = "1";
      var run = function () { apply(table, fltRow); };
      fltRow.addEventListener("input", run);
      fltRow.addEventListener("change", run);
    }
    rebuildSelects(table, fltRow);
    restoreState(table, fltRow);   // tras un ↻: re-aplica blotter + orden previos
    apply(table, fltRow);
    wireSort(table);
    applySort(table);          // re-aplica el orden tras appends del scroll
    updateSortArrows(table);
  }
  // Se re-corre tras cada swap HTMX que toque #uni-results (el gate filtra el resto).
  document.addEventListener("htmx:afterSettle", init);
  // OJO: el wrapper es necesario — pasar `init` directo le daría el Event de
  // DOMContentLoaded (target=document) y el gate cortaría la carga inicial.
  document.addEventListener("DOMContentLoaded", function () { init(); });
})();
