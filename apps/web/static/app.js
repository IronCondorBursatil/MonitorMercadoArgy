"use strict";

const REFRESH_MS = 5 * 1000;

const MESES_ABBR = [
  "ene", "feb", "mar", "abr", "may", "jun",
  "jul", "ago", "sep", "oct", "nov", "dic",
];

// Valor de una CSS var del :root (tema activo). n = nombre, ej. "--text-faint".
const cssv = (n) => getComputedStyle(document.documentElement).getPropertyValue(n).trim();

// Columnas a esconder en el frontend (server las manda igual, pero
// el v4 aprobado las saca por redundancia con el ticker).
const DROP_KEYS_BY_MONITOR = {
  bonares:   new Set(["name", "maturity"]),
  bopreales: new Set(["name", "maturity"]),
};

// Columnas que arrancan ocultas por default pero el usuario puede activar
// desde el botón ▦. Solo se aplican la primera vez (si el usuario nunca
// tocó las columnas de ese monitor). Si el usuario hizo "Mostrar todas",
// respetamos su decisión en reloads posteriores.
const DEFAULT_HIDDEN_COLS_BY_MONITOR = {
  // tna_360 / tem_360 ocultas por default; usuario activa desde ▦.
  // Las 5 métricas avanzadas también ocultas por default.
  tasa_fija: ["tna_360", "tem_360", "dv01", "convexity", "tir_real", "carry_roll", "spread_curva"],
};

// Columnas con mini-barra (las que tienen signo).
const BAR_KINDS = new Set(["percent_signed", "scenario", "percent_bullet"]);
// Rich/cheap: color de punto según residuo vs la curva log ajustada.
const CURVE_CHEAP = "#1f9d6b"; // verde: sobre-rinde vs curva (barato)
const CURVE_RICH  = "#d64550"; // rojo: sub-rinde vs curva (caro)
const CURVE_RESID_BAND = 0.15; // pp de banda neutra alrededor de la curva

// Paleta para los charts (Chart.js). Theme-aware: light = idem style.css;
// dark = tonos claros para legibilidad sobre fondo oscuro. CHART se sincroniza
// con el tema activo vía syncChartPalette() (al iniciar y al togglear).
const CHART_LIGHT = {
  NAVY:        "#0a1d4a",
  NAVY_DARK:   "#06143b",
  ACCENT_BLUE: "#3a5fcf",
  BOPREAL:     "#1aa094",  // teal para 3ra serie BOPREALES
  TEXT_DIM:    "#4a5780",
  GRID:        "#e6ecf5",
  BORDER:      "#d2d8e6",
};
const CHART_DARK = {
  NAVY:        "#9db4ef",
  NAVY_DARK:   "#c9d4f0",
  ACCENT_BLUE: "#6486e6",
  BOPREAL:     "#3fb9ab",
  TEXT_DIM:    "#aab4d4",
  GRID:        "rgba(255, 255, 255, 0.10)",
  BORDER:      "#26345a",
};
const CHART = { ...CHART_LIGHT };
function syncChartPalette() {
  const dark = document.documentElement.getAttribute("data-theme") === "dark";
  Object.assign(CHART, dark ? CHART_DARK : CHART_LIGHT);
}
syncChartPalette();

// Curva soberana: SOLO bonos MEP (terminan en "D"). Decisión deliberada para
// evitar mezclar monedas — Pesos (sin sufijo) tendrían TIRs negativas absurdas
// por mismatch ARS-price / USD-cashflows, y CABLE (sufijo "C") es el mismo
// bono que el MEP con un spread mínimo que duplica puntos. Si en el futuro
// querés sumar CABLE, cambiar a /[CD]$/ o sumarlos por separado como serie.
const CURVA_TICKER_RE = /D$/i;
// Excepciones puntuales (D-suffix pero descartados por motivo específico):
const CURVA_EXCLUDED_TICKERS = new Set([
  "BPY6D",  // vto inminente, TIR distorsionada (-93%).
]);
const _isCurvaTicker = (t) =>
  CURVA_TICKER_RE.test(String(t)) && !CURVA_EXCLUDED_TICKERS.has(String(t).toUpperCase());

// Instancia del chart de curva (singleton; se actualiza en cada refresh).
let curvaChart = null;

// =====================================================================
// Formato
// =====================================================================

// Fechas/horas en huso horario de Buenos Aires (correcto sin importar el TZ
// del navegador). Devuelve las partes ya formateadas; el formato visual lo
// arma cada helper para preservar el look existente (DD-mmm-AA, HH:MM:SS).
const _AR_TZ = "America/Argentina/Buenos_Aires";
let _arPartsFmt = null;
function _arDateParts(d) {
  if (!_arPartsFmt) {
    _arPartsFmt = new Intl.DateTimeFormat("es-AR", {
      timeZone: _AR_TZ,
      year: "numeric", month: "2-digit", day: "2-digit",
      hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23",
    });
  }
  const out = {};
  for (const p of _arPartsFmt.formatToParts(d)) out[p.type] = p.value;
  return out;
}

const fmt = {
  number(v, dec = 2) {
    if (v === null || v === undefined || Number.isNaN(v)) return "–";
    return Number(v).toLocaleString("es-AR", {
      minimumFractionDigits: dec,
      maximumFractionDigits: dec,
    });
  },
  percent(v, dec = 2) {
    if (v === null || v === undefined || Number.isNaN(v)) return "–";
    return `${Number(v).toLocaleString("es-AR", {
      minimumFractionDigits: dec,
      maximumFractionDigits: dec,
    })}%`;
  },
  percentSigned(v, dec = 2) {
    if (v === null || v === undefined || Number.isNaN(v)) return "–";
    const n = Number(v);
    const sign = n > 0 ? "+" : "";
    return `${sign}${n.toLocaleString("es-AR", {
      minimumFractionDigits: dec,
      maximumFractionDigits: dec,
    })}%`;
  },
  volume(v) {
    if (v === null || v === undefined || Number.isNaN(v) || Number(v) === 0) return "–";
    const n = Number(v);
    if (n >= 1e9) return `${(n / 1e9).toFixed(2).replace(".", ",")} B`;
    if (n >= 1e6) return `${(n / 1e6).toFixed(1).replace(".", ",")} M`;
    if (n >= 1e3) return `${(n / 1e3).toFixed(0)} K`;
    return n.toFixed(0);
  },
  text(v) {
    if (v === null || v === undefined) return "–";
    return String(v);
  },
  // "30-abr-26" (en huso de Buenos Aires)
  dateV4(d) {
    const p = _arDateParts(d);
    return `${p.day}-${MESES_ABBR[Number(p.month) - 1]}-${p.year.slice(-2)}`;
  },
  // "29-10-27" — formato argentino DD-MM-AA (año a 2 dígitos). Acepta ISO
  // "YYYY-MM-DD" (lo que devuelve el backend) o un Date. Para cualquier otra
  // cosa, devuelve el string tal cual.
  dateAR(v) {
    if (v === null || v === undefined || v === "") return "–";
    const s = String(v);
    const m = s.match(/^(\d{4})-(\d{2})-(\d{2})/);
    if (m) return `${m[3]}-${m[2]}-${m[1].slice(-2)}`;
    return s;
  },
  // "14:32:18" (en huso de Buenos Aires)
  timeHMS(d) {
    const p = _arDateParts(d);
    return `${p.hour}:${p.minute}:${p.second}`;
  },
};

// =====================================================================
// Render de celdas
// =====================================================================

function columnMaxAbs(rows, key) {
  let max = 0;
  for (const row of rows) {
    const v = row[key];
    if (v === null || v === undefined) continue;
    const n = Number(v);
    if (Number.isFinite(n)) {
      const a = Math.abs(n);
      if (a > max) max = a;
    }
  }
  return max || 1;
}

function renderCell(col, value, ctx) {
  const td = document.createElement("td");

  if (value === null || value === undefined) {
    td.textContent = "–";
    td.classList.add("empty");
    if (col.kind === "text" || col.kind === "date") td.classList.add("col-text");
    if (col.key === "ticker") td.classList.add("ticker");
    return td;
  }

  switch (col.kind) {
    case "text":
      td.classList.add("col-text");
      td.textContent = fmt.text(value);
      if (col.key === "ticker") {
        td.classList.add("ticker");
        if (ctx.stockChart) {
          const t = String(value).toUpperCase();
          td.classList.add("stock-ticker-clickable");
          td.setAttribute("data-stock-ticker", t);
          td.title = `Ver gráfico de ${value}`;
        } else if (!ctx.noDetail) {
          const t = String(value).toUpperCase();
          td.classList.add("ticker-clickable");
          td.setAttribute("data-ticker", t);
          td.title = `Ver detalle de ${value}`;
        }
      }
      return td;

    case "date":
      td.classList.add("col-text");
      td.textContent = fmt.dateAR(value);
      return td;

    case "number":
      td.textContent = fmt.number(value, col.decimals ?? 2);
      if (col.key === "price") td.classList.add("price");
      return td;

    case "volume":
      td.textContent = fmt.volume(value);
      return td;

    case "percent":
      td.textContent = fmt.percent(value, col.decimals ?? 2);
      return td;

    case "percent_signed":
    case "scenario": {
      const dec = col.decimals ?? (col.kind === "scenario" ? 1 : 2);
      const n = Number(value);
      const text = fmt.percentSigned(n, dec);

      td.classList.add("has-bar");
      if (col.kind === "scenario") td.classList.add("scenario");
      if (n > 0)      td.classList.add("pos");
      else if (n < 0) td.classList.add("neg");

      const maxAbs = ctx.maxAbsByCol.get(col.key) || 1;
      const ratio = Math.min(Math.abs(n) / maxAbs, 1);
      const pct = (ratio * 100).toFixed(1);

      const track = document.createElement("div");
      track.className = "bar-track";
      const fill = document.createElement("div");
      fill.className = "bar-fill";
      fill.style.width = `${pct}%`;
      track.appendChild(fill);

      const span = document.createElement("span");
      span.className = "cell-text";
      span.textContent = text;

      td.appendChild(track);
      td.appendChild(span);
      return td;
    }

    case "sparkline_range": {
      // Range sparkline: thin line of the last N closes over a faint band
      // showing the min-max extent of the period. A dot marks the most
      // recent close. Line color reflects period direction (close[-1] vs
      // close[0]) — green up, red down.
      const arr = Array.isArray(value)
        ? value.map(Number).filter(Number.isFinite)
        : [];
      if (arr.length < 2) {
        td.textContent = "–";
        td.classList.add("empty");
        return td;
      }

      const W = 92, H = 22, PAD = 2;
      const min = Math.min(...arr);
      const max = Math.max(...arr);
      const span = max - min || 1;
      const xAt = (i) => PAD + (i / (arr.length - 1)) * (W - 2 * PAD);
      const yAt = (v) => H - PAD - ((v - min) / span) * (H - 2 * PAD);

      let d = "";
      for (let i = 0; i < arr.length; i++) {
        d += (i === 0 ? "M" : "L") + xAt(i).toFixed(1) + " " + yAt(arr[i]).toFixed(1);
      }
      const up = arr[arr.length - 1] >= arr[0];
      const color = up ? "#1aa094" : "#d04848";

      // Min / max y-coords define a faint horizontal band so the "range"
      // is visible at a glance even when the line is mostly flat.
      const yMin = yAt(min);
      const yMax = yAt(max);
      const bandY = Math.min(yMin, yMax);
      const bandH = Math.abs(yMax - yMin);

      const lx = xAt(arr.length - 1);
      const ly = yAt(arr[arr.length - 1]);

      td.classList.add("sparkline-cell");
      td.innerHTML =
        `<svg xmlns="http://www.w3.org/2000/svg" width="${W}" height="${H}" class="sparkline" ` +
        `role="img" aria-label="Evolución últimos ${arr.length} cierres">` +
          `<rect x="${PAD}" y="${bandY.toFixed(1)}" width="${W - 2 * PAD}" ` +
            `height="${Math.max(bandH, 1).toFixed(1)}" fill="${color}" opacity="0.10"/>` +
          `<path d="${d}" fill="none" stroke="${color}" stroke-width="1.3" ` +
            `stroke-linejoin="round" stroke-linecap="round"/>` +
          `<circle cx="${lx.toFixed(1)}" cy="${ly.toFixed(1)}" r="2" fill="${color}"/>` +
        `</svg>`;
      return td;
    }

    case "percent_bullet": {
      // Bullet chart: barra centrada con eje en 0. Positivos extienden a
      // la derecha, negativos a la izquierda — magnitud y signo legibles
      // de un vistazo sin tener que leer el número.
      const dec = col.decimals ?? 2;
      const n = Number(value);
      const text = fmt.percentSigned(n, dec);

      td.classList.add("bullet");
      if (n > 0)      td.classList.add("pos");
      else if (n < 0) td.classList.add("neg");

      const maxAbs = ctx.maxAbsByCol.get(col.key) || 1;
      const halfPct = (Math.min(Math.abs(n) / maxAbs, 1) * 50).toFixed(2);

      const track = document.createElement("div");
      track.className = "bullet-track";

      const axis = document.createElement("div");
      axis.className = "bullet-axis";
      track.appendChild(axis);

      const fill = document.createElement("div");
      fill.className = "bullet-fill";
      if (n >= 0) {
        fill.style.left = "50%";
        fill.style.width = `${halfPct}%`;
      } else {
        fill.style.right = "50%";
        fill.style.width = `${halfPct}%`;
      }
      track.appendChild(fill);

      const span = document.createElement("span");
      span.className = "cell-text";
      span.textContent = text;

      td.appendChild(track);
      td.appendChild(span);
      return td;
    }

    default:
      td.textContent = String(value);
      return td;
  }
}

// =====================================================================
// Render de un panel
// =====================================================================

function renderPanel(panel, monitor) {
  panel.classList.remove("loading", "error");
  if (monitor.status === "loading") panel.classList.add("loading");
  if (monitor.status === "error")   panel.classList.add("error");

  const sub  = panel.querySelector("[data-role='subtitle']");
  const ts   = panel.querySelector("[data-role='ts']");
  const body = panel.querySelector("[data-role='body']");

  sub.textContent = monitor.subtitle || "";
  // panel-ts eliminado — el timestamp de actualización se muestra en el
  // live-block del header global ("EN VIVO HH:MM:SS hs") que ya cubre
  // todo el dashboard. Por ticker de panel no aporta información adicional.

  body.innerHTML = "";
  if (monitor.status !== "ok") return;
  if (!monitor.rows || monitor.rows.length === 0) {
    body.innerHTML = `<div style="padding:14px 16px;color:var(--text-dim)">Sin datos.</div>`;
    return;
  }

  // Filtrado de columnas redundantes (name/maturity en bonares y bopreales)
  // + columnas que el usuario ocultó vía el botón ▦ del header.
  const drop = DROP_KEYS_BY_MONITOR[monitor.id] || new Set();
  const userHidden = new Set(hiddenColsByMonitor[monitor.id] || []);
  const cols = monitor.columns.filter(
    (c) => !drop.has(c.key) && !userHidden.has(c.key),
  );

  // Pre-calculo max abs por columna con barra
  const maxAbsByCol = new Map();
  for (const c of cols) {
    if (BAR_KINDS.has(c.kind)) {
      maxAbsByCol.set(c.key, columnMaxAbs(monitor.rows, c.key));
    }
  }
  const NO_DETAIL_PANELS = new Set(["futuros"]);
  const ctx = {
    maxAbsByCol,
    noDetail: NO_DETAIL_PANELS.has(monitor.id),
    stockChart: monitor.id === "panel_lider",
  };

  const table = document.createElement("table");
  table.className = "bonds";

  const thead = document.createElement("thead");
  const trh = document.createElement("tr");
  cols.forEach((col) => {
    const th = document.createElement("th");
    th.textContent = col.label;
    if (col.kind === "text" || col.kind === "date") th.classList.add("col-text");
    trh.appendChild(th);
  });
  thead.appendChild(trh);
  table.appendChild(thead);

  const tbody = document.createElement("tbody");
  monitor.rows.forEach((row) => {
    const tr = document.createElement("tr");
    cols.forEach((col) => {
      const td = renderCell(col, row[col.key], ctx);
      // Sendero BEI: REM proyectado (≠ dato real REM) → cursiva + color tenue
      if (monitor.id === "bei_sendero" && row.rem_projected &&
          (col.key === "rem_mensual" || col.key === "diff")) {
        td.style.fontStyle = "italic";
        td.style.opacity = "0.7";
      }
      tr.appendChild(td);
    });
    tbody.appendChild(tr);
  });
  table.appendChild(tbody);
  body.appendChild(table);
}

// =====================================================================
// Render del panel CURVA SOBERANA (Chart.js scatter + smooth line)
// Usa los datos de los monitores soberanos (bonares/bopreales): TIR vs DM.
// =====================================================================

function splitBySeries(points) {
  const al = [], gd = [];
  for (const p of points) {
    const t = String(p.ticker).toUpperCase();
    if (t.startsWith("AL") || t.startsWith("AO") || t.startsWith("AE")) al.push(p);
    else if (t.startsWith("GD")) gd.push(p);
  }
  al.sort((a, b) => a.x - b.x);
  gd.sort((a, b) => a.x - b.x);
  return { al, gd };
}

// Regresion logaritmica y = a + b * ln(x) por minimos cuadrados.
// Devuelve {a, b} o null si no se puede ajustar.
function fitLogCurve(points) {
  if (!points || points.length < 2) return null;
  const lnXs = [], ys = [];
  for (const p of points) {
    if (!(p.x > 0) || !Number.isFinite(p.y)) return null;
    lnXs.push(Math.log(p.x));
    ys.push(p.y);
  }
  const n = points.length;
  const meanLnX = lnXs.reduce((a, b) => a + b, 0) / n;
  const meanY   = ys.reduce((a, b) => a + b, 0) / n;
  let num = 0, den = 0;
  for (let i = 0; i < n; i++) {
    num += (lnXs[i] - meanLnX) * (ys[i] - meanY);
    den += (lnXs[i] - meanLnX) ** 2;
  }
  if (den === 0) return null;
  const b = num / den;
  const a = meanY - b * meanLnX;
  return { a, b };
}

// Para puntos clusterizados (cerca entre si en x,y), apila las labels
// verticalmente con un offset incremental para evitar overlap.
// Devuelve array de offsets (px) alineado con `series`.
function computeLabelOffsets(series, baseOffset, thresholdX = 0.20,
                              thresholdY = 0.5, step = 14) {
  if (!series || !series.length) return [];
  const out = [];
  for (let i = 0; i < series.length; i++) {
    let cluster = 0;
    for (let j = 0; j < i; j++) {
      const dx = Math.abs(series[i].x - series[j].x);
      const dy = Math.abs(series[i].y - series[j].y);
      if (dx < thresholdX && dy < thresholdY) cluster++;
    }
    out.push(baseOffset + cluster * step);
  }
  return out;
}

// Genera N puntos a lo largo de la curva log (entre min y max de xs).
function logCurvePoints(points, n = 100) {
  const fit = fitLogCurve(points);
  if (!fit) return [];
  const xs = points.map((p) => p.x);
  const xMin = Math.min(...xs);
  const xMax = Math.max(...xs);
  if (!(xMax > xMin)) return [];
  const step = (xMax - xMin) / (n - 1);
  const out = [];
  for (let i = 0; i < n; i++) {
    const x = xMin + i * step;
    out.push({ x, y: fit.a + fit.b * Math.log(x) });
  }
  return out;
}

// Construye los datasets de Chart.js para una serie (puntos + linea log).
function curvaDatasets(series, color, label, labelAlign, opts = {}) {
  if (!series || series.length === 0) return [];
  // Offset base segun alineacion (top -> negativo en datalabels conven., bot -> positivo)
  const baseOffset = 8;
  const offsets = computeLabelOffsets(series, baseOffset);
  // Rich/cheap: colorea cada punto por su residuo vs la curva log ajustada
  // (verde = sobre-rinde/barato, rojo = sub-rinde/caro, base = sobre la curva).
  let pointColors = color;
  if (opts.colorByResidual) {
    const fit = fitLogCurve(series);
    if (fit) {
      pointColors = series.map((p) => {
        const resid = p.y - (fit.a + fit.b * Math.log(p.x));
        if (resid >  CURVE_RESID_BAND) return CURVE_CHEAP;
        if (resid < -CURVE_RESID_BAND) return CURVE_RICH;
        return color;
      });
    }
  }
  return [
    {
      // Puntos reales
      label,
      data: series,
      showLine: false,
      backgroundColor:      pointColors,
      pointBackgroundColor: pointColors,
      borderColor:     color,
      pointRadius: 6,
      pointHoverRadius: 9,
      datalabels: {
        align: labelAlign, anchor: "center",
        // offset por punto (apila labels en clusters)
        offset: (ctx) => offsets[ctx.dataIndex] ?? baseOffset,
        color,
        font: { weight: 700, size: 11 },
        formatter: (v) => v.ticker,
      },
    },
    {
      // Curva log (oculta del legend con "_line_" prefix)
      label: `_line_${label}`,
      data: logCurvePoints(series),
      showLine: true,
      borderColor: color,
      borderWidth: 2,
      pointRadius: 0,
      pointHoverRadius: 0,
      backgroundColor: "transparent",
      tension: 0,
      datalabels: { display: false },
    },
  ];
}

function renderCurvaPanel(panel, bonaresMonitor, bopMonitor) {
  panel.classList.remove("loading", "error");
  if (bonaresMonitor.status === "loading") { panel.classList.add("loading"); return; }
  if (bonaresMonitor.status === "error")   { panel.classList.add("error");   return; }

  const sub    = panel.querySelector("[data-role='subtitle']");
  const canvas = panel.querySelector("[data-role='canvas']");

  // Puntos AL/AE/GD desde bonares (solo MEP — ver CURVA_TICKER_RE).
  const sovPoints = (bonaresMonitor.rows || [])
    .map((row) => ({ ticker: row.ticker, x: row.duration, y: row.tir }))
    .filter((p) => p.x != null && p.y != null && _isCurvaTicker(p.ticker));

  // Puntos BOPREALES (solo MEP, BPY6D excluido por vto inminente).
  const bpRows = (bopMonitor && bopMonitor.rows) || [];
  const bopr = bpRows
    .map((row) => ({ ticker: row.ticker, x: row.duration, y: row.tir }))
    .filter((p) => p.x != null && p.y != null && _isCurvaTicker(p.ticker))
    .sort((a, b) => a.x - b.x);

  const { al, gd } = splitBySeries(sovPoints);

  const totalBonos = al.length + gd.length + bopr.length;
  sub.textContent = `${totalBonos} bonos · regresión logarítmica · TIR vs Duration`;

  const datasets = [
    ...curvaDatasets(al,   "#6ab4f7",          "BONARES (AL/AO)",  "top"),
    ...curvaDatasets(gd,   "#f0c040",          "GLOBALES (GD/AE)", "bottom"),
    ...curvaDatasets(bopr, CHART.BOPREAL,     "BOPREALES",              "bottom"),
  ];

  // Update incremental si ya existe la instancia
  if (curvaChart) {
    curvaChart.data.datasets = datasets;
    curvaChart.update("none");
    return;
  }

  // Primera vez: registrar plugin de datalabels y crear el chart
  if (window.Chart && window.ChartDataLabels) {
    Chart.register(ChartDataLabels);
  }

  curvaChart = new Chart(canvas.getContext("2d"), {
    type: "scatter",
    data: { datasets },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      layout: { padding: { top: 24, right: 16, bottom: 8, left: 8 } },
      scales: {
        x: {
          title: {
            display: true,
            text: "Duration Modificada (años)",
            color: CHART.TEXT_DIM,
            font: { weight: 700, size: 12 },
          },
          ticks: { color: CHART.TEXT_DIM, font: { size: 11 } },
          grid:  { color: CHART.GRID },
        },
        y: {
          title: {
            display: true,
            text: "Rendimiento (TIR %)",
            color: CHART.TEXT_DIM,
            font: { weight: 700, size: 12 },
          },
          ticks: {
            color: CHART.TEXT_DIM,
            font: { size: 11 },
            callback: (v) => `${fmt.number(v, 1)}%`,
          },
          grid: { color: CHART.GRID },
        },
      },
      plugins: {
        legend: {
          display: true,
          position: "top",
          align: "end",
          labels: {
            color: CHART.NAVY_DARK,
            font: { weight: 700, size: 11 },
            boxWidth: 12, boxHeight: 12,
            usePointStyle: true,
            // Oculta los datasets de la curva (label "_line_..." )
            filter: (item) => !item.text.startsWith("_line_"),
          },
        },
        tooltip: {
          backgroundColor: CHART.NAVY,
          titleColor: "#fff",
          bodyColor: "#fff",
          padding: 10,
          // Solo tooltips en los datasets de puntos (no en la linea de regresion)
          filter: (item) => !item.dataset.label.startsWith("_line_"),
          callbacks: {
            title: (items) => items[0].raw.ticker,
            label: (item) =>
              `TIR ${fmt.number(item.raw.y, 2)}%  ·  DM ${fmt.number(item.raw.x, 2)} años`,
          },
        },
        datalabels: { padding: 4 },
      },
    },
  });
}

// =====================================================================
// Header (fecha + live indicator)
// =====================================================================

function setBrandDate(d = new Date()) {
  document.getElementById("brand-date").textContent = fmt.dateV4(d);
}

function setLiveStatus(state, ts) {
  // state: "ok" | "loading" | "error"
  const block = document.getElementById("live-block");
  block.classList.remove("ok", "loading", "error");
  block.classList.add(state);

  // live-time eliminado del header — no hay nada que actualizar
}

// =====================================================================
// Render global + fetch
// =====================================================================

function renderFxStrip(fx) {
  const el = document.getElementById("fx-strip");
  if (!el) return;
  if (!fx || Object.keys(fx).length === 0) {
    el.innerHTML = '<span class="fx-empty">Cargando cotizaciones USD…</span>';
    return;
  }
  const ORDER = ["oficial", "mayorista", "blue", "bolsa", "contadoconliqui", "cripto", "tarjeta", "tamar"];
  // Nombres cortos para el chip (override del `nombre` que manda dolarapi).
  const NAME_OVERRIDES = { contadoconliqui: "CCL", bolsa: "MEP" };
  const num = (v) => v != null
    ? Number(v).toLocaleString("es-AR", { minimumFractionDigits: 2, maximumFractionDigits: 2 })
    : "—";
  const parts = [];

  // Chip BCRA Reservas — va al principio, separado visualmente del bloque FX.
  if (fx._bcra_macro) {
    const { reservas, delta } = fx._bcra_macro;
    const resVal = reservas != null
      ? `USD ${(reservas / 1000).toLocaleString("es-AR", { minimumFractionDigits: 1, maximumFractionDigits: 1 })}bn`
      : "—";
    let deltaHtml = "";
    if (delta != null) {
      const sign = delta >= 0 ? "+" : "";
      const cls  = delta >= 0 ? "bcra-delta-pos" : "bcra-delta-neg";
      deltaHtml = `<span class="fx-side"><span class="fx-side-label">∆ hoy</span><span class="fx-val ${cls}">${sign}${Math.round(delta)}mm</span></span>`;
    }
    parts.push(
      `<div class="fx-quote bcra-macro">
         <span class="fx-name">Reservas</span>
         <div class="fx-prices">
           <span class="fx-side"><span class="fx-val">${resVal}</span></span>
           ${deltaHtml}
         </div>
       </div>`
    );
  }

  // Chip Riesgo País (EMBI+) — ArgentinaDatos, TTL 5min.
  if (fx._riesgo_pais) {
    const { valor, delta_abs, delta_pct } = fx._riesgo_pais;
    let deltaHtml = "";
    if (delta_abs != null && delta_pct != null) {
      const cls   = delta_abs > 0 ? "rp-delta-pos" : delta_abs < 0 ? "rp-delta-neg" : "";
      const arrow = delta_abs > 0 ? "▲" : delta_abs < 0 ? "▼" : "●";
      const sign  = delta_abs > 0 ? "+" : "";
      deltaHtml = `<span class="rp-delta ${cls}">${arrow} ${sign}${delta_abs} (${sign}${delta_pct}%)</span>`;
    }
    parts.push(
      `<div class="fx-quote riesgo-pais">
         <span class="fx-name">RIESGO PAÍS</span>
         <div class="fx-prices">
           <span class="fx-side rp-row">
             <span class="fx-val">${valor != null ? Number(valor).toLocaleString("es-AR") : "—"}</span>
             ${deltaHtml}
           </span>
         </div>
       </div>`
    );
  }

  for (const casa of ORDER) {
    const q = fx[casa];
    if (!q) continue;
    const nombre = NAME_OVERRIDES[casa] || q.nombre || casa;
    if (casa === "tamar") {
      parts.push(
        `<div class="fx-quote">
           <span class="fx-name">${nombre}</span>
           <div class="fx-prices">
             <span class="fx-side"><span class="fx-side-label">TNA</span><span class="fx-val">${num(q.venta)}%</span></span>
           </div>
         </div>`
      );
      continue;
    }
    parts.push(
      `<div class="fx-quote">
         <span class="fx-name">${nombre}</span>
         <div class="fx-prices">
           <span class="fx-side"><span class="fx-side-label">Compra</span><span class="fx-val">$${num(q.compra)}</span></span>
           <span class="fx-side"><span class="fx-side-label">Venta</span><span class="fx-val">$${num(q.venta)}</span></span>
         </div>
       </div>`
    );
  }
  el.innerHTML = parts.join("");
}

function renderAll(snapshot) {
  let anyError = false;
  let anyLoading = false;

  renderFxStrip(snapshot.fx || {});

  snapshot.monitors.forEach((m) => {
    if (m.status === "error")   anyError = true;
    if (m.status === "loading") anyLoading = true;
    const panel = document.querySelector(`.panel[data-id='${m.id}']`);
    if (panel) renderPanel(panel, m);
  });

  // Curva soberana: panel virtual que combina AL/AE + GD desde el monitor
  // bonares (3 colores: AL/AE, GD, BOPREALES).
  const bnr = snapshot.monitors.find((m) => m.id === "bonares");
  const bp  = snapshot.monitors.find((m) => m.id === "bopreales");
  const curvaPanel = document.querySelector(".panel[data-id='curva_soberana']");
  if (curvaPanel && bnr) renderCurvaPanel(curvaPanel, bnr, bp);

  // Curvas extra (CER, Tasa Fija, DL, TAMAR, DUAL) — solo se renderizan si
  // el panel está visible. Cada uno lee del monitor indicado en data-source.
  document.querySelectorAll(".panel.panel-curva[data-source]").forEach((panel) => {
    const item = panel.closest(".grid-stack-item");
    if (item && item.style.display === "none") return;
    const src = panel.getAttribute("data-source");
    const monitor = snapshot.monitors.find((m) => m.id === src);
    if (monitor) renderBondCurve(panel, monitor);
  });


  if (anyError)        setLiveStatus("error", snapshot.ts);
  else if (anyLoading) setLiveStatus("loading", snapshot.ts);
  else                 setLiveStatus("ok", snapshot.ts);
}

// Último snapshot para poder re-renderizar localmente cuando el usuario
// togglea columnas, sin tener que esperar al próximo fetch.
let lastSnapshot = null;
let lastBeiRows = null;

// fetch con timeout explícito (AbortController). Evita que un endpoint colgado
// deje la UI esperando para siempre; en error mantenemos el último dato bueno.
async function fetchWithTimeout(url, opts = {}, ms = 6000) {
  const ctrl = new AbortController();
  const id = setTimeout(() => ctrl.abort(), ms);
  try {
    return await fetch(url, { ...opts, signal: ctrl.signal });
  } finally {
    clearTimeout(id);
  }
}

async function fetchSnapshot() {
  try {
    const r = await fetchWithTimeout("/api/snapshot", { cache: "no-store" }, 6000);
    if (!r.ok) throw new Error("HTTP " + r.status);
    const j = await r.json();
    lastSnapshot = j;
    renderAll(j);
    // El primer fetch garantiza que conocemos las columnas — recién entonces
    // tiene sentido inyectar los botones de columnas (popover lee de aquí).
    _injectColumnButtons();
  } catch (e) {
    console.warn("snapshot fetch fallo:", e);
    // Fallback graceful: si ya teníamos datos, los dejamos en pantalla y solo
    // marcamos el estado (stale). El próximo tick reintenta.
    setLiveStatus("error");
  }
}

// =====================================================================
// Column visibility: botón ▦ en cada panel header + popover con checkboxes.
// Persistido por panel en localStorage; sobrevive a reloads.
// =====================================================================

const COLS_STORAGE_KEY = "monitor.hiddenCols.v1";
let hiddenColsByMonitor = {};

function loadHiddenCols() {
  try {
    hiddenColsByMonitor = JSON.parse(localStorage.getItem(COLS_STORAGE_KEY) || "{}");
  } catch {
    hiddenColsByMonitor = {};
  }
  // Seed columnas ocultas por default para monitores que aún no fueron
  // configurados por el usuario (key === undefined). Si el usuario hizo
  // "Mostrar todas" el key queda como [] (no undefined), y no se pisa.
  let changed = false;
  for (const [mid, defaults] of Object.entries(DEFAULT_HIDDEN_COLS_BY_MONITOR)) {
    if (hiddenColsByMonitor[mid] === undefined) {
      hiddenColsByMonitor[mid] = defaults.slice();
      changed = true;
    }
  }
  if (changed) saveHiddenCols();
}

function saveHiddenCols() {
  try {
    localStorage.setItem(COLS_STORAGE_KEY, JSON.stringify(hiddenColsByMonitor));
  } catch (e) {
    console.warn("No pude guardar columnas ocultas:", e);
  }
}

function _escHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]),
  );
}

function _buildColsPopover(pop, monitorId) {
  const m = lastSnapshot && lastSnapshot.monitors.find((x) => x.id === monitorId);
  if (!m || !m.columns || !m.columns.length) {
    pop.innerHTML = '<div class="cols-popover-empty">Sin datos aún</div>';
    return;
  }
  const drop = DROP_KEYS_BY_MONITOR[monitorId] || new Set();
  const hidden = new Set(hiddenColsByMonitor[monitorId] || []);
  const visibleCols = m.columns.filter((c) => !drop.has(c.key));

  const items = visibleCols
    .map((col) => {
      const checked = !hidden.has(col.key) ? "checked" : "";
      return (
        '<label class="cols-popover-item">' +
        `<input type="checkbox" ${checked} data-col="${_escHtml(col.key)}"/>` +
        `<span>${_escHtml(col.label || col.key)}</span>` +
        "</label>"
      );
    })
    .join("");

  pop.innerHTML =
    '<div class="cols-popover-title">Columnas visibles</div>' +
    items +
    '<div class="cols-popover-actions">' +
    '<button type="button" class="cols-popover-reset">Mostrar todas</button>' +
    "</div>";

  pop.querySelectorAll("input[type=checkbox]").forEach((cb) => {
    cb.addEventListener("change", (e) => {
      e.stopPropagation();
      const key = cb.dataset.col;
      const set = new Set(hiddenColsByMonitor[monitorId] || []);
      if (cb.checked) set.delete(key);
      else set.add(key);
      hiddenColsByMonitor[monitorId] = [...set];
      saveHiddenCols();
      if (lastSnapshot) renderAll(lastSnapshot);
    });
  });

  pop.querySelector(".cols-popover-reset").addEventListener("click", (e) => {
    e.stopPropagation();
    hiddenColsByMonitor[monitorId] = [];   // [] = "mostrar todas" explícito (≠ undefined = nunca tocado)
    saveHiddenCols();
    pop.hidden = true;
    if (lastSnapshot) renderAll(lastSnapshot);
  });

  // Cliks DENTRO del popover no lo deben cerrar.
  pop.addEventListener("click", (e) => e.stopPropagation());
}

// Paneles de bonos que tienen curva TIR vs DM (los rows del monitor tienen
// `duration` y `tir`). Los charts (panel-curva) ya tienen su propia vista, así
// que el botón se inyecta solo sobre los paneles tabulares de bonos.
const CURVE_POPUP_SOURCES = new Set([
  "bonares", "bopreales", "cer", "tasa_fija",
  "dolar_linked", "tamar",
]);

// Singleton del popup: una sola instancia DOM reutilizada — al cerrar se
// destruye el Chart.js pero el contenedor queda en el body para próxima vez.
let _curvePopupChart = null;
function _ensureCurvePopupDom() {
  let overlay = document.getElementById("curve-popup-overlay");
  if (overlay) return overlay;
  overlay = document.createElement("div");
  overlay.id = "curve-popup-overlay";
  overlay.className = "curve-popup-overlay";
  overlay.hidden = true;
  overlay.innerHTML = `
    <div class="curve-popup" role="dialog" aria-modal="true" aria-labelledby="curve-popup-title">
      <header class="curve-popup-header">
        <h3 id="curve-popup-title">Curva</h3>
        <span class="curve-popup-sub" data-role="subtitle"></span>
        <span class="curve-popup-ts" data-role="ts">—</span>
        <button class="curve-popup-close" type="button" aria-label="Cerrar">×</button>
      </header>
      <div class="curve-popup-body panel panel-curva" data-id="__curve_popup__">
        <div class="chart-container">
          <canvas data-role="canvas" role="img" aria-label="Curva TIR vs Duration Modificada"></canvas>
        </div>
      </div>
    </div>`;
  document.body.appendChild(overlay);

  const close = () => closeCurvePopup();
  overlay.querySelector(".curve-popup-close").addEventListener("click", close);
  // Click en el backdrop (fuera de .curve-popup) cierra; click dentro no.
  overlay.addEventListener("click", (e) => {
    if (e.target === overlay) close();
  });
  return overlay;
}

function openCurvePopup(monitorId, label, renderFn) {
  if (!lastSnapshot) return;
  const monitor = lastSnapshot.monitors.find((m) => m.id === monitorId);
  if (!monitor) return;
  const render = renderFn || renderBondCurve;
  const overlay = _ensureCurvePopupDom();
  overlay.querySelector("#curve-popup-title").textContent = `Curva — ${label}`;

  const body = overlay.querySelector(".curve-popup-body");
  // Cache key único por panel para no chocar con los charts de los paneles
  // curva-extra. data-source es lo que renderBondCurve usa de key.
  body.setAttribute("data-source", `popup_${monitorId}`);
  // Mover subtitle/ts del header al panel para que renderBondCurve los pueble.
  // Como están en el header del popup (que es hermano del body), seteamos los
  // refs directamente leyendo del monitor.
  const sub = overlay.querySelector(".curve-popup-sub");
  const ts = overlay.querySelector(".curve-popup-ts");
  // Crear inputs temporales dentro del body para que renderBondCurve los encuentre.
  let bSub = body.querySelector("[data-role='subtitle']");
  let bTs = body.querySelector("[data-role='ts']");
  if (!bSub) { bSub = document.createElement("span"); bSub.setAttribute("data-role", "subtitle"); bSub.style.display = "none"; body.appendChild(bSub); }
  if (!bTs) { bTs = document.createElement("span"); bTs.setAttribute("data-role", "ts"); bTs.style.display = "none"; body.appendChild(bTs); }

  overlay.hidden = false;
  trapFocusIn(overlay);
  // Render dentro del body (el renderer detecta el canvas vía data-source).
  render(body, monitor);
  // Espejar subtitle/ts al header visible.
  if (sub) sub.textContent = bSub.textContent;
  if (ts) ts.textContent = bTs.textContent;
  _curvePopupChart = bondCurveCharts[`popup_${monitorId}`] || null;
  // Forzar resize tras el reveal porque el canvas pasó de hidden a visible.
  requestAnimationFrame(() => { if (_curvePopupChart) _curvePopupChart.resize(); });
}

function closeCurvePopup() {
  const overlay = document.getElementById("curve-popup-overlay");
  if (!overlay || overlay.hidden) return;
  overlay.hidden = true;
  releaseFocusTrap();
  // Destruir el Chart.js para liberar el canvas (sino al reabrir con otro
  // monitor el chart anterior interferiría).
  Object.keys(bondCurveCharts).forEach((k) => {
    if (k.startsWith("popup_")) {
      try { bondCurveCharts[k].destroy(); } catch {}
      delete bondCurveCharts[k];
    }
  });
  _curvePopupChart = null;
}

// =====================================================================
// Popup de histórico de precios (OHLC desde data912.com)
// Click sobre celda .ticker-clickable → fetch /api/bond_history/<ticker>
// → line chart de precios de cierre, default último 1 año.
// =====================================================================

const HISTORY_RANGES = [
  { id: "1M", days: 30, label: "1M" },
  { id: "3M", days: 90, label: "3M" },
  { id: "6M", days: 180, label: "6M" },
  { id: "1Y", days: 365, label: "1Y" },
  { id: "ALL", days: null, label: "All" },
];
const HISTORY_DEFAULT_RANGE = "1Y";

// Tickers que SÍ tienen histórico en data912.com/historical/bonds.
// Source of truth en Python (apps/web/server.py:HISTORICAL_SUPPORTED_TICKERS).
// Lo consumimos vía /api/supported_tickers al boot — sin esto teníamos
// dos copias hardcoded (Python + JS) con riesgo de drift.
// Arranca con un placeholder vacío; init() lo puebla async.
let HISTORY_SUPPORTED_TICKERS = new Set();

async function _loadSupportedTickers() {
  try {
    const r = await fetchWithTimeout("/api/supported_tickers", {}, 6000);
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const j = await r.json();
    HISTORY_SUPPORTED_TICKERS = new Set(j.tickers || []);
    // Re-render paneles si ya hay un snapshot — para marcar celdas
    // ticker-clickable que llegaron antes que la lista.
    if (lastSnapshot) renderAll(lastSnapshot);
  } catch (e) {
    console.warn("No pude cargar /api/supported_tickers — popup de histórico deshabilitado:", e);
  }
}

// Convención de sufijos de moneda:
//   sin sufijo  → Pesos (ARS)
//   sufijo "D"  → MEP (USD vía bono local)
//   sufijo "C"  → CABLE (USD offshore)
// Construyo base→variants una vez al cargar el script. Si AL30 ∈ supported
// y AL30D también, entonces base "AL30" tiene variantes Pesos + MEP. Si
// además existe AL30C → tres variantes (caso AL30 y GD30).
// Si un ticker termina en D pero su base sin D NO está en supported (ej
// BA37D, BPY26), se trata como ticker único — no se muestra selector.
const _CURRENCY_ORDER = { Pesos: 0, MEP: 1, CABLE: 2 };
const HISTORY_VARIANTS_BY_BASE = (function() {
  const m = new Map();
  for (const t of HISTORY_SUPPORTED_TICKERS) {
    let base = t, currency = "Pesos";
    if (t.endsWith("D") && HISTORY_SUPPORTED_TICKERS.has(t.slice(0, -1))) {
      base = t.slice(0, -1); currency = "MEP";
    } else if (t.endsWith("C") && HISTORY_SUPPORTED_TICKERS.has(t.slice(0, -1))) {
      base = t.slice(0, -1); currency = "CABLE";
    }
    if (!m.has(base)) m.set(base, []);
    m.get(base).push({ ticker: t, currency });
  }
  for (const list of m.values()) list.sort((a, b) => _CURRENCY_ORDER[a.currency] - _CURRENCY_ORDER[b.currency]);
  return m;
})();
const HISTORY_BASE_BY_TICKER = (function() {
  const m = new Map();
  for (const [base, variants] of HISTORY_VARIANTS_BY_BASE) {
    for (const v of variants) m.set(v.ticker, base);
  }
  return m;
})();


// =====================================================================
// Popup de DETALLE de bono (tabs: Detalles + Chart + Calculadora)
// Reemplaza al history popup como entry point de cualquier ticker.
// =====================================================================

let _bondPopupTicker = null;
let _bondPopupDetail = null;          // payload de /api/bond_detail
let _bondPopupTab = "detalles";       // "detalles" | "chart" | "calc"
let _bondPopupChart = null;           // Chart.js instance del tab Chart
let _bondPopupChartPoints = [];       // OHLC points cargados
let _bondPopupCalcDebounce = null;

const _BOND_CALC_DEBOUNCE_MS = 250;
const _DASH = "—";

function _bondFmtNum(v, dec = 2) {
  if (v === null || v === undefined || Number.isNaN(Number(v))) return _DASH;
  return Number(v).toLocaleString("es-AR", {
    minimumFractionDigits: dec, maximumFractionDigits: dec,
  });
}
function _bondFmtPctSgn(v, dec = 2) {
  if (v === null || v === undefined || Number.isNaN(Number(v))) return _DASH;
  const n = Number(v) * 100;
  const sign = n > 0 ? "+" : "";
  return `${sign}${n.toLocaleString("es-AR", { minimumFractionDigits: dec, maximumFractionDigits: dec })}%`;
}
function _bondFmtPct(v, dec = 2) {
  if (v === null || v === undefined || Number.isNaN(Number(v))) return _DASH;
  return `${(Number(v) * 100).toLocaleString("es-AR", {
    minimumFractionDigits: dec, maximumFractionDigits: dec,
  })}%`;
}
function _bondFmtDate(iso) {
  if (!iso) return _DASH;
  const d = new Date(iso + "T00:00:00");
  if (Number.isNaN(d.getTime())) return iso;
  const dd = String(d.getDate()).padStart(2, "0");
  const mm = String(d.getMonth() + 1).padStart(2, "0");
  const aa = String(d.getFullYear()).slice(-2);
  return `${dd}-${mm}-${aa}`;
}

function _ensureBondPopupDom() {
  let overlay = document.getElementById("bond-popup-overlay");
  if (overlay) return overlay;
  overlay = document.createElement("div");
  overlay.id = "bond-popup-overlay";
  overlay.className = "bond-popup-overlay";
  overlay.hidden = true;
  overlay.innerHTML = `
    <div class="bond-popup" role="dialog" aria-modal="true" aria-labelledby="bond-popup-title">
      <header class="bond-popup-header">
        <h3 id="bond-popup-title">Detalle</h3>
        <nav class="bond-popup-tabs" role="tablist">
          <button type="button" class="bond-tab active" data-tab="detalles" role="tab">Detalles</button>
          <button type="button" class="bond-tab" data-tab="chart" role="tab">Chart</button>
          <button type="button" class="bond-tab" data-tab="calc" role="tab">Calculadora</button>
        </nav>
        <button class="bond-popup-close" type="button" aria-label="Cerrar">×</button>
      </header>
      <div class="bond-popup-body">
        <section class="bond-tab-pane" data-pane="detalles"></section>
        <section class="bond-tab-pane" data-pane="chart" hidden>
          <div class="bond-chart-controls">
            <div class="history-currency-row" data-role="currency-row" hidden></div>
            <div class="history-range-group" role="group" aria-label="Rango temporal">
              ${HISTORY_RANGES.map(r =>
                `<button type="button" class="history-range-btn" data-range="${r.id}">${r.label}</button>`
              ).join("")}
            </div>
            <span class="bond-chart-sub" data-role="chart-sub"></span>
          </div>
          <div class="chart-container bond-chart-container">
            <canvas data-role="canvas" role="img" aria-label="Precio histórico"></canvas>
          </div>
        </section>
        <section class="bond-tab-pane" data-pane="calc" hidden></section>
      </div>
    </div>`;
  document.body.appendChild(overlay);

  overlay.querySelector(".bond-popup-close").addEventListener("click", closeBondPopup);
  // Cierre SOLO por botón X o tecla ESC — sin click en el fondo (evita cerrar
  // sin querer al interactuar cerca del borde del popup).
  overlay.querySelectorAll(".bond-tab").forEach((b) => {
    b.addEventListener("click", () => _setBondPopupTab(b.getAttribute("data-tab")));
  });
  overlay.querySelectorAll(".history-range-btn").forEach((b) => {
    b.addEventListener("click", () => _setBondChartRange(b.getAttribute("data-range")));
  });
  // Resize de ventana → recomputar CF cap si Detalles está visible.
  window.addEventListener("resize", () => {
    if (!overlay.hidden && _bondPopupTab === "detalles") _resizeCashflowCard(overlay);
  });
  return overlay;
}

function _setBondPopupTab(tab) {
  const overlay = document.getElementById("bond-popup-overlay");
  if (!overlay || tab === _bondPopupTab) return;
  const body = overlay.querySelector(".bond-popup-body");

  // Transición suave: medir alto actual, switchear, medir alto natural nuevo,
  // animar de uno a otro. Sin esto el popup snappea instantáneo entre tabs
  // y "salta" si los tamaños difieren mucho (CF largo vs Calculadora corta).
  const oldH = body.offsetHeight;
  _bondPopupTab = tab;
  overlay.querySelectorAll(".bond-tab").forEach((b) => {
    b.classList.toggle("active", b.getAttribute("data-tab") === tab);
  });
  overlay.querySelectorAll(".bond-tab-pane").forEach((p) => {
    p.hidden = p.getAttribute("data-pane") !== tab;
  });

  // En Detalles, ajustar el CF card antes de medir (si no, el grid arrancaría
  // con el CF a su altura natural — overflow visual durante la transición).
  if (tab === "detalles") _resizeCashflowCard(overlay);

  body.style.height = "auto";
  const newH = body.offsetHeight;
  body.style.height = oldH + "px";
  void body.offsetHeight;  // force reflow para que la transición arranque del valor viejo
  body.style.transition = "height 220ms ease-out";
  body.style.height = newH + "px";
  setTimeout(() => {
    body.style.transition = "";
    body.style.height = "auto";
    if (tab === "chart" && _bondPopupChart) _bondPopupChart.resize();
  }, 240);
}

function _resizeCashflowCard(overlay) {
  // CF card max-height = altura natural de la columna izquierda.
  // Pareja CSS Grid no nos sirve porque por default ambos cols estiran al
  // row más alto; queremos lo opuesto (derecha capada por izquierda).
  const pane = overlay.querySelector("[data-pane='detalles']");
  if (!pane) return;
  const left = pane.querySelector(".bond-detalles-left");
  const card = pane.querySelector(".bond-cf-card");
  if (!left || !card) return;
  // Soltar el max-height previo para que la izquierda mida su natural sin
  // verse afectada por el grid stretching (en realidad no la afecta, pero
  // por las dudas).
  card.style.maxHeight = "";
  const h = left.offsetHeight;
  if (h > 0) card.style.maxHeight = h + "px";
}

async function openBondDetailPopup(ticker, opts) {
  if (!ticker) return;
  const overlay = _ensureBondPopupDom();
  overlay.hidden = false;
  document.body.style.overflow = "hidden";  // lock scroll del fondo mientras el popup está abierto
  if (!(opts && opts.reload)) trapFocusIn(overlay);
  const isReload = !!(opts && opts.reload);
  _bondPopupTicker = String(ticker).toUpperCase();
  if (!isReload) _bondPopupTab = "detalles";

  // Reset tabs visuales SOLO en open inicial (en reload mantenemos la tab activa).
  if (!isReload) {
    overlay.querySelectorAll(".bond-tab").forEach((b) => {
      b.classList.toggle("active", b.getAttribute("data-tab") === "detalles");
    });
    overlay.querySelectorAll(".bond-tab-pane").forEach((p) => {
      p.hidden = p.getAttribute("data-pane") !== "detalles";
    });
    overlay.querySelector("#bond-popup-title").textContent = `${_bondPopupTicker} — cargando…`;
    overlay.querySelector("[data-pane='detalles']").innerHTML = `<div class="bond-loading">Cargando detalle…</div>`;
  }

  let payload;
  try {
    const res = await fetch(
      `/api/bond_detail/${encodeURIComponent(_bondPopupTicker)}?lag=${_bondCalcSettlementLag}`
    );
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    payload = await res.json();
  } catch (e) {
    overlay.querySelector("[data-pane='detalles']").innerHTML =
      `<div class="bond-error">No se pudo cargar el detalle: ${e.message || e}</div>`;
    return;
  }
  _bondPopupDetail = payload;

  const meta = payload.meta || {};
  overlay.querySelector("#bond-popup-title").textContent =
    `${meta.ticker || _bondPopupTicker}` + (meta.short_name && meta.short_name !== meta.ticker ? ` — ${meta.short_name}` : "");

  _renderBondDetailesTab(overlay, payload);
  _renderBondCalcTab(overlay, payload);

  // Tab Chart: ocultar el botón si data912 no soporta histórico para este ticker.
  const tabChart = overlay.querySelector(".bond-tab[data-tab='chart']");
  if (payload.chart_supported) {
    tabChart.hidden = false;
    _initBondChartTab(overlay, _bondPopupTicker);
  } else {
    tabChart.hidden = true;
    if (_bondPopupChart) { try { _bondPopupChart.destroy(); } catch {} _bondPopupChart = null; }
  }
}

function closeBondPopup() {
  const overlay = document.getElementById("bond-popup-overlay");
  if (!overlay || overlay.hidden) return;
  overlay.hidden = true;
  document.body.style.overflow = "";  // restaurar scroll del fondo
  releaseFocusTrap();
  if (_bondPopupChart) { try { _bondPopupChart.destroy(); } catch {} _bondPopupChart = null; }
  _bondPopupDetail = null;
  _bondPopupChartPoints = [];
  _bondPopupTicker = null;
  _bondCalcSettlementLag = 1;  // reset al default T+1 para el próximo open
  if (_bondPopupCalcDebounce) { clearTimeout(_bondPopupCalcDebounce); _bondPopupCalcDebounce = null; }
}

// ---------- Tab 1: Detalles --------------------------------------------- //
function _renderBondDetailesTab(overlay, payload) {
  const pane = overlay.querySelector("[data-pane='detalles']");
  const meta = payload.meta || {};
  const metrics = payload.metrics || {};
  const cashflows = payload.cashflows || [];

  const metaRows = [
    ["Tipo", meta.instrument_type || _DASH],
    ["Cupón", meta.cupon || _DASH],
    ["Moneda", meta.currency || _DASH],
    ["Fecha emisión", _bondFmtDate(meta.fecha_emision)],
    ["Fecha vencimiento", _bondFmtDate(meta.fecha_vencimiento)],
    ["Último cupón", _bondFmtDate(metrics.last_coupon_date)],
    ["Próximo cupón", _bondFmtDate(metrics.next_coupon_date)],
    ["Freq. pagos (anual)", meta.payment_frequency || _DASH],
  ];
  if (meta.category) metaRows.splice(2, 0, ["Categoría", meta.category]);
  if (meta.cer_base != null) metaRows.push(["CER base", _bondFmtNum(meta.cer_base, 4)]);
  if (meta.cer_lag != null) metaRows.push(["CER lag (días háb.)", meta.cer_lag]);
  if (meta.spread_rate != null) metaRows.push(["Spread TAMAR", _bondFmtPct(meta.spread_rate, 2)]);
  if (meta.floor_rate_monthly != null) metaRows.push(["Floor mensual (DUAL)", _bondFmtPct(meta.floor_rate_monthly, 2)]);
  if (meta.cer_spread != null) metaRows.push(["Spread CER", _bondFmtPct(meta.cer_spread, 2)]);

  const metricsRows = [
    ["TIR efectiva (TEA)", _bondFmtPct(metrics.tir, 2)],
    ["TIR nominal (TNA)", _bondFmtPct(metrics.tna, 2)],
    ["TEM", _bondFmtPct(metrics.tem, 2)],
    ["Modified Duration", _bondFmtNum(metrics.duration, 2)],
    ["Valor Técnico", _bondFmtNum(metrics.technical_value, 2)],
    ["Paridad", _bondFmtPct(metrics.parity, 2)],
    ["Valor Residual %", _bondFmtNum(metrics.residual_nominal, 2)],
    ["Intereses corridos", _bondFmtNum(metrics.accrued_interest, 4) +
      (metrics.days_accrued != null ? ` (${metrics.days_accrued}d)` : "")],
    ["Current Yield", _bondFmtPct(metrics.current_yield, 2)],
    ["Term to Maturity (años)", _bondFmtNum(metrics.term_to_maturity, 2)],
  ];

  const cfRows = cashflows.length === 0
    ? `<tr><td colspan="6" class="empty">Sin cashflows</td></tr>`
    : cashflows.map(c => {
        const cls = [];
        if (c.is_past) cls.push("past");
        if (c.is_next) cls.push("next");
        return `
        <tr class="${cls.join(" ")}">
          <td>${_bondFmtDate(c.date)}</td>
          <td class="num">${_bondFmtNum(c.vr_cartera, 2)}</td>
          <td class="num">${_bondFmtNum(c.interest, 4)}</td>
          <td class="num">${_bondFmtNum(c.amortization, 4)}</td>
          <td class="label">${c.label || "—"}</td>
          <td class="num">${_bondFmtNum(c.total, 4)}</td>
        </tr>`;
      }).join("");

  const cardHtml = (title, rows) => `
    <div class="bond-card">
      <h4>${title}</h4>
      <table class="bond-info-table">
        <tbody>${rows.map(([k, v]) => `<tr><th>${k}</th><td>${v}</td></tr>`).join("")}</tbody>
      </table>
    </div>`;

  pane.innerHTML = `
    <div class="bond-detalles-grid">
      <div class="bond-detalles-left">
        ${cardHtml("Descripción técnica", metaRows)}
        ${cardHtml("Métricas vivas", metricsRows)}
      </div>
      <div class="bond-detalles-right">
        <div class="bond-card bond-cf-card">
          <h4>Cashflow (${cashflows.length})</h4>
          <div class="bond-cf-table-wrap">
            <table class="bond-cf-table">
              <thead>
                <tr>
                  <th>Fecha Cupón</th>
                  <th>VR Cartera</th>
                  <th>Renta Efect.</th>
                  <th>Amortización %<br>c/100 VN</th>
                  <th>Obs. Prox. Pago</th>
                  <th>Total</th>
                </tr>
              </thead>
              <tbody>${cfRows}</tbody>
            </table>
          </div>
        </div>
      </div>
    </div>`;

  // Sizing del CF: cap a la altura natural de la columna izquierda + scroll
  // del próximo cupón al centro (útil en bonos con muchos cupones pasados).
  requestAnimationFrame(() => {
    const overlay = document.getElementById("bond-popup-overlay");
    if (overlay) _resizeCashflowCard(overlay);
    const nextRow = pane.querySelector(".bond-cf-table tr.next");
    if (nextRow) nextRow.scrollIntoView({ block: "center", behavior: "auto" });
  });
}

// ---------- Tab 2: Chart (histórico OHLC) ------------------------------- //

// Cutoff temporal (ms epoch) para un rango de HISTORY_RANGES; days null/0 → todo.
function _rangeCutoff(rangeId) {
  const range = HISTORY_RANGES.find(r => r.id === rangeId);
  return range && range.days ? (Date.now() - range.days * 86400000) : -Infinity;
}

// Normaliza la respuesta /api/*_history a puntos {t,c,o,h,l,v,dr,sa} ordenados por fecha.
function _parseHistoryPoints(payload) {
  return (payload.points || [])
    .map(p => {
      const t = Date.parse(p.date);
      return Number.isFinite(t) ? { t, c: p.c, o: p.o, h: p.h, l: p.l, v: p.v, dr: p.dr, sa: p.sa } : null;
    })
    .filter(p => p && p.c != null && Number.isFinite(p.c))
    .sort((a, b) => a.t - b.t);
}

// Config Chart.js de un line chart OHLC (cierre + tooltip O/H/L/retorno/σ). pointsRef
// devuelve el array completo de puntos para que el tooltip resuelva O/H/L por fecha.
function _ohlcLineConfig({ ticker, data, pointsRef, color, bg, borderWidth, tension, padding, spanDays }) {
  const dateTick = (v) => {
    const d = new Date(v);
    return spanDays > 270
      ? d.toLocaleDateString("es-AR", { month: "short", year: "2-digit" })
      : d.toLocaleDateString("es-AR", { day: "2-digit", month: "short" });
  };
  return {
    type: "line",
    data: {
      datasets: [{
        label: ticker, data,
        borderColor: color, backgroundColor: bg,
        borderWidth, pointRadius: 0, pointHoverRadius: 4,
        tension, fill: true,
      }],
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      interaction: { mode: "index", intersect: false },
      layout: { padding },
      scales: {
        x: {
          type: "linear",
          ticks: { color: CHART.TEXT_DIM, font: { size: 11 }, maxTicksLimit: 8, autoSkip: true, callback: dateTick },
          grid: { color: CHART.GRID },
        },
        y: {
          title: { display: true, text: "Precio (cierre)", color: CHART.TEXT_DIM, font: { weight: 700, size: 12 } },
          ticks: { color: CHART.TEXT_DIM, font: { size: 11 }, callback: (v) => fmt.number(v, 2) },
          grid: { color: CHART.GRID },
        },
      },
      plugins: {
        legend: { display: false },
        datalabels: { display: false },
        tooltip: {
          backgroundColor: CHART.NAVY, titleColor: "#fff", bodyColor: "#fff", padding: 10,
          callbacks: {
            title: (items) => new Date(items[0].parsed.x).toLocaleDateString("es-AR", {
              day: "2-digit", month: "short", year: "numeric",
            }),
            label: (item) => {
              const p = pointsRef().find(x => x.t === item.parsed.x);
              if (!p) return `Cierre $${fmt.number(item.parsed.y, 2)}`;
              const lines = [`Cierre $${fmt.number(p.c, 2)}`];
              if (p.o != null) lines.push(`O ${fmt.number(p.o, 2)}  H ${fmt.number(p.h, 2)}  L ${fmt.number(p.l, 2)}`);
              if (p.dr != null) lines.push(`Retorno diario ${fmt.number(p.dr * 100, 2)}%`);
              if (p.sa != null) lines.push(`σ anualizada ${fmt.number(p.sa * 100, 1)}%`);
              return lines;
            },
          },
        },
      },
    },
  };
}

function _initBondChartTab(overlay, ticker) {
  // Currency selector: si hay variantes Pesos/MEP/CABLE de la misma especie.
  const base = HISTORY_BASE_BY_TICKER.get(ticker) || ticker;
  const variants = HISTORY_VARIANTS_BY_BASE.get(base) || [{ ticker, currency: "" }];
  const curRow = overlay.querySelector("[data-pane='chart'] [data-role='currency-row']");
  curRow.innerHTML = "";
  if (variants.length > 1) {
    curRow.hidden = false;
    for (const v of variants) {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "history-currency-btn";
      btn.setAttribute("data-ticker", v.ticker);
      btn.innerHTML = `<span class="cur-name">${v.currency}</span><span class="cur-ticker">${v.ticker}</span>`;
      btn.addEventListener("click", () => _loadBondChart(v.ticker));
      curRow.appendChild(btn);
    }
  } else {
    curRow.hidden = true;
  }
  _loadBondChart(ticker);
}

async function _loadBondChart(ticker) {
  const overlay = document.getElementById("bond-popup-overlay");
  if (!overlay) return;
  overlay.querySelectorAll("[data-pane='chart'] .history-currency-btn").forEach((b) => {
    b.classList.toggle("active", b.getAttribute("data-ticker") === ticker);
  });
  const sub = overlay.querySelector("[data-role='chart-sub']");
  sub.textContent = "Cargando…";

  let payload;
  try {
    const res = await fetch(`/api/bond_history/${encodeURIComponent(ticker)}`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    payload = await res.json();
  } catch (e) {
    sub.textContent = `Sin histórico disponible (${e.message || e})`;
    if (_bondPopupChart) { try { _bondPopupChart.destroy(); } catch {} _bondPopupChart = null; }
    _bondPopupChartPoints = [];
    return;
  }

  const points = _parseHistoryPoints(payload);
  _bondPopupChartPoints = points;
  if (!points.length) { sub.textContent = "Sin datos"; return; }

  const first = points[0], last = points[points.length - 1];
  const totalYears = fmt.number((last.t - first.t) / 86400000 / 365.25, 1);
  sub.textContent =
    `${points.length} ruedas · desde ${new Date(first.t).toLocaleDateString("es-AR")} (${totalYears}a) · último $${fmt.number(last.c, 2)}`;

  overlay.querySelectorAll("[data-pane='chart'] .history-range-btn").forEach((b) => {
    b.classList.toggle("active", b.getAttribute("data-range") === HISTORY_DEFAULT_RANGE);
  });

  const canvas = overlay.querySelector("[data-pane='chart'] [data-role='canvas']");
  if (_bondPopupChart) { try { _bondPopupChart.destroy(); } catch {} _bondPopupChart = null; }
  const cutoff = _rangeCutoff(HISTORY_DEFAULT_RANGE);
  const filtered = points.filter(p => p.t >= cutoff);

  _bondPopupChart = new Chart(canvas.getContext("2d"), _ohlcLineConfig({
    ticker,
    data: filtered.map(p => ({ x: p.t, y: p.c })),
    pointsRef: () => _bondPopupChartPoints,
    color: CHART.ACCENT_BLUE, bg: "rgba(46,99,255,0.10)",
    borderWidth: 1.6, tension: 0.18,
    padding: { top: 16, right: 12, bottom: 6, left: 6 },
    spanDays: (last.t - first.t) / 86400000,
  }));
  requestAnimationFrame(() => _bondPopupChart && _bondPopupChart.resize());
}

function _setBondChartRange(rangeId) {
  const overlay = document.getElementById("bond-popup-overlay");
  if (!overlay) return;
  overlay.querySelectorAll("[data-pane='chart'] .history-range-btn").forEach((b) => {
    b.classList.toggle("active", b.getAttribute("data-range") === rangeId);
  });
  if (!_bondPopupChart) return;
  const cutoff = _rangeCutoff(rangeId);
  const pts = _bondPopupChartPoints.filter(p => p.t >= cutoff);
  _bondPopupChart.data.datasets[0].data = pts.map(p => ({ x: p.t, y: p.c }));
  _bondPopupChart.update("none");
}

// =====================================================================
// Stock Chart Popup — Panel Líder historical price chart
// =====================================================================

let _stockChart = null;
let _stockChartPoints = [];
let _stockChartRange = "1Y";

function _getStockPopup() {
  let overlay = document.getElementById("stock-chart-overlay");
  if (overlay) return overlay;
  overlay = document.createElement("div");
  overlay.id = "stock-chart-overlay";
  overlay.className = "stock-chart-overlay";
  overlay.innerHTML = `
    <div class="stock-chart-popup" role="dialog" aria-modal="true">
      <header class="stock-chart-header">
        <span class="stock-chart-title" id="stock-chart-title">—</span>
        <span class="stock-chart-sub" data-role="sub"></span>
        <div class="stock-chart-ranges">
          ${HISTORY_RANGES.map(r =>
            `<button class="history-range-btn${r.id === "1Y" ? " active" : ""}" data-range="${r.id}">${r.label}</button>`
          ).join("")}
        </div>
        <button class="stock-chart-close" type="button" aria-label="Cerrar">×</button>
      </header>
      <div class="stock-chart-body">
        <canvas data-role="canvas"></canvas>
      </div>
    </div>`;
  document.body.appendChild(overlay);

  overlay.querySelector(".stock-chart-close").addEventListener("click", closeStockChartPopup);
  overlay.addEventListener("click", (e) => {
    if (e.target === overlay) closeStockChartPopup();
  });
  overlay.querySelectorAll(".history-range-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      _stockChartRange = btn.getAttribute("data-range");
      overlay.querySelectorAll(".history-range-btn").forEach(b =>
        b.classList.toggle("active", b === btn));
      _applyStockChartRange();
    });
  });
  return overlay;
}

function closeStockChartPopup() {
  const overlay = document.getElementById("stock-chart-overlay");
  if (!overlay) return;
  overlay.classList.remove("open");
  if (_stockChart) { try { _stockChart.destroy(); } catch {} _stockChart = null; }
  _stockChartPoints = [];
}

async function openStockChartPopup(ticker) {
  _stockChartRange = "1Y";
  const overlay = _getStockPopup();
  overlay.classList.add("open");
  overlay.querySelector("#stock-chart-title").textContent = ticker;
  const sub = overlay.querySelector("[data-role='sub']");
  sub.textContent = "Cargando…";
  overlay.querySelectorAll(".history-range-btn").forEach(b =>
    b.classList.toggle("active", b.getAttribute("data-range") === "1Y"));
  if (_stockChart) { try { _stockChart.destroy(); } catch {} _stockChart = null; }

  let payload;
  try {
    const res = await fetch(`/api/stock_history/${encodeURIComponent(ticker)}`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    payload = await res.json();
  } catch (e) {
    sub.textContent = `Error al cargar histórico (${e.message || e})`;
    return;
  }

  const points = _parseHistoryPoints(payload);
  _stockChartPoints = points;
  if (!points.length) { sub.textContent = "Sin datos disponibles"; return; }

  const first = points[0], last = points[points.length - 1];
  sub.textContent =
    `${points.length} ruedas · desde ${new Date(first.t).toLocaleDateString("es-AR")} · último $${fmt.number(last.c, 2)}`;

  _renderStockChart(overlay, ticker);
}

function _renderStockChart(overlay, ticker) {
  const canvas = overlay.querySelector("[data-role='canvas']");
  if (_stockChart) { try { _stockChart.destroy(); } catch {} _stockChart = null; }

  const cutoff = _rangeCutoff(_stockChartRange);
  const pts = _stockChartPoints.filter(p => p.t >= cutoff);
  if (!pts.length) return;

  const first = pts[0], last = pts[pts.length - 1];
  _stockChart = new Chart(canvas.getContext("2d"), _ohlcLineConfig({
    ticker,
    data: pts.map(p => ({ x: p.t, y: p.c })),
    pointsRef: () => _stockChartPoints,
    color: "#4fc3f7", bg: "rgba(79,195,247,0.08)",
    borderWidth: 1.8, tension: 0.15,
    padding: { top: 12, right: 16, bottom: 4, left: 8 },
    spanDays: (last.t - first.t) / 86400000,
  }));
  requestAnimationFrame(() => _stockChart && _stockChart.resize());
}

function _applyStockChartRange() {
  if (!_stockChart || !_stockChartPoints.length) return;
  const overlay = document.getElementById("stock-chart-overlay");
  if (!overlay) return;
  const cutoff = _rangeCutoff(_stockChartRange);
  const pts = _stockChartPoints.filter(p => p.t >= cutoff);
  const first = pts[0] || _stockChartPoints[0];
  const last = pts[pts.length - 1] || _stockChartPoints[_stockChartPoints.length - 1];
  const spanDays = first && last ? (last.t - first.t) / 86400000 : 365;
  _stockChart.options.scales.x.ticks.callback = (v) => {
    const d = new Date(v);
    return spanDays > 270
      ? d.toLocaleDateString("es-AR", { month: "short", year: "2-digit" })
      : d.toLocaleDateString("es-AR", { day: "2-digit", month: "short" });
  };
  _stockChart.data.datasets[0].data = pts.map(p => ({ x: p.t, y: p.c }));
  _stockChart.update("none");
}

// ---------- Tab 3: Calculadora ------------------------------------------ //
// UX: 4 inputs editables (Dirty / Clean / TIR / Paridad). Cambiar cualquiera
// recalcula los otros tres. Sin toggle de modo — el "source" es el último
// input que el usuario tocó.
//
// Truco para evitar feedback loop: `_suppressBondCalcInput` flag bloquea
// el handler 'input' mientras updateamos los campos programáticamente.
// V.Téc cacheada localmente para convertir Paridad → Dirty sin round-trip.

let _bondCalcVTec = null;       // V.Téc actual (per-100), del último resultado
let _bondCalcLastSource = null;  // "dirty" | "clean" | "tir" | "parity"
let _suppressBondCalcInput = false;
let _letraInflChart = null;      // Chart.js instance para el chart TEM vs inflación en letras
let _cerCurveChart = null;       // Chart.js scatter de la curva CER en la calculadora
let _bondCalcScenTimer = null;   // debounce del input "Mi escenario" (escenarios CER)
let _bondCalcSettlementLag = 1;  // 0 = T+0, 1 = T+1 (default mercado AR)

function _renderBondCalcTab(overlay, payload) {
  const pane = overlay.querySelector("[data-pane='calc']");
  const meta = payload.meta || {};
  const metrics = payload.metrics || {};
  const settleDate = _bondFmtDate(payload.settle_date);
  const isTamar = !!meta.is_tamar_family;
  const itype = (meta.instrument_type || "").toUpperCase();
  const isLetra = itype.includes("LECAP") || itype.includes("BONCAP");
  const isCer = !itype.includes("TAMAR") &&
    (itype.includes("CER") || itype.includes("CON CUPON") || itype.includes("STEP-UP"));

  _bondCalcVTec = (metrics.technical_value != null) ? Number(metrics.technical_value) : null;
  _bondCalcLastSource = null;
  if (payload.settlement_lag != null) _bondCalcSettlementLag = payload.settlement_lag;

  const initDirty = metrics.price_dirty != null ? Number(metrics.price_dirty).toFixed(4) : "";
  const initClean = metrics.price_clean != null ? Number(metrics.price_clean).toFixed(4) : "";
  const initTir = metrics.tir != null ? (metrics.tir * 100).toFixed(4) : "";
  const initParity = metrics.parity != null ? (metrics.parity * 100).toFixed(4) : "";
  const initTna365 = metrics.tna != null ? (metrics.tna * 100).toFixed(4) : "";
  const initTem365 = metrics.tem != null ? (metrics.tem * 100).toFixed(4) : "";
  const initTamar = metrics.tamar_forecast_used != null
    ? (metrics.tamar_forecast_used * 100).toFixed(2) : "";
  const ccy = meta.currency || "ARS";
  const lag = _bondCalcSettlementLag;

  pane.innerHTML = `
    <div class="bond-calc-grid">
      <div class="bond-calc-left">
        <div class="bond-card">
          <h4>Inputs editables</h4>
          <p class="bond-calc-help">${isLetra ? "Modificá cualquiera de los valores; los otros se recalculan." : "Modificá cualquiera de los 4 valores; los otros se recalculan."}</p>
          <div class="bond-calc-form">
            <div class="bond-calc-settle" role="group" aria-label="Liquidación">
              <span class="bond-calc-settle-label">Liquidación</span>
              <div class="bond-calc-settle-toggle">
                <button type="button" class="bond-settle-btn ${lag === 0 ? "active" : ""}" data-lag="0">T+0</button>
                <button type="button" class="bond-settle-btn ${lag === 1 ? "active" : ""}" data-lag="1">T+1</button>
              </div>
              <span class="bond-calc-settle-date" data-role="settle-date">${settleDate}</span>
            </div>
            <label class="bond-calc-row">
              <span>Precio (${ccy})</span>
              <input type="number" step="0.0001" data-input="dirty" value="${initDirty}">
            </label>
            ${!isLetra ? `
            <label class="bond-calc-row">
              <span>Precio Clean (${ccy})</span>
              <input type="number" step="0.0001" data-input="clean" value="${initClean}">
            </label>` : ""}
            <label class="bond-calc-row">
              <span>TIR (%)</span>
              <input type="number" step="0.0001" data-input="tir" value="${initTir}">
            </label>
            <label class="bond-calc-row">
              <span>Paridad (%)</span>
              <input type="number" step="0.0001" data-input="parity" value="${initParity}">
            </label>
            ${isLetra ? `
              <label class="bond-calc-row">
                <span>TNA (365)</span>
                <input type="number" step="0.0001" data-input="tna365" value="${initTna365}">
              </label>
              <label class="bond-calc-row">
                <span>TEM (365)</span>
                <input type="number" step="0.0001" data-input="tem365" value="${initTem365}">
              </label>` : ""}
            ${isTamar ? `
              <label class="bond-calc-row bond-calc-tamar">
                <span>TAMAR proyectado (%)</span>
                <input type="number" step="0.01" data-input="tamar" value="${initTamar}">
              </label>
              <p class="bond-calc-help">
                Override de la TAMAR forward usada para proyectar al vencimiento.
                Default = última publicada por BCRA.
              </p>` : ""}
          </div>
        </div>
        ${isCer ? `<div class="bond-calc-scenarios">
          <h4>Retorno por escenario de inflación</h4>
          <table class="bond-info-table bond-calc-scen-table">
            <thead><tr><th>Escenario</th><th>Infl./mes</th><th>Ret. total</th><th>TEA nom.</th></tr></thead>
            <tbody data-role="cer-scen-body">
              <tr><td colspan="4" class="bond-calc-help">Calculando…</td></tr>
            </tbody>
          </table>
          <div class="bond-calc-scen-mode">
            <span class="bond-calc-scen-modelbl">Mi escenario</span>
            <span class="cer-seg">
              <button type="button" class="cer-mode-btn on" data-mode="uniforme">Uniforme</button>
              <button type="button" class="cer-mode-btn" data-mode="custom">Custom</button>
            </span>
            <label class="cer-unif-wrap">%/mes <input type="number" step="0.01" class="cer-custom-infl-input" value=""></label>
          </div>
          <div class="cer-month-editor" hidden>
            <div class="cer-month-lbl">Inflación %/mes hasta el vencimiento</div>
            <div class="cer-month-list scroll-styled" data-role="cer-month-list"></div>
          </div>
          <p class="bond-calc-help" style="margin-top:6px">Proyecta el índice CER (con su lag de ~2 meses y prorrateo por 10º día hábil) y descuenta los flujos nominales contra el precio actual.</p>
        </div>` : ""}
      </div>${""}

      <div class="bond-calc-right">
        <div class="bond-card">
          <h4>Medidas de rentabilidad</h4>
          <table class="bond-info-table" data-role="results-rent">
            <tbody>
              <tr><th>TIR Efectiva (TEA)</th><td data-key="tir">${_bondFmtPct(metrics.tir, 2)}</td></tr>
              <tr class="bond-tbl-subhdr"><th colspan="2">Base 365 (act/365)</th></tr>
              <tr><th>TNA (m=365)</th><td data-key="tna">${_bondFmtPct(metrics.tna, 2)}</td></tr>
              <tr><th>TEM (act/365)</th><td data-key="tem">${_bondFmtPct(metrics.tem, 2)}</td></tr>
              <tr class="bond-tbl-subhdr"><th colspan="2">Base 360 (Sec. Finanzas — LECAPs)</th></tr>
              <tr><th>TNA (m=12)</th><td data-key="tna_360">${_bondFmtPct(metrics.tna_mensual, 2)}</td></tr>
              <tr><th>TEM (30/360)</th><td data-key="tem_360">${_bondFmtPct(metrics.tem_360, 2)}</td></tr>
              ${isTamar ? `<tr><th>Spread de Mercado</th><td data-key="spread_mercado">${_bondFmtPct(metrics.spread_mercado, 2)}</td></tr>` : ""}
              <tr><th>Current Yield</th><td data-key="current_yield">${_bondFmtPct(metrics.current_yield, 2)}</td></tr>
              <tr><th>TIR real (vs REM)</th><td data-key="tir_real">${_bondFmtPctSgn(metrics.tir_real, 2)}</td></tr>
              ${isCer ? `<tr><th>Inflación indiferencia</th><td><span data-key="breakeven_infl_monthly">${_DASH}</span> <span class="bond-calc-aux" data-key="breakeven_vs_ticker"></span></td></tr>` : ""}
            </tbody>
          </table>
        </div>

        <div class="bond-card">
          <h4>Valuación c/100 VN</h4>
          <table class="bond-info-table" data-role="results-val">
            <tbody>
              <tr><th>Valor Residual</th><td data-key="residual_nominal">${_bondFmtNum(metrics.residual_nominal, 2)}</td></tr>
              <tr><th>Intereses corridos (días)</th>
                  <td><span data-key="accrued_interest">${_bondFmtNum(metrics.accrued_interest, 4)}</span>
                      <span class="bond-calc-aux" data-key="days_accrued">${metrics.days_accrued != null ? `(${metrics.days_accrued}d)` : ""}</span></td></tr>
              <tr><th>Valor Técnico</th><td data-key="technical_value">${_bondFmtNum(metrics.technical_value, 2)}</td></tr>
              <tr><th>Clean / Residual %</th><td data-key="parity_clean">${_bondFmtPct(metrics.parity_clean, 4)}</td></tr>
            </tbody>
          </table>
        </div>

        <div class="bond-card">
          <h4>Medidas de sensibilidad</h4>
          <table class="bond-info-table" data-role="results-sens">
            <tbody>
              <tr><th>Modified Duration</th><td data-key="duration">${_bondFmtNum(metrics.duration, 4)}</td></tr>
              <tr><th>DV01 (per 100)</th><td data-key="dv01">${_bondFmtNum(metrics.dv01, 4)}</td></tr>
              <tr><th>Convexidad (PPV)</th><td data-key="convexity">${_bondFmtNum(metrics.convexity, 4)}</td></tr>
              <tr><th>Term to Maturity (años)</th><td data-key="term_to_maturity">${_bondFmtNum(metrics.term_to_maturity, 4)}</td></tr>
              <tr><th>Carry+Roll 30d</th><td data-key="carry_roll">${_bondFmtPctSgn(metrics.carry_roll, 2)}</td></tr>
              <tr><th>Spread vs curva</th><td data-key="spread_curva">${_bondFmtPctSgn(metrics.spread_curva, 2)}</td></tr>
            </tbody>
          </table>
        </div>
        ${isLetra ? `<div class="bond-card">
          <h4>TEM vs Inflación proyectada</h4>
          <div style="position:relative;height:200px">
            <canvas id="letra-infl-chart"></canvas>
          </div>
          <p class="bond-calc-help" style="margin-top:6px">% mensual · TEM letra · REM-BCRA · BEI implícito</p>
        </div>` : ""}
        ${isCer ? `<div class="bond-card">
          <h4>Posición en curva CER</h4>
          <div style="position:relative;height:200px">
            <canvas id="cer-curve-chart"></canvas>
          </div>
          <p class="bond-calc-help" style="margin-top:6px">TIR real vs Duration · este bono en amarillo</p>
        </div>` : ""}
      </div>
    </div>`;

  // Trigger explícito: ENTER o blur. Sin auto-update por keystroke — evita
  // recalcular mientras todavía estás tipeando (ej. "6" cuando vas a poner 64).
  const fire = (el) => {
    if (_suppressBondCalcInput) return;
    _bondCalcLastSource = el.getAttribute("data-input");
    _runBondCalc();
  };
  pane.querySelectorAll("input[data-input]").forEach((el) => {
    el.addEventListener("keydown", (e) => {
      if (e.key === "Enter") { e.preventDefault(); el.blur(); fire(el); }
    });
    el.addEventListener("change", () => fire(el));  // dispara al blur si cambió
  });

  // Toggle T+0 / T+1: cambia la fecha de referencia, recarga todo el detail
  // del ticker (re-fetch /api/bond_detail con el nuevo lag). El tab Calc queda
  // visible (reload preserva la tab activa).
  pane.querySelectorAll(".bond-settle-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      const newLag = parseInt(btn.getAttribute("data-lag"), 10);
      if (newLag === _bondCalcSettlementLag) return;
      _bondCalcSettlementLag = newLag;
      // Re-fetch del detalle con nuevo lag. opts.reload preserva la tab activa.
      openBondDetailPopup(_bondPopupTicker, { reload: true });
    });
  });

  if (isLetra) {
    const temPct = metrics.tem != null ? metrics.tem * 100 : null;
    _renderLetraInflChart(pane, temPct);
  }

  if (isCer) {
    _setCerBreakeven(pane, metrics.tir, metrics.duration);
    _renderCerCurveChart(pane, _bondPopupTicker, metrics.duration, metrics.tir);
    // Default modo Uniforme = REM mensual del primer mes (indicativo).
    const defInfl = _cerDefaultMonthlyInfl();
    const unifEl = pane.querySelector(".cer-custom-infl-input");
    if (unifEl && defInfl != null) unifEl.value = (defInfl * 100).toFixed(2);
    _cerBuildMonthEditor(pane, meta.fecha_vencimiento);
    _cerScenRefresh(pane, metrics.price_dirty);

    const debouncedRefresh = () => {
      clearTimeout(_bondCalcScenTimer);
      _bondCalcScenTimer = setTimeout(() => _cerScenRefresh(pane), 400);
    };
    // Toggle Uniforme / Custom.
    pane.querySelectorAll(".cer-mode-btn").forEach((b) => {
      b.addEventListener("click", () => {
        pane.querySelectorAll(".cer-mode-btn").forEach((x) => x.classList.toggle("on", x === b));
        const custom = b.dataset.mode === "custom";
        pane.querySelector(".cer-month-editor").hidden = !custom;
        const uw = pane.querySelector(".cer-unif-wrap");
        if (uw) uw.style.display = custom ? "none" : "";
        _cerScenRefresh(pane);
      });
    });
    // Input uniforme.
    if (unifEl) {
      unifEl.addEventListener("keydown", (e) => {
        if (e.key === "Enter") { e.preventDefault(); unifEl.blur(); _cerScenRefresh(pane); }
      });
      unifEl.addEventListener("input", debouncedRefresh);
    }
    // Inputs por mes (delegado).
    const list = pane.querySelector("[data-role='cer-month-list']");
    if (list) list.addEventListener("input", debouncedRefresh);
  }
}

async function _runBondCalc() {
  const overlay = document.getElementById("bond-popup-overlay");
  if (!overlay || !_bondPopupTicker || !_bondCalcLastSource) return;
  const pane = overlay.querySelector("[data-pane='calc']");
  if (!pane) return;

  const readNum = (sel) => {
    const raw = pane.querySelector(sel)?.value;
    const n = parseFloat(raw);
    return Number.isFinite(n) ? n : null;
  };

  const source = _bondCalcLastSource;
  let body = null;

  if (source === "tir") {
    const t = readNum("input[data-input='tir']");
    if (t === null) return;
    body = { mode: "from_tir", tir: t / 100.0 };
  } else if (source === "dirty") {
    const p = readNum("input[data-input='dirty']");
    if (p === null || p <= 0) return;
    body = { mode: "from_price", price: p, price_mode: "dirty" };
  } else if (source === "clean") {
    const p = readNum("input[data-input='clean']");
    if (p === null || p <= 0) return;
    body = { mode: "from_price", price: p, price_mode: "clean" };
  } else if (source === "parity") {
    // Paridad (%) → Dirty = paridad/100 × V.Téc. Sin V.Téc no podemos invertir.
    const par = readNum("input[data-input='parity']");
    if (par === null || _bondCalcVTec === null || _bondCalcVTec <= 0) return;
    const dirty = (par / 100.0) * _bondCalcVTec;
    if (dirty <= 0) return;
    body = { mode: "from_price", price: dirty, price_mode: "dirty" };
  } else if (source === "tamar") {
    // Cambiar TAMAR proyectado mantiene el precio dirty actual y recalcula
    // la TIR implícita (y todo lo downstream). Mental model del trader:
    // "si TAMAR fuera X%, qué TIR me da el precio actual?".
    const p = readNum("input[data-input='dirty']");
    if (p === null || p <= 0) return;
    body = { mode: "from_price", price: p, price_mode: "dirty" };
  } else if (source === "tna365") {
    const tna = readNum("input[data-input='tna365']");
    if (tna === null) return;
    // TNA(365) → TEA: (1 + TNA/365)^365 − 1
    const tea = Math.pow(1.0 + (tna / 100.0) / 365.0, 365.0) - 1.0;
    body = { mode: "from_tir", tir: tea };
  } else if (source === "tem365") {
    const tem = readNum("input[data-input='tem365']");
    if (tem === null) return;
    // TEM(act/365) → TEA: (1 + TEM)^(365/30) − 1
    const tea = Math.pow(1.0 + tem / 100.0, 365.0 / 30.0) - 1.0;
    body = { mode: "from_tir", tir: tea };
  }
  if (!body) return;
  body.settlement_lag = _bondCalcSettlementLag;
  // tamar_forecast: enviar SIEMPRE el valor actual del input (si el bono
  // es TAMAR family). Si el campo está vacío, mandar null = usar BCRA default.
  const tamarRaw = pane.querySelector("input[data-input='tamar']");
  if (tamarRaw) {
    const tv = parseFloat(tamarRaw.value);
    body.tamar_forecast = Number.isFinite(tv) ? (tv / 100.0) : null;
  }

  try {
    const res = await fetch(`/api/bond_calculate/${encodeURIComponent(_bondPopupTicker)}`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!res.ok) {
      console.warn("bond_calculate failed:", res.status, await res.text());
      return;
    }
    const out = await res.json();
    if (out.technical_value != null) _bondCalcVTec = Number(out.technical_value);
    _applyBondCalcResults(pane, out, source);
  } catch (e) {
    console.warn("bond_calculate error:", e);
  }
}

function _applyBondCalcResults(pane, out, sourceField) {
  const setTxt = (sel, val) => { const el = pane.querySelector(sel); if (el) el.textContent = val; };
  // Update tablas de resultados.
  setTxt("[data-key='tir']", _bondFmtPct(out.tir, 2));
  setTxt("[data-key='tna']", _bondFmtPct(out.tna, 2));
  setTxt("[data-key='tna_mensual']", _bondFmtPct(out.tna_mensual, 2));
  setTxt("[data-key='tem']", _bondFmtPct(out.tem, 2));
  setTxt("[data-key='current_yield']", _bondFmtPct(out.current_yield, 2));
  setTxt("[data-key='spread_mercado']", _bondFmtPct(out.spread_mercado, 2));
  setTxt("[data-key='residual_nominal']", _bondFmtNum(out.residual_nominal, 2));
  setTxt("[data-key='accrued_interest']", _bondFmtNum(out.accrued_interest, 4));
  setTxt("[data-key='days_accrued']", out.days_accrued != null ? `(${out.days_accrued}d)` : "");
  setTxt("[data-key='technical_value']", _bondFmtNum(out.technical_value, 2));
  setTxt("[data-key='parity_clean']", _bondFmtPct(out.parity_clean, 4));
  setTxt("[data-key='duration']", _bondFmtNum(out.duration, 4));
  setTxt("[data-key='dv01']", _bondFmtNum(out.dv01, 4));
  setTxt("[data-key='convexity']", _bondFmtNum(out.convexity, 4));
  setTxt("[data-key='term_to_maturity']", _bondFmtNum(out.term_to_maturity, 4));
  setTxt("[data-key='tna_360']", _bondFmtPct(out.tna_mensual, 2));
  setTxt("[data-key='tem_360']", _bondFmtPct(out.tem_360, 2));
  setTxt("[data-key='tir_real']", _bondFmtPctSgn(out.tir_real, 2));
  setTxt("[data-key='carry_roll']", _bondFmtPctSgn(out.carry_roll, 2));
  setTxt("[data-key='spread_curva']", _bondFmtPctSgn(out.spread_curva, 2));

  // Update los OTROS 3 inputs (no el que el usuario tipeó — destruiría el caret).
  _suppressBondCalcInput = true;
  try {
    const setVal = (sel, val) => {
      const el = pane.querySelector(sel);
      if (el && document.activeElement !== el) el.value = val;
    };
    if (sourceField !== "dirty" && out.price_dirty != null)
      setVal("input[data-input='dirty']", Number(out.price_dirty).toFixed(4));
    if (sourceField !== "clean" && out.price_clean != null)
      setVal("input[data-input='clean']", Number(out.price_clean).toFixed(4));
    if (sourceField !== "tir" && out.tir != null)
      setVal("input[data-input='tir']", (out.tir * 100).toFixed(4));
    if (sourceField !== "parity" && out.parity != null)
      setVal("input[data-input='parity']", (out.parity * 100).toFixed(4));
    if (sourceField !== "tna365" && out.tna != null)
      setVal("input[data-input='tna365']", (out.tna * 100).toFixed(4));
    if (sourceField !== "tem365" && out.tem != null)
      setVal("input[data-input='tem365']", (out.tem * 100).toFixed(4));
    // Si el usuario tocó otra cosa, sincronizar el campo TAMAR si vino vacío
    // (el backend devolvió el default BCRA). Si el usuario lo tipeó, respetar.
    if (sourceField !== "tamar" && out.tamar_forecast_used != null) {
      const tamarEl = pane.querySelector("input[data-input='tamar']");
      if (tamarEl && !tamarEl.value) {
        tamarEl.value = (out.tamar_forecast_used * 100).toFixed(2);
      }
    }
  } finally {
    _suppressBondCalcInput = false;
  }

  // Actualizar la línea TEM del chart de inflación si está visible.
  if (_letraInflChart && out.tem != null) {
    const temPct = out.tem * 100;
    _letraInflChart.data.datasets[0].data = _letraInflChart.data.labels.map(() => temPct);
    _letraInflChart.data.datasets[0].label = `TEM letra (${temPct.toFixed(2)}%)`;
    _letraInflChart.update("none");
  }

  // CER: el break-even, el punto activo de la curva y los escenarios reaccionan
  // al recálculo (cambió precio/TIR). El input "Mi escenario" sólo existe en CER.
  const cerCustom = pane.querySelector(".cer-custom-infl-input");
  if (cerCustom !== null) {
    _setCerBreakeven(pane, out.tir, out.duration);
    if (_cerCurveChart && out.duration != null && out.tir != null) {
      const ds = _cerCurveChart.data.datasets;
      const act = ds[ds.length - 1];
      if (act && act.label === _bondPopupTicker) {
        act.data = [{ ticker: act.label, x: out.duration, y: out.tir * 100 }];
        _cerCurveChart.update("none");
      }
    }
    if (out.price_dirty != null) _cerScenRefresh(pane, Number(out.price_dirty));
  }
}

// =====================================================================
// Chart TEM vs Inflación proyectada (solo LECAP/BONCAP)
// =====================================================================
async function _renderLetraInflChart(pane, temPct) {
  if (_letraInflChart) { _letraInflChart.destroy(); _letraInflChart = null; }
  const canvas = pane.querySelector("#letra-infl-chart");
  if (!canvas) return;

  let rows = [];
  try {
    const res = await fetch("/api/rem_bei_path");
    if (res.ok) { const d = await res.json(); rows = d.rows || []; }
  } catch (_) {}

  // Clip to last month where REM has data (REM is shorter than BEI)
  let lastRemIdx = -1;
  for (let i = 0; i < rows.length; i++) {
    if (rows[i].rem_mensual != null) lastRemIdx = i;
  }
  if (lastRemIdx >= 0) rows = rows.slice(0, lastRemIdx + 1);
  if (rows.length === 0) return;

  const labels  = rows.map(r => r.mes);
  const remData = rows.map(r => r.rem_mensual);
  const beiData = rows.map(r => r.bei_mensual);
  const temData = labels.map(() => temPct);

  const textFaint = cssv("--text-faint")   || "#8893bb";
  const gridColor = (cssv("--panel-border") || "#26345a") + "55";

  const temLabel = temPct != null ? `TEM letra (${temPct.toFixed(2)}%)` : "TEM letra";

  _letraInflChart = new Chart(canvas, {
    type: "line",
    data: {
      labels,
      datasets: [
        {
          label: temLabel,
          data: temData,
          borderColor: "#f5c451",
          backgroundColor: "transparent",
          borderWidth: 2.5,
          borderDash: [6, 4],
          pointRadius: 0,
          tension: 0,
          spanGaps: true,
        },
        {
          label: "REM BCRA",
          data: remData,
          borderColor: "#60c8ff",
          backgroundColor: "transparent",
          borderWidth: 2,
          pointRadius: 3,
          pointBackgroundColor: "#60c8ff",
          tension: 0.2,
          spanGaps: false,
        },
        {
          label: "BEI implícito",
          data: beiData,
          borderColor: "#a78bfa",
          backgroundColor: "transparent",
          borderWidth: 2,
          pointRadius: 3,
          pointBackgroundColor: "#a78bfa",
          tension: 0.2,
          spanGaps: false,
        },
      ],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      plugins: {
        datalabels: { display: false },
        legend: {
          display: true,
          labels: { color: textFaint, font: { size: 9, family: "JetBrains Mono" }, boxWidth: 16, padding: 8 },
        },
        tooltip: {
          callbacks: {
            label: ctx => `${ctx.dataset.label}: ${ctx.parsed.y != null ? Number(ctx.parsed.y).toFixed(2) + "%" : "—"}`,
          },
        },
      },
      scales: {
        x: {
          ticks: { color: textFaint, font: { size: 9, family: "JetBrains Mono" }, maxRotation: 45 },
          grid: { color: gridColor },
        },
        y: {
          ticks: { color: textFaint, font: { size: 9 }, callback: v => Number(v).toFixed(1) + "%" },
          grid: { color: gridColor },
        },
      },
    },
  });
}

// =====================================================================
// Análisis CER en la calculadora: break-even, curva CER y escenarios
// =====================================================================

// Punto de indiferencia: inflación que iguala el BONCER (TIR ya real) con la
// LECAP nominal de duration más cercana. be_anual = (1+TEA_lecap)/(1+TIR_real)−1.
function _cerBreakeven(tirReal, dmYears) {
  if (tirReal == null || dmYears == null || !lastSnapshot) return null;
  const tf = (lastSnapshot.monitors || []).find((m) => m.id === "tasa_fija");
  if (!tf || !tf.rows) return null;
  let best = null;
  for (const r of tf.rows) {
    if (r.duration > 0 && r.tir != null && Number.isFinite(r.duration) && Number.isFinite(r.tir)) {
      const d = Math.abs(r.duration - dmYears);
      if (best === null || d < best.d) best = { d, tea: r.tir / 100, ticker: r.ticker };
    }
  }
  if (!best) return null;
  const beA = (1 + best.tea) / (1 + tirReal) - 1;
  return { monthly: Math.pow(1 + beA, 1 / 12) - 1, annual: beA, ticker: best.ticker };
}

function _setCerBreakeven(pane, tirDec, dmYears) {
  const cell = pane.querySelector("[data-key='breakeven_infl_monthly']");
  const aux = pane.querySelector("[data-key='breakeven_vs_ticker']");
  if (!cell) return;
  const be = _cerBreakeven(tirDec, dmYears);
  if (!be) { cell.textContent = _DASH; if (aux) aux.textContent = ""; return; }
  cell.textContent = `${_bondFmtPct(be.monthly, 2)}/mes`;
  if (aux) aux.textContent = `vs ${be.ticker}`;
  const tr = cell.closest("tr");
  if (tr) tr.title = `Break-even anual ${_bondFmtPct(be.annual, 1)} · si la inflación supera este nivel, el bono CER le gana a ${be.ticker}`;
}

// Default razonable para "Mi escenario": primer dato mensual del sendero
// (REM si está, sino BEI). Los valores del sendero vienen en % (÷100 → decimal).
function _cerDefaultMonthlyInfl() {
  const s = (lastSnapshot && lastSnapshot.monitors || []).find((m) => m.id === "bei_sendero");
  const rows = (s && s.rows) || [];
  for (const r of rows) {
    if (r.rem_mensual != null) return r.rem_mensual / 100;
    if (r.bei_mensual != null) return r.bei_mensual / 100;
  }
  return null;
}

function _renderCerCurveChart(pane, ticker, dmYears, tirDec) {
  if (_cerCurveChart) { _cerCurveChart.destroy(); _cerCurveChart = null; }
  const canvas = pane.querySelector("#cer-curve-chart");
  if (!canvas) return;

  const cer = (lastSnapshot && lastSnapshot.monitors || []).find((m) => m.id === "cer");
  const points = ((cer && cer.rows) || [])
    .map((r) => ({ ticker: r.ticker, x: r.duration, y: r.tir }))
    .filter((p) => p.x != null && p.y != null && Number.isFinite(p.x) && Number.isFinite(p.y) && p.x > 0);

  const activeY = tirDec != null ? tirDec * 100 : null;
  const active = (dmYears != null && activeY != null)
    ? { ticker, x: dmYears, y: activeY }
    : (points.find((p) => p.ticker === ticker) || null);
  const others = points.filter((p) => p.ticker !== ticker);

  const textFaint = cssv("--text-faint") || "#8893bb";
  const gridColor = (cssv("--panel-border") || "#26345a") + "55";

  const datasets = [{
    label: "BONCER",
    data: others,
    showLine: false,
    backgroundColor: "#5b6b9e",
    borderColor: "#5b6b9e",
    pointRadius: 4,
    pointHoverRadius: 7,
    datalabels: { display: false },
  }];
  const lineData = logCurvePoints(points);
  if (lineData.length) {
    datasets.push({
      label: "_line_cer",
      data: lineData,
      type: "line",
      showLine: true,
      borderColor: "#60c8ff",
      borderWidth: 2,
      pointRadius: 0,
      pointHoverRadius: 0,
      backgroundColor: "transparent",
      tension: 0,
      datalabels: { display: false },
    });
  }
  if (active) {
    datasets.push({
      label: active.ticker,
      data: [active],
      showLine: false,
      backgroundColor: "#f5c451",
      borderColor: "#f5c451",
      pointRadius: 7,
      pointHoverRadius: 9,
      datalabels: {
        align: "top", anchor: "center", color: "#f5c451",
        font: { weight: 700, size: 11 }, formatter: (v) => v.ticker,
      },
    });
  }

  _cerCurveChart = new Chart(canvas, {
    type: "scatter",
    data: { datasets },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      plugins: {
        datalabels: { display: false },
        legend: { display: false },
        tooltip: {
          callbacks: {
            title: (items) => items[0].raw.ticker || "",
            label: (item) => `TIR ${Number(item.raw.y).toFixed(2)}% · DM ${Number(item.raw.x).toFixed(2)}a`,
          },
        },
      },
      scales: {
        x: {
          title: { display: true, text: "Duration (años)", color: textFaint, font: { size: 9 } },
          ticks: { color: textFaint, font: { size: 9 } },
          grid: { color: gridColor },
        },
        y: {
          title: { display: true, text: "TIR real (%)", color: textFaint, font: { size: 9 } },
          ticks: { color: textFaint, font: { size: 9 }, callback: (v) => Number(v).toFixed(0) + "%" },
          grid: { color: gridColor },
        },
      },
    },
  });
}

const _MES_NUM = {ene:1,feb:2,mar:3,abr:4,may:5,jun:6,jul:7,ago:8,sep:9,oct:10,nov:11,dic:12};

// {"YYYY-MM": {rem, bei}} (decimales) desde el sendero BEI del snapshot.
function _cerSenderoMaps() {
  const out = {};
  const s = (lastSnapshot && lastSnapshot.monitors || []).find((m) => m.id === "bei_sendero");
  for (const r of ((s && s.rows) || [])) {
    const mm = String(r.mes || "").toLowerCase().split("-");
    const num = _MES_NUM[mm[0]];
    if (!num || mm.length < 2) continue;
    const y = 2000 + parseInt(mm[1], 10);
    const key = `${y}-${String(num).padStart(2, "0")}`;
    out[key] = {
      rem: r.rem_mensual != null ? r.rem_mensual / 100 : null,
      bei: r.bei_mensual != null ? r.bei_mensual / 100 : null,
    };
  }
  return out;
}

// Lista de meses {y,m} desde el mes corriente hasta el de vencimiento (inclusive).
function _cerMonthKeys(matIso) {
  const out = [];
  if (!matIso) return out;
  const mat = new Date(matIso + "T00:00:00");
  if (isNaN(mat.getTime())) return out;
  const now = new Date();
  let y = now.getFullYear(), m = now.getMonth() + 1;
  const my = mat.getFullYear(), mm = mat.getMonth() + 1;
  let guard = 0;
  while ((y < my || (y === my && m <= mm)) && guard++ < 600) {
    out.push({ y, m });
    m++; if (m > 12) { m = 1; y++; }
  }
  return out;
}

const _fmtRef = (v) => v != null ? (v * 100).toFixed(2).replace(".", ",") : "—";

// Construye el editor mes-a-mes (modo Custom). REM/BEI van como referencia gris.
function _cerBuildMonthEditor(pane, matIso) {
  const list = pane.querySelector("[data-role='cer-month-list']");
  if (!list) return;
  const sm = _cerSenderoMaps();
  list.innerHTML = _cerMonthKeys(matIso).map(({ y, m }) => {
    const key = `${y}-${String(m).padStart(2, "0")}`;
    const ref = sm[key] || {};
    const remTxt = _fmtRef(ref.rem), beiTxt = _fmtRef(ref.bei);
    const lbl = `${MESES_ABBR[m - 1]}-${String(y % 100).padStart(2, "0")}`;
    return `<div class="cer-mrow">
      <span class="mes">${lbl}</span>
      <input type="text" inputmode="decimal" data-ym="${key}">
      <span class="ref">REM ${remTxt} · BEI ${beiTxt}</span>
    </div>`;
  }).join("");
}

// Mapa {"YYYY-MM": decimal} desde los inputs por mes. Forward-fill: un mes vacío
// arrastra el ÚLTIMO valor cargado por el usuario hasta el final (textbox queda
// vacío, pero el cálculo se completa). Antes del primer valor cargado, usa REM
// (o BEI) como referencia indicativa.
function _cerBuildCustomMap(pane) {
  const sm = _cerSenderoMaps();
  const out = {};
  let lastEntered = null;  // último valor tipeado por el usuario (decimal)
  pane.querySelectorAll("[data-role='cer-month-list'] input[data-ym]").forEach((el) => {
    const key = el.getAttribute("data-ym");
    const v = parseFloat((el.value || "").replace(",", ".").trim());
    if (Number.isFinite(v)) {
      lastEntered = v / 100;
      out[key] = lastEntered;
      return;
    }
    if (lastEntered != null) {           // forward-fill del último cargado
      out[key] = lastEntered;
    } else {                              // todavía no hay valores → REM/BEI ref
      const ref = sm[key] || {};
      if (ref.rem != null) out[key] = ref.rem;
      else if (ref.bei != null) out[key] = ref.bei;
    }
  });
  return out;
}

// Lee modo + inputs y dispara el POST de escenarios.
function _cerScenRefresh(pane, priceDirty) {
  const px = priceDirty != null ? priceDirty
    : parseFloat(pane.querySelector("input[data-input='dirty']")?.value);
  const mode = pane.querySelector(".cer-mode-btn.on")?.dataset.mode || "uniforme";
  let customInfl = null, customMonthly = null;
  if (mode === "custom") {
    customMonthly = _cerBuildCustomMap(pane);
  } else {
    const ci = parseFloat((pane.querySelector(".cer-custom-infl-input")?.value || "").replace(",", "."));
    customInfl = Number.isFinite(ci) ? ci / 100 : null;
  }
  _postCerScenarios(pane, Number.isFinite(px) ? px : null, customInfl, customMonthly);
}

async function _postCerScenarios(pane, priceDirty, customInfl, customMonthly) {
  const body = pane.querySelector("[data-role='cer-scen-body']");
  if (!body) return;
  let rows = [];
  try {
    const res = await fetch(`/api/cer_scenarios/${encodeURIComponent(_bondPopupTicker)}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        price_dirty: priceDirty,
        settlement_lag: _bondCalcSettlementLag,
        custom_infl_monthly: customInfl,
        custom_monthly: customMonthly,
      }),
    });
    if (res.ok) { const d = await res.json(); rows = d.rows || []; }
  } catch (_) {}
  if (!rows.length) {
    body.innerHTML = `<tr><td colspan="4" class="bond-calc-help">Sin datos suficientes (REM/CER no disponibles).</td></tr>`;
    return;
  }
  body.innerHTML = rows.map((r) => `
    <tr>
      <td>${r.label}</td>
      <td>${_bondFmtPct(r.infl_monthly, 2)}</td>
      <td>${_bondFmtPct(r.total_return, 1)}</td>
      <td>${_bondFmtPct(r.tea_nominal, 1)}</td>
    </tr>`).join("");
}

// =====================================================================
// Popup gráfico Sendero BEI vs REM mensual
// =====================================================================
let _senderoChartInstance = null;

function openSenderoChartPopup() {
  const rows = lastSnapshot?.monitors?.find(m => m.id === "bei_sendero")?.rows || [];
  if (!rows.length) return;

  let overlay = document.getElementById("sendero-chart-overlay");
  if (!overlay) {
    overlay = document.createElement("div");
    overlay.id = "sendero-chart-overlay";
    overlay.style.cssText =
      "position:fixed;inset:0;z-index:1100;background:rgba(5,10,30,0.82);" +
      "display:flex;align-items:center;justify-content:center;";
    overlay.innerHTML = `
      <div style="background:var(--panel-bg);border:1px solid var(--panel-border);
                  border-radius:8px;padding:20px;width:min(820px,96vw);
                  max-height:90vh;overflow:auto;position:relative;">
        <header style="display:flex;align-items:center;gap:10px;margin-bottom:14px">
          <h3 style="font-size:12px;font-weight:800;letter-spacing:1px;
                     color:var(--text-white);text-transform:uppercase;flex:1;margin:0">
            Sendero Mensual · BEI vs REM-BCRA
          </h3>
          <button id="sendero-chart-close" type="button"
                  style="background:none;border:none;color:var(--text-dim);
                         font-size:18px;cursor:pointer;padding:0 4px;line-height:1">×</button>
        </header>
        <div style="position:relative;height:380px">
          <canvas id="sendero-chart-canvas"></canvas>
        </div>
        <p style="font-size:9px;color:var(--text-faint);margin-top:8px;font-style:italic">
          REM proyectado (meses sin dato real) = mensualización del YoY 12m · BCRA
        </p>
      </div>`;
    document.body.appendChild(overlay);
    overlay.addEventListener("click", e => { if (e.target === overlay) closeSenderoChartPopup(); });
    overlay.querySelector("#sendero-chart-close").addEventListener("click", closeSenderoChartPopup);
  }

  overlay.style.display = "flex";
  trapFocusIn(overlay);

  if (_senderoChartInstance) { _senderoChartInstance.destroy(); _senderoChartInstance = null; }

  const textFaint = cssv("--text-faint") || "#8893bb";
  const gridColor = (cssv("--panel-border") || "#26345a") + "66";

  const labels  = rows.map(r => r.mes);
  const beiData = rows.map(r => r.bei_mensual);
  const remData = rows.map(r => r.rem_mensual);
  const diffData = rows.map(r => r.diff);

  // Barras coloreadas por signo: verde positivo, rojo negativo
  const barBg     = diffData.map(v => v == null ? "transparent" : v >= 0 ? "#22c55e66" : "#ef444466");
  const barBorder = diffData.map(v => v == null ? "transparent" : v >= 0 ? "#22c55e"   : "#ef4444");

  // REM: sólido donde es dato real, punteado donde es proyectado
  const remSegment = {
    borderDash: ctx => rows[ctx.p1DataIndex]?.rem_projected ? [5, 4] : [],
    borderColor: ctx => rows[ctx.p1DataIndex]?.rem_projected
      ? "#60c8ff88" : "#60c8ff",
  };

  const canvas = overlay.querySelector("#sendero-chart-canvas");
  _senderoChartInstance = new Chart(canvas, {
    type: "bar",
    data: {
      labels,
      datasets: [
        {
          type: "line",
          label: "BEI mensual",
          data: beiData,
          borderColor: "#a78bfa",
          backgroundColor: "transparent",
          borderWidth: 2.5,
          pointRadius: 4,
          pointBackgroundColor: "#a78bfa",
          tension: 0.25,
          spanGaps: false,
          yAxisID: "y",
          order: 1,
        },
        {
          type: "line",
          label: "REM mensual",
          data: remData,
          borderColor: "#60c8ff",
          backgroundColor: "transparent",
          borderWidth: 2.5,
          pointRadius: 4,
          pointBackgroundColor: "#60c8ff",
          tension: 0.25,
          spanGaps: true,
          segment: remSegment,
          yAxisID: "y",
          order: 2,
        },
        {
          type: "bar",
          label: "BEI − REM",
          data: diffData,
          backgroundColor: barBg,
          borderColor: barBorder,
          borderWidth: 1.5,
          borderRadius: 3,
          yAxisID: "y",
          order: 3,
        },
      ],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      plugins: {
        datalabels: { display: false },
        legend: {
          display: true,
          position: "top",
          labels: {
            color: textFaint,
            font: { size: 10, family: "JetBrains Mono" },
            boxWidth: 18,
            padding: 12,
          },
        },
        tooltip: {
          callbacks: {
            label: ctx => {
              const v = ctx.parsed.y;
              const proj = ctx.datasetIndex === 1 && rows[ctx.dataIndex]?.rem_projected ? " *" : "";
              return `${ctx.dataset.label}: ${v != null ? v.toFixed(2) + "%" : "—"}${proj}`;
            },
          },
        },
      },
      scales: {
        x: {
          ticks: {
            color: textFaint,
            font: { size: 10, family: "JetBrains Mono" },
            maxRotation: 0,
          },
          grid: { color: gridColor },
        },
        y: {
          ticks: {
            color: textFaint,
            font: { size: 10 },
            callback: v => Number(v).toFixed(2) + "%",
          },
          grid: { color: gridColor },
        },
      },
    },
  });
}

function closeSenderoChartPopup() {
  const overlay = document.getElementById("sendero-chart-overlay");
  if (overlay) overlay.style.display = "none";
  releaseFocusTrap();
  if (_senderoChartInstance) { _senderoChartInstance.destroy(); _senderoChartInstance = null; }
}

// =====================================================================
// Panel FCI (Fondos Comunes de Inversión · CAFCI)
// Datos diarios servidos por /api/fci (catálogo + matriz de rendimientos
// de estadisticas.cafci.org.ar). Panel autónomo: NO depende del snapshot
// de 5s — fetchea on-load y cuando cambian los filtros / orden / búsqueda.
// =====================================================================

const FCI_PERIOD_COLS = [
  { key: "dias_7",   label: "7d" },
  { key: "mes_1",    label: "1m" },
  { key: "dias_90",  label: "3m" },
  { key: "dias_180", label: "6m" },
  { key: "ytd",      label: "YTD" },
  { key: "meses_12", label: "12m" },
];
const FCI_PERIOD_LABELS = {
  dias_7: "7 días", mes_1: "1 mes", dias_90: "90 días",
  dias_180: "180 días", ytd: "En el año", meses_12: "12 meses",
};
const FCI_DEFAULTS = { tipo: "Mercado de Dinero", moneda: "Peso Argentina" };
const FCI_FETCH_LIMIT = 300;

let _fciState = { tipo: null, moneda: null, q: "", sort: "mes_1", dir: "desc", metric: "tna" };
let _fciInited = false;
let _fciSearchDebounce = null;

function _fciLiq(d) {
  if (d === null || d === undefined) return "–";
  return "T+" + d;
}

async function fetchFci() {
  const p = new URLSearchParams();
  if (_fciState.tipo)   p.set("tipo", _fciState.tipo);
  if (_fciState.moneda) p.set("moneda", _fciState.moneda);
  if (_fciState.q)      p.set("q", _fciState.q);
  p.set("sort", _fciState.sort);
  p.set("dir", _fciState.dir);
  p.set("metric", _fciState.metric);
  p.set("limit", String(FCI_FETCH_LIMIT));
  // Timeout generoso: el primer fetch tras boot puede pagar el JSON de ~3.9MB
  // si el prime en background del server aún no terminó.
  const r = await fetchWithTimeout("/api/fci?" + p.toString(), { cache: "no-store" }, 30000);
  if (!r.ok) throw new Error("HTTP " + r.status);
  return await r.json();
}

async function refreshFci() {
  const panel = document.querySelector(".panel[data-id='fci']");
  if (!panel) return;
  const body = panel.querySelector("[data-role='fci-body']");
  if (body && !body.querySelector("table")) {
    body.innerHTML = `<div class="fci-msg">Cargando fondos…</div>`;
  }
  try {
    const data = await fetchFci();
    _renderFciPanel(panel, data);
  } catch (e) {
    if (body) body.innerHTML =
      `<div class="fci-msg fci-err">No se pudo cargar FCI: ${_escHtml(String(e.message || e))}</div>`;
  }
}

function _fciFillSelect(sel, options, selected) {
  if (!sel || sel.dataset.filled) return;
  sel.innerHTML = (options || [])
    .map((o) => `<option value="${_escHtml(o.value)}">${_escHtml(o.value)} (${o.count})</option>`)
    .join("");
  sel.dataset.filled = "1";
  if (selected && Array.from(sel.options).some((o) => o.value === selected)) {
    sel.value = selected;
  } else if (sel.options.length) {
    // Default no disponible para esta dimensión → tomar el primero y reflejarlo.
    sel.selectedIndex = 0;
  }
}

function _renderFciPanel(panel, data) {
  const meta = data.meta || {};
  const sub = panel.querySelector("[data-role='fci-sub']");
  if (sub) {
    const fb = meta.fecha_base ? fmt.dateAR(meta.fecha_base) : "—";
    const n = (data.funds || []).length;
    const capped = n >= FCI_FETCH_LIMIT ? "+" : "";
    sub.textContent = `Datos al ${fb} · ${n}${capped} fondos · CAFCI`;
  }
  _fciFillSelect(document.getElementById("fci-tipo"), meta.tipo_renta_options, _fciState.tipo);
  _fciFillSelect(document.getElementById("fci-moneda"), meta.moneda_options, _fciState.moneda);
  // Sincronizar el estado con lo que terminó seleccionado (por si el default
  // no existía en las opciones y el select cayó al primero).
  const tipoSel = document.getElementById("fci-tipo");
  const monSel = document.getElementById("fci-moneda");
  if (tipoSel && tipoSel.value) _fciState.tipo = tipoSel.value;
  if (monSel && monSel.value) _fciState.moneda = monSel.value;

  _renderFciTable(panel, data.funds || []);
}

function _renderFciTable(panel, funds) {
  const body = panel.querySelector("[data-role='fci-body']");
  if (!body) return;
  if (!funds.length) {
    body.innerHTML = `<div class="fci-msg">Sin fondos para este filtro.</div>`;
    return;
  }
  const metric = _fciState.metric;
  const table = document.createElement("table");
  table.className = "bonds fci-table";

  const thead = document.createElement("thead");
  const trh = document.createElement("tr");
  const addTh = (label, cls, sortKey) => {
    const th = document.createElement("th");
    th.textContent = label;
    if (cls) th.className = cls;
    if (sortKey) {
      th.classList.add("fci-sortable");
      if (_fciState.sort === sortKey) {
        th.classList.add(_fciState.dir === "asc" ? "sort-asc" : "sort-desc");
      }
      th.title = "Ordenar por " + label;
      th.addEventListener("click", () => _fciSort(sortKey));
    }
    trh.appendChild(th);
  };
  addTh("Fondo", "col-text");
  addTh("Soc. Gerente", "col-text");
  addTh("Liq", "col-text");
  addTh("VCP");
  FCI_PERIOD_COLS.forEach((c) => addTh(c.label, null, c.key));
  thead.appendChild(trh);
  table.appendChild(thead);

  const tbody = document.createElement("tbody");
  funds.forEach((f) => {
    const tr = document.createElement("tr");
    tr.className = "fci-row";
    tr.setAttribute("data-clase-id", f.clase_id);
    tr.title = "Ver detalle de " + (f.fondo_nombre || "");

    const tdN = document.createElement("td");
    tdN.className = "col-text fci-fondo";
    tdN.textContent = f.fondo_nombre || "–";
    if (f.clase_nombre && f.clase_nombre !== f.fondo_nombre) {
      const tag = document.createElement("span");
      tag.className = "fci-clase-tag";
      tag.textContent = f.clase_nombre;
      tdN.appendChild(tag);
    }
    tr.appendChild(tdN);

    const tdS = document.createElement("td");
    tdS.className = "col-text fci-soc";
    tdS.textContent = f.sociedad || "–";
    tr.appendChild(tdS);

    const tdL = document.createElement("td");
    tdL.className = "col-text";
    tdL.textContent = _fciLiq(f.dias_liquidacion);
    tr.appendChild(tdL);

    const tdV = document.createElement("td");
    tdV.textContent = fmt.number(f.vcp, 2);
    tr.appendChild(tdV);

    FCI_PERIOD_COLS.forEach((c) => {
      const td = document.createElement("td");
      const cell = (f.rend && f.rend[c.key]) || {};
      td.textContent = fmt.percent(cell[metric], 2);
      if (c.key === _fciState.sort) td.classList.add("fci-sorted");
      tr.appendChild(td);
    });
    tbody.appendChild(tr);
  });
  table.appendChild(tbody);

  body.innerHTML = "";
  body.appendChild(table);
}

function _fciSort(key) {
  if (_fciState.sort === key) {
    _fciState.dir = _fciState.dir === "asc" ? "desc" : "asc";
  } else {
    _fciState.sort = key;
    _fciState.dir = "desc";
  }
  refreshFci();
}

function initFciPanel() {
  if (_fciInited) return;
  const panel = document.querySelector(".panel[data-id='fci']");
  if (!panel) return;
  _fciInited = true;
  _fciState.tipo = FCI_DEFAULTS.tipo;
  _fciState.moneda = FCI_DEFAULTS.moneda;

  const tipoSel = document.getElementById("fci-tipo");
  const monSel  = document.getElementById("fci-moneda");
  const search  = document.getElementById("fci-search");
  if (tipoSel) tipoSel.addEventListener("change", () => { _fciState.tipo = tipoSel.value || null; refreshFci(); });
  if (monSel)  monSel.addEventListener("change", () => { _fciState.moneda = monSel.value || null; refreshFci(); });
  if (search) {
    search.addEventListener("input", () => {
      clearTimeout(_fciSearchDebounce);
      _fciSearchDebounce = setTimeout(() => { _fciState.q = search.value.trim(); refreshFci(); }, 300);
    });
  }
  panel.querySelectorAll(".fci-metric-btn").forEach((b) => {
    b.addEventListener("click", () => {
      _fciState.metric = b.getAttribute("data-metric");
      panel.querySelectorAll(".fci-metric-btn").forEach((x) => x.classList.toggle("active", x === b));
      refreshFci();
    });
  });
  // Click en una fila → popup de detalle.
  const body = panel.querySelector("[data-role='fci-body']");
  if (body) {
    body.addEventListener("click", (e) => {
      const tr = e.target.closest("tr[data-clase-id]");
      if (tr) openFciDetailPopup(tr.getAttribute("data-clase-id"));
    });
  }
  refreshFci();
}

// ---------- Popup de detalle de FCI (reusa el chrome de bond-popup) ---------
function _ensureFciPopupDom() {
  let overlay = document.getElementById("fci-popup-overlay");
  if (overlay) return overlay;
  overlay = document.createElement("div");
  overlay.id = "fci-popup-overlay";
  overlay.className = "bond-popup-overlay fci-popup-overlay";
  overlay.hidden = true;
  overlay.innerHTML = `
    <div class="bond-popup fci-popup" role="dialog" aria-modal="true" aria-labelledby="fci-popup-title">
      <header class="bond-popup-header">
        <h3 id="fci-popup-title">Fondo</h3>
        <button class="bond-popup-close" type="button" aria-label="Cerrar">×</button>
      </header>
      <div class="bond-popup-body fci-popup-body"></div>
    </div>`;
  document.body.appendChild(overlay);
  overlay.querySelector(".bond-popup-close").addEventListener("click", closeFciPopup);
  overlay.addEventListener("click", (e) => { if (e.target === overlay) closeFciPopup(); });
  overlay.addEventListener("keydown", (e) => { if (e.key === "Escape") closeFciPopup(); });
  return overlay;
}

function closeFciPopup() {
  const overlay = document.getElementById("fci-popup-overlay");
  if (!overlay || overlay.hidden) return;
  overlay.hidden = true;
  releaseFocusTrap();
}

function _fciDetailHtml(f) {
  const metaRows = [
    ["Clase", f.clase_nombre || "–"],
    ["Tipo de renta", f.tipo_renta || "–"],
    ["Moneda", f.moneda || "–"],
    ["Soc. gerente", f.sociedad || "–"],
    ["Liquidación rescates", _fciLiq(f.dias_liquidacion)],
    ["VCP (valor cuotaparte)", fmt.number(f.vcp, 4)],
    ["Datos al", fmt.dateAR(f.fecha_valor)],
  ];
  const metaHtml = metaRows
    .map(([k, v]) => `<tr><th>${_escHtml(k)}</th><td>${_escHtml(String(v))}</td></tr>`)
    .join("");
  const rendHtml = FCI_PERIOD_COLS.map((c) => {
    const r = (f.rend && f.rend[c.key]) || {};
    return `<tr><td class="col-text">${FCI_PERIOD_LABELS[c.key]}</td>` +
           `<td>${fmt.percent(r.tna, 2)}</td><td>${fmt.percent(r.directo, 2)}</td></tr>`;
  }).join("");
  return `
    <div class="fci-detail-grid">
      <div class="fci-detail-card">
        <h4>Datos del fondo</h4>
        <table class="fci-meta-table">${metaHtml}</table>
      </div>
      <div class="fci-detail-card">
        <h4>Rendimientos</h4>
        <table class="bonds fci-rend-table">
          <thead><tr><th class="col-text">Período</th><th>TNA</th><th>Directo</th></tr></thead>
          <tbody>${rendHtml}</tbody>
        </table>
      </div>
    </div>
    <p class="fci-detail-note">Fuente: CAFCI · TNA sobre días corridos. Rendimientos
    pasados no garantizan rendimientos futuros.</p>`;
}

async function openFciDetailPopup(claseId) {
  if (!claseId) return;
  const overlay = _ensureFciPopupDom();
  overlay.hidden = false;
  trapFocusIn(overlay);
  const bodyEl = overlay.querySelector(".fci-popup-body");
  overlay.querySelector("#fci-popup-title").textContent = "Cargando…";
  bodyEl.innerHTML = `<div class="bond-loading">Cargando detalle…</div>`;
  let f;
  try {
    const r = await fetch(`/api/fci/${encodeURIComponent(claseId)}`, { cache: "no-store" });
    if (!r.ok) throw new Error("HTTP " + r.status);
    f = await r.json();
  } catch (e) {
    bodyEl.innerHTML = `<div class="bond-error">No se pudo cargar: ${_escHtml(String(e.message || e))}</div>`;
    return;
  }
  overlay.querySelector("#fci-popup-title").textContent = f.fondo_nombre || "Fondo";
  bodyEl.innerHTML = _fciDetailHtml(f);
}

function _injectColumnButtons() {
  document.querySelectorAll(".grid-stack-item").forEach((item) => {
    const panel = item.querySelector(".panel");
    if (!panel) return;
    const monitorId = panel.getAttribute("data-id");
    const header = panel.querySelector(".panel-header");
    if (!header || header.querySelector(".cols-btn-wrap")) return;

    const wrap = document.createElement("div");
    wrap.className = "cols-btn-wrap";

    // Botones de curva y columnas: solo para paneles tabulares (no charts).
    // El panel FCI maneja sus propias columnas/filtros → omitir el botón ▦.
    if (!panel.classList.contains("panel-curva") && monitorId !== "fci") {
      // Botón de curva: solo para paneles de bonos (tienen tir + duration).
      if (CURVE_POPUP_SOURCES.has(monitorId)) {
        const curveBtn = document.createElement("button");
        curveBtn.type = "button";
        curveBtn.className = "panel-curve-btn";
        curveBtn.title = "Ver curva TIR vs Duration";
        curveBtn.setAttribute("aria-label", "Ver curva TIR vs Duration");
        curveBtn.innerHTML =
          '<svg viewBox="0 0 16 16" width="13" height="13" fill="none" ' +
          'stroke="currentColor" stroke-width="1.6" stroke-linecap="round" ' +
          'stroke-linejoin="round" aria-hidden="true">' +
          '<polyline points="1.5,12 5,8.5 8.5,10 14,3"/>' +
          '<line x1="1.5" y1="14" x2="14" y2="14"/>' +
          '</svg>';
        curveBtn.addEventListener("click", (e) => {
          e.stopPropagation();
          const label = panel.querySelector("h2")?.textContent?.trim() || monitorId;
          openCurvePopup(monitorId, label);
        });
        wrap.appendChild(curveBtn);
      }

      // Botón de gráfico sendero BEI vs REM.
      if (monitorId === "bei_sendero") {
        const senderoChartBtn = document.createElement("button");
        senderoChartBtn.type = "button";
        senderoChartBtn.className = "panel-curve-btn";
        senderoChartBtn.title = "Ver gráfico BEI vs REM mensual";
        senderoChartBtn.setAttribute("aria-label", "Ver gráfico BEI vs REM");
        senderoChartBtn.innerHTML =
          '<svg viewBox="0 0 16 16" width="13" height="13" fill="none" ' +
          'stroke="currentColor" stroke-width="1.6" stroke-linecap="round" ' +
          'stroke-linejoin="round" aria-hidden="true">' +
          '<rect x="1.5" y="7" width="3" height="7"/>' +
          '<rect x="6.5" y="3" width="3" height="11"/>' +
          '<rect x="11.5" y="5" width="3" height="9"/>' +
          '<line x1="1" y1="1.5" x2="15" y2="1.5"/>' +
          '</svg>';
        senderoChartBtn.addEventListener("click", (e) => {
          e.stopPropagation();
          openSenderoChartPopup();
        });
        wrap.appendChild(senderoChartBtn);
      }

      // Botón de curva de futuros: TNA vs días al vencimiento.
      if (monitorId === "futuros") {
        const futBtn = document.createElement("button");
        futBtn.type = "button";
        futBtn.className = "panel-curve-btn";
        futBtn.title = "Ver curva TNA vs días al vto";
        futBtn.setAttribute("aria-label", "Ver curva TNA vs días al vencimiento");
        futBtn.innerHTML =
          '<svg viewBox="0 0 16 16" width="13" height="13" fill="none" ' +
          'stroke="currentColor" stroke-width="1.6" stroke-linecap="round" ' +
          'stroke-linejoin="round" aria-hidden="true">' +
          '<polyline points="1.5,12 5,8.5 8.5,10 14,3"/>' +
          '<line x1="1.5" y1="14" x2="14" y2="14"/>' +
          '</svg>';
        futBtn.addEventListener("click", (e) => {
          e.stopPropagation();
          const label = panel.querySelector("h2")?.textContent?.trim() || monitorId;
          openCurvePopup(monitorId, label, renderFuturosChart);
        });
        wrap.appendChild(futBtn);
      }

      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "panel-cols-btn";
      btn.title = "Mostrar/ocultar columnas";
      btn.setAttribute("aria-label", "Mostrar u ocultar columnas");
      btn.setAttribute("aria-haspopup", "true");
      btn.setAttribute("aria-expanded", "false");
      btn.innerHTML = "▦";

      const pop = document.createElement("div");
      pop.className = "cols-popover";
      pop.setAttribute("role", "menu");
      pop.hidden = true;

      btn.addEventListener("click", (e) => {
        e.stopPropagation();
        const wasHidden = pop.hidden;
        // Cerrar cualquier otro popover abierto antes de abrir este.
        document.querySelectorAll(".cols-popover").forEach((p) => {
          if (p !== pop) p.hidden = true;
        });
        document.querySelectorAll(".panel-cols-btn").forEach((b) => {
          if (b !== btn) b.setAttribute("aria-expanded", "false");
        });
        if (wasHidden) {
          _buildColsPopover(pop, monitorId);
          pop.hidden = false;
          btn.setAttribute("aria-expanded", "true");
          const rect = btn.getBoundingClientRect();
          pop.style.top = rect.bottom + 4 + "px";
          pop.style.right = Math.max(4, window.innerWidth - rect.right) + "px";
          pop.style.left = "auto";
        } else {
          pop.hidden = true;
          btn.setAttribute("aria-expanded", "false");
        }
      });

      wrap.appendChild(btn);
      wrap.appendChild(pop);
    }

    // Botón × para cerrar el panel — disponible en TODOS los paneles.
    // Usa setPanelVisible() para persistir en localStorage y permitir
    // restaurar desde ⚙ Layout.
    const gsId = item.getAttribute("gs-id") || monitorId;
    const closeBtn = document.createElement("button");
    closeBtn.type = "button";
    closeBtn.className = "panel-close-btn";
    closeBtn.title = "Ocultar panel (restaurar desde ⚙ Layout)";
    closeBtn.setAttribute("aria-label", "Cerrar panel");
    closeBtn.textContent = "×";
    closeBtn.addEventListener("click", (e) => {
      e.stopPropagation();
      setPanelVisible(gsId, false);
    });
    wrap.appendChild(closeBtn);

    header.appendChild(wrap);
  });

  // Cerrar al hacer click fuera o ESC — bind único por documento.
  if (!document._colsBound) {
    const closeAllColsPopovers = () => {
      document.querySelectorAll(".cols-popover").forEach((p) => (p.hidden = true));
      document.querySelectorAll(".panel-cols-btn").forEach((b) => b.setAttribute("aria-expanded", "false"));
    };
    document.addEventListener("click", closeAllColsPopovers);
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape") {
        closeAllColsPopovers();
        closeCurvePopup();
        closeBondPopup();
        closeFciPopup();
        closeStockChartPopup();
      }
    });
    // Delegated handler para tickers clickeables. Captura clicks dentro de
    // cualquier panel — fast enough, no instalamos handlers por celda.
    document.addEventListener("click", (e) => {
      const cell = e.target.closest(".ticker-clickable");
      if (!cell) return;
      const t = cell.getAttribute("data-ticker");
      if (t) openBondDetailPopup(t);
    });
    document.addEventListener("click", (e) => {
      const cell = e.target.closest(".stock-ticker-clickable");
      if (!cell) return;
      const t = cell.getAttribute("data-stock-ticker");
      if (t) openStockChartPopup(t);
    });
    document._colsBound = true;
  }
}

// =====================================================================
// Botón de screenshot (Capturar)
// =====================================================================

function downloadCanvas(canvas, filename) {
  const link = document.createElement("a");
  link.href = canvas.toDataURL("image/png");
  link.download = filename;
  link.click();
}

function todayStamp() {
  const d = new Date();
  return `${fmt.dateV4(d)}_${fmt.timeHMS(d).replace(/:/g, "")}`;
}

async function captureFiel() {
  const btn = document.getElementById("btn-capture");
  btn.disabled = true;
  document.body.classList.add("capturing");
  try {
    const canvas = await html2canvas(document.body, {
      backgroundColor: getComputedStyle(document.body).backgroundColor,
      scale: 4,
      useCORS: true,
      logging: false,
    });
    downloadCanvas(canvas, `monitor_${todayStamp()}.png`);
  } catch (e) {
    console.error("capture fallo:", e);
    alert("No se pudo generar la captura: " + e.message);
  } finally {
    document.body.classList.remove("capturing");
    btn.disabled = false;
  }
}


// =====================================================================
// BEI history chart — uses /api/bei_history (CSV-backed daily series)
// =====================================================================

let beiHistoryChart = null;

const BEI_TENOR_COLORS = {
  "3M":  "#3a5fcf",
  "6M":  "#1aa094",
  "9M":  "#cf6f3a",
  "1Y":  "#a045cf",
  "18M": "#cf3a5f",
  "2Y":  "#3a8fcf",
  "3Y":  "#5a5a5a",
};

async function fetchBeiHistory() {
  try {
    const r = await fetchWithTimeout("/api/bei_history", { cache: "no-store" }, 6000);
    if (!r.ok) throw new Error("HTTP " + r.status);
    const j = await r.json();
    lastBeiRows = j.rows || [];
    renderBeiHistory(lastBeiRows);
  } catch (e) {
    console.warn("bei history fetch fallo:", e);
  }
}

function renderBeiHistory(rows) {
  const canvas = document.getElementById("bei-history-chart");
  if (!canvas) return;
  const tsEl = document.getElementById("bei-history-ts");

  if (!rows.length) {
    if (tsEl) tsEl.textContent = "Sin historia aún — corre el monitor BEI al menos una vez.";
    return;
  }

  // Pivot rows -> {fecha: {tenor: bei_spot}}
  const byDate = new Map();
  for (const r of rows) {
    if (!r.fecha || r.bei_spot == null) continue;
    if (!byDate.has(r.fecha)) byDate.set(r.fecha, {});
    byDate.get(r.fecha)[r.tenor_label] = Number(r.bei_spot) * 100;
  }
  const dates = [...byDate.keys()].sort();
  const tenors = ["3M", "6M", "9M", "1Y", "18M", "2Y", "3Y"];

  // With only 1 date, use bigger dots so the data is visible.
  const singleDay = dates.length <= 1;
  const datasets = tenors.map((t) => ({
    label: t,
    data: dates.map((d) => byDate.get(d)[t] ?? null),
    borderColor: BEI_TENOR_COLORS[t] || "#888",
    backgroundColor: BEI_TENOR_COLORS[t] || "#888",
    borderWidth: 2,
    pointRadius: singleDay ? 8 : 2,
    pointHoverRadius: singleDay ? 10 : 5,
    tension: 0.25,
    spanGaps: true,
  }));

  if (beiHistoryChart) {
    beiHistoryChart.data.labels = dates;
    beiHistoryChart.data.datasets = datasets;
    beiHistoryChart.update();
  } else {
    beiHistoryChart = new Chart(canvas.getContext("2d"), {
      type: "line",
      data: { labels: dates, datasets },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        plugins: {
          legend: { position: "right", labels: { color: CHART.TEXT_DIM, font: { size: 11 } } },
          tooltip: { callbacks: { label: (ctx) => `${ctx.dataset.label}: ${fmt.number(ctx.parsed.y, 2)}%` } },
        },
        scales: {
          x: { ticks: { color: CHART.TEXT_DIM, maxTicksLimit: 12 }, grid: { color: CHART.GRID } },
          y: {
            ticks: { color: CHART.TEXT_DIM, callback: (v) => `${v}%` },
            grid: { color: CHART.GRID },
            title: { display: true, text: "BEI spot (TEA %)", color: CHART.TEXT_DIM },
          },
        },
      },
    });
  }
  if (tsEl) {
    if (dates.length === 1) {
      tsEl.textContent = `1 fecha (${dates[0]}) — los puntos son visibles; la línea aparece al acumular días`;
    } else {
      tsEl.textContent = `${dates.length} fechas en la serie`;
    }
  }
}

// =====================================================================
// ABM modal — alta/baja/modificación de instrumentos
// =====================================================================

let abmSchemas = null;        // {Sheet: {label, fields: [...]}}
let abmCurrentSheet = null;   // sheet currently rendered in the form
let abmEditingTicker = null;  // ticker being edited (null = new)
// Cashflows editables del ticker actual. Modificados en lugar via inputs.
// Source = "sheet" (explicit en master) | "synth" (engine fallback) | "empty".
let abmCashflows = [];        // [{date, amortization, interest}]
let abmCashflowsSource = "empty";

// Sheets que renderizan el panel editable de cashflows. Otros tipos (Tasa_Fija
// usa Cashflows_Fija con shape distinto, no soportado por ahora) lo ocultan.
const ABM_CASHFLOW_SHEETS = new Set(["Soberanos", "CER"]);

function abmOpen() {
  const overlay = document.getElementById("abm-overlay");
  overlay.hidden = false;
  trapFocusIn(overlay);
  document.body.style.overflow = "hidden";
  if (!abmSchemas) abmLoadSchemas();
  abmRefreshTickerList();
  abmSetStatus("", null);
}

function abmClose() {
  document.getElementById("abm-overlay").hidden = true;
  document.body.style.overflow = "";
  releaseFocusTrap();
  abmResetForm();
}

async function abmLoadSchemas() {
  try {
    const r = await fetch("/api/abm/schemas");
    const j = await r.json();
    abmSchemas = j.schemas || {};
    const sel = document.getElementById("abm-sheet-select");
    sel.innerHTML = `<option value="">— Elegí una hoja —</option>`;
    for (const [sheet, meta] of Object.entries(abmSchemas)) {
      const opt = document.createElement("option");
      opt.value = sheet;
      opt.textContent = meta.label || sheet;
      sel.appendChild(opt);
    }
  } catch (e) {
    abmSetStatus("Error cargando schemas: " + e.message, "err");
  }
}

async function abmRefreshTickerList() {
  try {
    const r = await fetch("/api/abm/instruments");
    const j = await r.json();
    const dl = document.getElementById("abm-ticker-list");
    dl.innerHTML = "";
    for (const it of j.items || []) {
      const opt = document.createElement("option");
      opt.value = it.ticker;
      opt.label = it.sheet;
      dl.appendChild(opt);
    }
  } catch (e) {
    abmSetStatus("Error cargando lista: " + e.message, "err");
  }
}

function abmResetForm() {
  document.getElementById("abm-search-input").value = "";
  document.getElementById("abm-sheet-picker").hidden = true;
  document.getElementById("abm-form").hidden = true;
  document.getElementById("abm-fields").innerHTML = "";
  document.getElementById("abm-sheet-select").value = "";
  document.getElementById("abm-delete").style.display = "inline-block";
  abmCurrentSheet = null;
  abmEditingTicker = null;
}

function abmSetStatus(text, kind) {
  const el = document.getElementById("abm-status");
  el.textContent = text || "";
  el.className = "abm-status" + (kind ? " " + kind : "");
}

// Soberanos (BONAR/GLOBAL/BOPREAL) en data912 suelen tener hasta 3 tickers
// por bono — uno por moneda de liquidación. Al dar de alta uno nuevo,
// pedimos los 3 a la vez y damos de alta los que el usuario complete.
const SOBERANO_CURRENCY_VARIANTS = [
  { key: "ticker_pesos", label: "Pesos", help: "ej. AL30 / BPY26 (sin sufijo)" },
  { key: "ticker_mep",   label: "MEP",   help: "ej. AL30D / BPY6D (sufijo D)" },
  { key: "ticker_cable", label: "CABLE", help: "ej. AL30C / BPY6C (sufijo C)" },
];

function abmRenderFields(sheet, values) {
  if (!abmSchemas || !abmSchemas[sheet]) return;
  const meta = abmSchemas[sheet];
  const container = document.getElementById("abm-fields");
  container.innerHTML = "";
  abmCurrentSheet = sheet;

  // Banner de help al tope del form si la hoja lo expone — hoy lo usamos
  // para avisar que los cashflows se sintetizan solos (UX: el usuario no
  // tiene que entrar a editar la hoja Cashflows después del alta).
  if (meta.help) {
    const helpEl = document.createElement("div");
    helpEl.className = "abm-field full abm-sheet-help";
    helpEl.textContent = meta.help;
    container.appendChild(helpEl);
  }

  // Modo multi-moneda: alta NUEVA + sheet Soberanos → render 3 tickers en vez
  // de 1. En edición o en otras hojas (CER, LECAP, etc.) se mantiene el flow
  // single-ticker — esas hojas no tienen el patrón de variants por moneda.
  const isNewSoberano = !abmEditingTicker && sheet === "Soberanos";
  if (isNewSoberano) {
    const group = document.createElement("div");
    group.className = "abm-field full abm-currency-group";
    const title = document.createElement("div");
    title.className = "abm-currency-title";
    title.textContent = "Tickers por moneda (completá al menos uno; dejá vacío los que no apliquen)";
    group.appendChild(title);
    const row = document.createElement("div");
    row.className = "abm-currency-row";
    for (const v of SOBERANO_CURRENCY_VARIANTS) {
      const cell = document.createElement("div");
      cell.className = "abm-currency-cell";
      const lbl = document.createElement("label");
      lbl.setAttribute("for", "abm-cur-" + v.key);
      lbl.textContent = v.label;
      cell.appendChild(lbl);
      const inp = document.createElement("input");
      inp.id = "abm-cur-" + v.key;
      inp.type = "text";
      inp.dataset.multiTicker = v.label;
      inp.placeholder = v.label;
      inp.autocomplete = "off";
      inp.style.textTransform = "uppercase";
      cell.appendChild(inp);
      const hint = document.createElement("span");
      hint.className = "hint";
      hint.textContent = v.help;
      cell.appendChild(hint);
      row.appendChild(cell);
    }
    group.appendChild(row);
    container.appendChild(group);
  }

  for (const f of meta.fields) {
    // En modo multi-moneda, los inputs ticker + short_name los reemplaza el
    // grupo de arriba (short_name = ticker, hace match 1:1).
    if (isNewSoberano && (f.key === "ticker" || f.key === "short_name")) continue;

    const wrap = document.createElement("div");
    wrap.className = "abm-field";
    if (f.classes) wrap.dataset.classes = f.classes.join(",");
    if (f.show_if_amort) wrap.dataset.showIfAmort = "1";
    const lbl = document.createElement("label");
    lbl.setAttribute("for", "abm-f-" + f.key);
    lbl.textContent = f.label;
    if (f.required) {
      const r = document.createElement("span"); r.className = "req"; r.textContent = " *";
      lbl.appendChild(r);
    }
    wrap.appendChild(lbl);

    let input;
    const initial = values && (values[f.key] != null) ? String(values[f.key]) : "";
    if (f.type === "select") {
      input = document.createElement("select");
      for (const opt of (f.options || [])) {
        const o = document.createElement("option");
        o.value = opt; o.textContent = opt || "(vacío)";
        input.appendChild(o);
      }
      input.value = initial;
    } else {
      input = document.createElement("input");
      input.type = f.type === "number" ? "number" : (f.type === "date" ? "date" : "text");
      if (f.type === "number") input.inputMode = "decimal";
      if (f.step) input.step = f.step;
      // For date: convert YYYY-MM-DD or DD/MM/YYYY input to YYYY-MM-DD for input[type=date]
      if (f.type === "date" && initial) {
        input.value = abmNormalizeDate(initial);
      } else {
        input.value = initial;
      }
    }
    input.id = "abm-f-" + f.key;
    input.dataset.key = f.key;
    if (f.required) {
      input.required = true;
      input.setAttribute("aria-required", "true");
    }

    if (f.help) {
      const h = document.createElement("span");
      h.className = "hint";
      h.id = "abm-help-" + f.key;
      h.textContent = f.help;
      input.setAttribute("aria-describedby", h.id);
      wrap.appendChild(input);
      wrap.appendChild(h);
    } else {
      wrap.appendChild(input);
    }
    container.appendChild(wrap);
  }
  document.getElementById("abm-form").hidden = false;

  // Tasa_Fija: visibilidad dinámica según clase y tipo de amortización.
  if (sheet === "Tasa_Fija") {
    _abmUpdateConditionalFields(container);
    ["clase", "tipo amortizacion"].forEach(key => {
      const el = container.querySelector(`[data-key="${key}"]`);
      if (el) el.addEventListener("change", () => _abmUpdateConditionalFields(container));
    });
    // Pre-fill automático desde ArgentinaDatos al salir del campo ticker (solo alta nueva).
    if (!abmEditingTicker) {
      const tickerEl = container.querySelector('[data-key="ticker"]');
      if (tickerEl) tickerEl.addEventListener("blur", _abmPrefillFromArgentinaDatos);
    }
  }
}

async function _abmPrefillFromArgentinaDatos(evt) {
  const t = evt.target.value.trim().toUpperCase();
  if (!t) return;
  try {
    const r = await fetch("/api/letras/prefill/" + encodeURIComponent(t));
    if (!r.ok) return;
    const j = await r.json();
    const _fill = (key, val) => {
      const el = document.getElementById("abm-f-" + key);
      if (el && !el.value && val) el.value = val;
    };
    _fill("fecha_emision", j.fecha_emision);
    _fill("fecha_pago",    j.fecha_pago);
    if (j.tem_licit != null) {
      const el = document.getElementById("abm-f-tem_licit");
      if (el && !el.value) el.value = j.tem_licit;
    }
    const temPct = j.tem_licit != null ? " — TEM " + (j.tem_licit * 100).toFixed(4) + "%" : "";
    abmSetStatus("Pre-llenado desde ArgentinaDatos ✓" + temPct, "ok");
  } catch (_) {}
}

function _abmUpdateConditionalFields(container) {
  // 1. Con clase vacía → ocultar todo lo que tenga data-classes (solo Ticker+Clase visibles).
  //    Con clase elegida → mostrar solo los campos cuya lista de classes la incluye.
  const claseEl = container.querySelector('[data-key="clase"]');
  const clase = (claseEl ? claseEl.value : "").toUpperCase();
  container.querySelectorAll(".abm-field[data-classes]").forEach(wrap => {
    wrap.hidden = !clase || !wrap.dataset.classes.split(",").includes(clase);
  });
  // 2. Campos de amortización: además requieren tipo_amortizacion = "amortizing".
  const amortEl = container.querySelector('[data-key="tipo amortizacion"]');
  const amortWrap = amortEl ? amortEl.closest(".abm-field") : null;
  const isAmortizing = amortWrap && !amortWrap.hidden && amortEl.value === "amortizing";
  container.querySelectorAll("[data-show-if-amort]").forEach(wrap => {
    if (!wrap.hidden) wrap.hidden = !isAmortizing;
  });
}

// ---------- Cashflow editor ----------

function abmShowCashflowsPanel(visible) {
  const p = document.getElementById("abm-cashflows-panel");
  if (!p) return;
  p.hidden = !visible;
}

function abmSetCashflows(rows, source) {
  // Filtramos a futuro: los pagos pasados no influyen en TIR/MD/Convexidad
  // (el engine los descarta de todas formas). Mostrar sólo el "próximo pago
  // en adelante" reduce ruido y deja la tabla acotada a lo que importa.
  // Si el bono ya venció, la tabla queda vacía (mensaje empty-state).
  const todayISO = new Date().toISOString().slice(0, 10);
  const raw = (rows || []).map((cf) => ({
    date: String(cf.date || "").slice(0, 10),
    amortization: Number(cf.amortization) || 0,
    interest: Number(cf.interest) || 0,
  }));
  const totalCount = raw.length;
  abmCashflows = raw
    .filter((cf) => cf.date && cf.date >= todayISO)
    .sort((a, b) => a.date.localeCompare(b.date));
  abmCashflowsSource = source || "empty";
  abmRenderCashflowsTable();
  const srcEl = document.getElementById("abm-cashflows-source");
  if (srcEl) {
    const futCount = abmCashflows.length;
    const droppedCount = totalCount - futCount;
    const suffix = droppedCount > 0
      ? ` (${futCount} futuros · ${droppedCount} pasados ocultos)`
      : (futCount > 0 ? ` (${futCount} futuros)` : "");
    const base = {
      sheet:  "explícito (hoja Cashflows)",
      synth:  "generado on-the-fly · editá y guardá para fijarlos",
      empty:  "sin datos — completá o regenerá desde params",
    }[abmCashflowsSource] || "";
    // Caso especial: source tiene datos pero TODO fue al pasado (bono vencido).
    if (totalCount > 0 && futCount === 0) {
      srcEl.textContent = "bono sin pagos futuros — vencido";
      srcEl.className = "abm-cashflows-source src-empty";
    } else {
      srcEl.textContent = base + suffix;
      srcEl.className = "abm-cashflows-source src-" + abmCashflowsSource;
    }
  }
}

function abmRenderCashflowsTable() {
  const tbody = document.getElementById("abm-cashflows-tbody");
  if (!tbody) return;
  tbody.innerHTML = "";

  // Empty-state: sin filas (bono vencido o nada cargado todavía).
  if (!abmCashflows.length) {
    const tr = document.createElement("tr");
    const td = document.createElement("td");
    td.colSpan = 7;
    td.className = "abm-cf-empty";
    td.textContent = "Sin pagos futuros. Tocá ⟳ Regenerar o + Fila para empezar.";
    tr.appendChild(td);
    tbody.appendChild(tr);
    abmUpdateCashflowsTotals();
    return;
  }

  // VR Cartera (residual post-pago): empieza en sum(amorts) y descuenta
  // el amort de cada fila. Última fila debería quedar en 0 si el bono se
  // amortiza por completo en este slice de cashflows.
  const totalAmort = abmCashflows.reduce((s, c) => s + (Number(c.amortization) || 0), 0);
  let running = totalAmort;

  abmCashflows.forEach((cf, idx) => {
    const tr = document.createElement("tr");
    tr.dataset.idx = String(idx);

    const amort = Number(cf.amortization) || 0;
    const interest = Number(cf.interest) || 0;
    running -= amort;
    const vr = running;
    const total = amort + interest;
    const obs = amort > 0 ? "Renta + Amort." : (interest > 0 ? "Renta" : "—");

    // Editables (dblclick → input):
    tr.appendChild(_cfCell("date", cf.date, idx));
    // Computado VR Cartera (read-only):
    tr.appendChild(_cfComputed("cf-vr", _cfFormatNum(vr)));
    // Editable Renta Efect:
    tr.appendChild(_cfCell("interest", cf.interest, idx));
    // Editable Amortización:
    tr.appendChild(_cfCell("amortization", cf.amortization, idx));
    // Computado Obs:
    tr.appendChild(_cfComputed("cf-obs", obs));
    // Computado Total:
    tr.appendChild(_cfComputed("cf-total", _cfFormatNum(total)));

    const actCell = document.createElement("td");
    actCell.className = "cf-actions";
    const delBtn = document.createElement("button");
    delBtn.type = "button";
    delBtn.className = "abm-cf-del";
    delBtn.title = "Eliminar fila";
    delBtn.textContent = "×";
    delBtn.addEventListener("click", () => {
      abmCashflows.splice(idx, 1);
      abmRenderCashflowsTable();
    });
    actCell.appendChild(delBtn);
    tr.appendChild(actCell);

    tbody.appendChild(tr);
  });
  abmUpdateCashflowsTotals();
}

function _cfComputed(extraClass, text) {
  const td = document.createElement("td");
  td.className = "cf-computed " + extraClass;
  td.textContent = text;
  return td;
}

// ---- celdas display-as-text + dblclick para editar ----
function _cfFormatDate(value) {
  if (!value) return "—";
  const m = String(value).match(/^(\d{4})-(\d{2})-(\d{2})/);
  return m ? `${m[3]}/${m[2]}/${m[1]}` : String(value);
}
function _cfFormatNum(value) {
  const n = Number(value) || 0;
  // Conservar hasta 4 decimales si los necesita; mínimo 2.
  const needs4 = Math.abs(n - Math.round(n * 100) / 100) > 1e-9;
  return n.toFixed(needs4 ? 4 : 2);
}
function _cfDisplay(field, value) {
  return field === "date" ? _cfFormatDate(value) : _cfFormatNum(value);
}

function _cfCell(field, value, idx) {
  const td = document.createElement("td");
  td.className = "cf-cell cf-" + field;
  td.textContent = _cfDisplay(field, value);
  td.title = "Doble click para editar";
  td.tabIndex = 0;
  td.addEventListener("dblclick", () => _cfEditCell(td, field, idx));
  // Atajo: Enter cuando la celda tiene foco también dispara edición.
  td.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === "F2") {
      e.preventDefault();
      _cfEditCell(td, field, idx);
    }
  });
  return td;
}

function _cfEditCell(td, field, idx) {
  if (td.querySelector("input")) return;  // ya en modo edición
  const original = abmCashflows[idx][field];
  const inp = document.createElement("input");
  if (field === "date") {
    inp.type = "date";
    inp.value = String(original || "").slice(0, 10);
  } else {
    inp.type = "number";
    inp.step = "0.0001";
    inp.value = original;
  }
  td.textContent = "";
  td.appendChild(inp);
  inp.focus();
  if (field !== "date") inp.select();

  let done = false;
  const finish = (newVal) => {
    if (done) return;
    done = true;
    abmCashflows[idx][field] = newVal;
    // Full re-render: editar amort cambia VR Cartera + Total + Obs de la fila
    // y VR Cartera de TODAS las filas siguientes. Editar interest cambia
    // Total + Obs. Editar date cambia ordenamiento implícito. Re-render
    // mantiene todo coherente sin lógica parcial.
    abmRenderCashflowsTable();
  };
  inp.addEventListener("blur", () => {
    finish(field === "date" ? inp.value : (Number(inp.value) || 0));
  });
  inp.addEventListener("keydown", (e) => {
    if (e.key === "Enter") {
      e.preventDefault();
      finish(field === "date" ? inp.value : (Number(inp.value) || 0));
    } else if (e.key === "Escape") {
      e.preventDefault();
      done = true;
      td.textContent = _cfDisplay(field, original);
    }
  });
}

function abmUpdateCashflowsTotals() {
  const tA = document.getElementById("abm-cf-total-amort");
  const tI = document.getElementById("abm-cf-total-interest");
  const tP = document.getElementById("abm-cf-total-pago");
  if (!tA || !tI) return;
  const sumA = abmCashflows.reduce((s, c) => s + (Number(c.amortization) || 0), 0);
  const sumI = abmCashflows.reduce((s, c) => s + (Number(c.interest) || 0), 0);
  tA.textContent = sumA.toFixed(2);
  tI.textContent = sumI.toFixed(2);
  if (tP) tP.textContent = (sumA + sumI).toFixed(2);
}

async function abmCashflowsRegenerate() {
  // POST los fields actuales del form a /api/abm/preview_cashflows y
  // reemplaza la tabla con el synth. NO toca el master Excel.
  const fields = {};
  document.querySelectorAll("#abm-fields [data-key]").forEach((el) => {
    fields[el.dataset.key] = el.value;
  });
  // En modo multi-moneda no hay un "ticker" único en fields — el synth
  // ignora ticker, sólo usa cupon/freq/amort. Fine sin él.
  try {
    const r = await fetch("/api/abm/preview_cashflows", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ fields }),
    });
    const j = await r.json();
    if (j.error) { abmSetStatus("Regenerar falló: " + j.error, "err"); return; }
    abmSetCashflows(j.cashflows || [], "synth");
    abmSetStatus(`Regenerados ${(j.cashflows||[]).length} flujos`, "ok");
  } catch (e) {
    abmSetStatus("Error regenerando: " + e.message, "err");
  }
}

function abmCashflowsAddRow() {
  // Si hay flujos previos, copiar la última fecha + 6 meses como sugerencia.
  let suggDate = "";
  if (abmCashflows.length) {
    const last = abmCashflows[abmCashflows.length - 1].date;
    if (last) {
      const d = new Date(last + "T00:00:00");
      if (!isNaN(d)) {
        d.setMonth(d.getMonth() + 6);
        suggDate = d.toISOString().slice(0, 10);
      }
    }
  }
  abmCashflows.push({ date: suggDate, amortization: 0, interest: 0 });
  abmRenderCashflowsTable();
}

function abmNormalizeDate(s) {
  // Accepts "YYYY-MM-DD", "DD/MM/YYYY", "YYYY-MM-DD HH:MM:SS"; returns "YYYY-MM-DD"
  if (!s) return "";
  s = String(s).trim();
  const isoMatch = s.match(/^(\d{4})-(\d{2})-(\d{2})/);
  if (isoMatch) return `${isoMatch[1]}-${isoMatch[2]}-${isoMatch[3]}`;
  const dmyMatch = s.match(/^(\d{1,2})[\/\-](\d{1,2})[\/\-](\d{4})/);
  if (dmyMatch) {
    const dd = dmyMatch[1].padStart(2, "0");
    const mm = dmyMatch[2].padStart(2, "0");
    return `${dmyMatch[3]}-${mm}-${dd}`;
  }
  return s;
}

async function abmLoad() {
  const t = document.getElementById("abm-search-input").value.trim().toUpperCase();
  if (!t) {
    abmSetStatus("Ingresá un ticker para cargar", "err");
    return;
  }
  abmSetStatus("Cargando " + t + "…", null);
  try {
    const r = await fetch("/api/abm/instrument/" + encodeURIComponent(t));
    if (r.status === 404) {
      abmSetStatus(`Ticker ${t} no existe — usá "+ Nueva especie" para crearlo`, "err");
      return;
    }
    const j = await r.json();
    if (j.error) throw new Error(j.error);
    abmEditingTicker = t;
    document.getElementById("abm-sheet-picker").hidden = true;
    abmRenderFields(j.sheet, j.fields);
    // Cashflows: traemos los del response (sheet o synth fallback).
    if (ABM_CASHFLOW_SHEETS.has(j.sheet)) {
      abmSetCashflows(j.cashflows || [], j.cashflows_source || "empty");
      abmShowCashflowsPanel(true);
    } else {
      abmShowCashflowsPanel(false);
      abmCashflows = []; abmCashflowsSource = "empty";
    }
    document.getElementById("abm-delete").style.display = "inline-block";
    abmSetStatus(`Editando ${t} (${j.sheet})`, "ok");
  } catch (e) {
    abmSetStatus("Error: " + e.message, "err");
  }
}

function abmNew() {
  abmEditingTicker = null;
  document.getElementById("abm-search-input").value = "";
  document.getElementById("abm-sheet-picker").hidden = false;
  document.getElementById("abm-form").hidden = true;
  document.getElementById("abm-fields").innerHTML = "";
  document.getElementById("abm-delete").style.display = "none";
  abmShowCashflowsPanel(false);
  abmCashflows = []; abmCashflowsSource = "empty";
  abmSetStatus("Elegí una hoja y completá los campos", null);
}

function abmOnSheetChange(e) {
  const sheet = e.target.value;
  if (!sheet) {
    document.getElementById("abm-form").hidden = true;
    abmShowCashflowsPanel(false);
    return;
  }
  abmRenderFields(sheet, {});
  // Alta nueva: cashflows arranca vacío. El usuario completa fields y
  // toca "Regenerar" o agrega filas a mano.
  if (ABM_CASHFLOW_SHEETS.has(sheet)) {
    abmSetCashflows([], "empty");
    abmShowCashflowsPanel(true);
  } else {
    abmShowCashflowsPanel(false);
  }
}

async function abmSave(e) {
  e.preventDefault();
  if (!abmCurrentSheet) {
    abmSetStatus("Falta elegir hoja", "err");
    return;
  }

  // Modo multi-moneda: alta nueva + Soberanos. Recolectamos los 3 inputs de
  // ticker y posteamos un save por cada ticker no-vacío con los mismos
  // campos comunes. short_name se setea = ticker.
  const isNewSoberano = !abmEditingTicker && abmCurrentSheet === "Soberanos";
  if (isNewSoberano) {
    const tickerInputs = Array.from(
      document.querySelectorAll("#abm-fields [data-multi-ticker]"),
    );
    const tickers = tickerInputs
      .map((el) => ({ label: el.dataset.multiTicker, value: el.value.trim().toUpperCase() }))
      .filter((x) => x.value);
    if (!tickers.length) {
      abmSetStatus("Completá al menos un ticker (Pesos / MEP / CABLE)", "err");
      return;
    }
    const commonFields = {};
    document.querySelectorAll("#abm-fields [data-key]").forEach((el) => {
      commonFields[el.dataset.key] = el.value;
    });
    abmSetStatus(`Guardando ${tickers.length} variant(es)…`, null);
    const created = [];
    const errors = [];
    // En multi-moneda, los cashflows son idénticos para los 3 variants
    // (mismo bono subyacente). Los enviamos por cada ticker.
    const cfPayload = ABM_CASHFLOW_SHEETS.has(abmCurrentSheet) && abmCashflows.length
      ? abmCashflows : null;
    for (const t of tickers) {
      const fields = { ...commonFields, ticker: t.value, short_name: t.value };
      try {
        const r = await fetch("/api/abm/instrument", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ sheet: abmCurrentSheet, fields, cashflows: cfPayload }),
        });
        const j = await r.json();
        if (!r.ok) throw new Error(j.error || ("HTTP " + r.status));
        created.push(`${t.value} (${t.label})`);
      } catch (err) {
        errors.push(`${t.value}: ${err.message}`);
      }
    }
    abmRefreshTickerList();
    if (errors.length) {
      abmSetStatus(
        `Creados ${created.length}/${tickers.length}. Errores: ${errors.join("; ")}`,
        "err",
      );
    } else {
      abmSetStatus(`Creados ${created.length} ticker(s): ${created.join(", ")}`, "ok");
    }
    return;
  }

  // Flow single-ticker (edición o sheets no-Soberano).
  const fields = {};
  document.querySelectorAll("#abm-fields [data-key]").forEach((el) => {
    fields[el.dataset.key] = el.value;
  });
  if (!fields.ticker || !String(fields.ticker).trim()) {
    abmSetStatus("Ticker es obligatorio", "err");
    return;
  }
  abmSetStatus("Guardando…", null);
  // Cashflows opcionales: si hay tabla visible Y no está vacía, los enviamos
  // para que el backend los persista. Si está vacía y el panel no aplica
  // (otras hojas) o el user los borró todos, pasamos null = preservar synth.
  const cashflows = ABM_CASHFLOW_SHEETS.has(abmCurrentSheet) && abmCashflows.length
    ? abmCashflows : null;
  try {
    const r = await fetch("/api/abm/instrument", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ sheet: abmCurrentSheet, fields, cashflows }),
    });
    const j = await r.json();
    if (!r.ok) throw new Error(j.error || ("HTTP " + r.status));
    let msg = `${j.action.toUpperCase()} ${j.ticker} en ${j.sheet}`;
    if (j.cashflows != null) msg += ` · ${j.cashflows} cashflows`;
    abmSetStatus(msg, "ok");
    abmRefreshTickerList();
    abmEditingTicker = j.ticker;
  } catch (err) {
    abmSetStatus("Error: " + err.message, "err");
  }
}

async function abmDelete() {
  if (!abmEditingTicker) {
    abmSetStatus("Cargá un ticker antes de eliminar", "err");
    return;
  }
  if (!confirm(`¿Eliminar definitivamente ${abmEditingTicker}? Esta acción no se puede deshacer.`)) {
    return;
  }
  abmSetStatus("Eliminando…", null);
  try {
    const r = await fetch("/api/abm/instrument/" + encodeURIComponent(abmEditingTicker), {
      method: "DELETE",
    });
    const j = await r.json();
    if (!r.ok && r.status !== 404) throw new Error(j.error || ("HTTP " + r.status));
    abmSetStatus(`Eliminado ${j.ticker || abmEditingTicker}`, "ok");
    abmResetForm();
    abmRefreshTickerList();
  } catch (err) {
    abmSetStatus("Error: " + err.message, "err");
  }
}

function abmInit() {
  document.getElementById("btn-abm").addEventListener("click", abmOpen);
  document.getElementById("abm-close").addEventListener("click", abmClose);
  document.getElementById("abm-overlay").addEventListener("click", (e) => {
    if (e.target.id === "abm-overlay") abmClose();
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !document.getElementById("abm-overlay").hidden) abmClose();
  });
  document.getElementById("abm-load").addEventListener("click", abmLoad);
  document.getElementById("abm-search-input").addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); abmLoad(); }
  });
  document.getElementById("abm-new").addEventListener("click", abmNew);
  document.getElementById("abm-sheet-select").addEventListener("change", abmOnSheetChange);
  document.getElementById("abm-form").addEventListener("submit", abmSave);
  document.getElementById("abm-delete").addEventListener("click", abmDelete);
  document.getElementById("abm-cf-regen")?.addEventListener("click", abmCashflowsRegenerate);
  document.getElementById("abm-cf-add")?.addEventListener("click", abmCashflowsAddRow);
}

// =====================================================================
// Bootstrap
// =====================================================================

// =====================================================================
// Gridstack: layout drag/resize/responsive con persistencia
// =====================================================================

// v10 = column=60 + cellHeight=5 → step horizontal y vertical 5x más finos
// (~25-30px en pantallas típicas, sensación tipo "redimensionar imagen en
// Photoshop"). El vendor sólo trae CSS .gs-12 / .gs-1, así que inyectamos
// las reglas .gs-60 en tiempo de init — sin eso los ítems quedan invisibles
// con width:8.333% del default 12-col.
const LAYOUT_STORAGE_KEY = "monitor.layout.v11";

// Recorta la altura del .grid-stack al fondo del último ítem visible + un
// margen visual. Sin esto, los paneles ocultos vía display:none siguen
// reservando su slot en el engine de Gridstack y la página queda con un
// hueco enorme al final. Usa !important para ganarle al inline style que
// Gridstack pinta en cada _updateContainerHeight.
const GRID_BOTTOM_BREATHING_PX = 12;
function _recomputeGridHeight() {
  const grid = document.querySelector(".grid-stack");
  if (!grid) return;
  const gridTop = grid.getBoundingClientRect().top + window.scrollY;
  let maxBottom = 0;
  grid.querySelectorAll(".grid-stack-item").forEach((el) => {
    if (getComputedStyle(el).display === "none") return;
    const r = el.getBoundingClientRect();
    const b = r.bottom + window.scrollY - gridTop;
    if (b > maxBottom) maxBottom = b;
  });
  if (maxBottom > 0) {
    grid.style.setProperty(
      "height",
      `${Math.ceil(maxBottom + GRID_BOTTOM_BREATHING_PX)}px`,
      "important",
    );
  }
}

// Inyecta reglas .gs-60 > .grid-stack-item[gs-x|gs-w="N"] para N=1..60.
// El vendor (gridstack.min.css) sólo cubre .gs-12 hardcoded. Sin esto,
// con column=60 todos los ítems caen al 8.333% del fallback .gs-12.
function _injectGs60Stylesheet() {
  if (document.getElementById("gs60-grid-css")) return;
  const cols = 60;
  const lines = [];
  lines.push(`.gs-${cols} > .grid-stack-item { width: ${(100/cols).toFixed(4)}%; }`);
  for (let n = 1; n <= cols; n++) {
    const pct = (n * 100 / cols).toFixed(4);
    lines.push(`.gs-${cols} > .grid-stack-item[gs-x="${n}"] { left: ${pct}%; }`);
    lines.push(`.gs-${cols} > .grid-stack-item[gs-w="${n}"] { width: ${pct}%; }`);
    lines.push(`.gs-${cols} > .grid-stack-item[gs-min-w="${n}"] { min-width: ${pct}%; }`);
    lines.push(`.gs-${cols} > .grid-stack-item[gs-max-w="${n}"] { max-width: ${pct}%; }`);
  }
  const style = document.createElement("style");
  style.id = "gs60-grid-css";
  style.textContent = lines.join("\n");
  document.head.appendChild(style);
}
let gridInstance = null;

function initGridstack() {
  if (typeof GridStack === "undefined") {
    console.error(
      "GridStack no cargó. Verificar que /static/vendor/gridstack/gridstack-all.js " +
      "esté accesible (se sirve localmente; no depende de CDN).",
    );
    // Banner visible para que el usuario vea el problema sin tener que abrir
    // la consola — sin Gridstack los paneles se ven apilados full-width.
    const banner = document.createElement("div");
    banner.style.cssText =
      "background:#d04848;color:#fff;padding:8px 14px;font-weight:600;font-size:13px;text-align:center;";
    banner.textContent =
      "⚠ GridStack no se pudo inicializar — paneles en modo apilado. Revisar consola.";
    document.body.insertBefore(banner, document.body.firstChild);
    return null;
  }

  // Inyectar las reglas .gs-60 ANTES de init — si el grid ya pintó sin
  // CSS de columnas, los ítems quedan en 8.333% y el primer frame se ve mal.
  _injectGs60Stylesheet();

  const grid = GridStack.init({
    // 60 columnas → step horizontal ≈ ancho/60 (1/5 del clásico 12-col).
    // cellHeight=5 → step vertical de 5px. Combinado da redimensionamiento
    // ultra-fino tipo Photoshop. Requiere CSS .gs-60 inyectado arriba.
    column: 60,
    cellHeight: 5,
    margin: 4,
    minRow: 1,
    // float:false (default): los ítems auto-flotan a la posición más
    // cercana arriba. Sin esto, items sin gs-x/gs-y se rompen.
    float: false,
    animate: true,
    draggable: { handle: ".panel-header", scroll: true },
    // Las 8 direcciones de resize: 4 esquinas (nw/ne/sw/se) + 4 lados
    // (n/e/s/w). El usuario puede agarrar cualquier borde.
    resizable: {
      handles: "n, e, s, w, ne, se, sw, nw",
      autoHide: false,
    },
  });

  // Restaurar layout persistido. Tolera SUBSETS: si el cache tiene menos
  // ítems que el HTML (porque algunos están ocultos vía visibility toggle),
  // carga las posiciones que conozca y deja los nuevos en su lugar HTML
  // default. Solo descarta el cache si referencia paneles que ya no existen.
  try {
    const raw = localStorage.getItem(LAYOUT_STORAGE_KEY);
    if (raw) {
      const saved = JSON.parse(raw);
      const currentIds = new Set(
        Array.from(document.querySelectorAll(".grid-stack-item")).map((el) =>
          el.getAttribute("gs-id"),
        ),
      );
      const isLoadable = saved.length > 0 && saved.every((s) => currentIds.has(s.id));
      if (isLoadable) {
        // Paneles nuevos (agregados DESPUÉS de que el usuario guardó su layout)
        // no están en `saved`. GridStack.load() con addAndRemove=true (default)
        // ELIMINA de la grilla los widgets ausentes de la lista → el panel nuevo
        // "desaparece" aunque esté visible en ⚙ Layout. Para evitarlo, mergeamos:
        // a cada id presente en el DOM pero ausente de `saved` le agregamos su
        // posición de fábrica (o un fallback al fondo si no está en FACTORY_LAYOUT).
        const savedIds = new Set(saved.map((s) => s.id));
        const factoryById = Object.fromEntries(FACTORY_LAYOUT.map((f) => [f.id, f]));
        const appended = Array.from(currentIds)
          .filter((id) => id && !savedIds.has(id))
          .map((id) => factoryById[id] || { id, x: 0, y: 100000, w: 30, h: 100 });
        grid.load(saved.concat(appended));
      } else {
        grid.load(FACTORY_LAYOUT);
      }
    } else {
      grid.load(FACTORY_LAYOUT);
    }
  } catch (e) {
    console.warn("Layout cache inválido, ignoro:", e);
    grid.load(FACTORY_LAYOUT);
  }

  // Persistir en cada cambio (move, resize, breakpoint change).
  const save = () => {
    try {
      localStorage.setItem(LAYOUT_STORAGE_KEY, JSON.stringify(grid.save(false)));
    } catch (e) {
      console.warn("No pude guardar layout:", e);
    }
  };
  grid.on("change", save);
  grid.on("resizestop", save);
  grid.on("dragstop", save);

  // Recortar la altura del grid al fondo del último panel visible. Gridstack
  // mantiene en su engine los items con display:none (toggle de visibilidad)
  // y eso deja huecos enormes (~2000px) abajo. Recomputamos en cada cambio.
  grid.on("change", _recomputeGridHeight);
  grid.on("resizestop", _recomputeGridHeight);
  grid.on("dragstop", _recomputeGridHeight);
  // Init: dar tiempo a que Gridstack pinte una vez, luego recortar.
  setTimeout(_recomputeGridHeight, 100);
  setTimeout(_recomputeGridHeight, 500);

  // Redibujar charts Chart.js cuando el panel cambia de tamaño — sin esto
  // el canvas se queda con dimensiones viejas y se ve estirado.
  grid.on("resizestop", (event, el) => {
    const id = el.getAttribute("gs-id");
    if (id === "curva_soberana" && typeof curvaChart !== "undefined" && curvaChart) {
      curvaChart.resize();
    }
    if (id === "bei_history" && typeof beiHistoryChart !== "undefined" && beiHistoryChart) {
      beiHistoryChart.resize();
    }
  });

  // Resize global (rotación de pantalla, debugger abierto) — Chart.js
  // suele recuperarse solo, pero forzar evita layouts pegados.
  window.addEventListener("resize", () => {
    if (typeof curvaChart !== "undefined" && curvaChart) curvaChart.resize();
    if (typeof beiHistoryChart !== "undefined" && beiHistoryChart) beiHistoryChart.resize();
  });

  return grid;
}

// =====================================================================
// Visibilidad de paneles + curvas extra + presets de layout
// =====================================================================

const STORAGE = {
  LAYOUT:       "monitor.layout.v10",
  HIDDEN_COLS:  "monitor.hiddenCols.v1",
  PANEL_VIS:    "monitor.panelVisibility.v3",
  USER_DEFAULT: "monitor.userDefault.v1",
};

// Layout de fábrica: 4 columnas de 15/60, coincide con la foto de referencia.
// Se aplica cuando el usuario no tiene ningún layout guardado (primera carga o
// tras "Layout original") y como destino de "Layout original".
const FACTORY_LAYOUT = [
  // Columna 0 – Bonares + LECAPS
  { id: "bonares",      x: 0,  y: 0,   w: 15, h: 105 },
  { id: "tasa_fija",    x: 0,  y: 105, w: 15, h: 130 },
  // Columna 1 – BOPREALES + Futuros + BEI Sendero
  { id: "bopreales",    x: 15, y: 0,   w: 15, h: 65  },
  { id: "futuros",      x: 15, y: 65,  w: 15, h: 70  },
  { id: "bei_sendero",  x: 15, y: 135, w: 15, h: 100 },
  // Columna 2 – CER + Dolar Linked + BEI Pares
  { id: "cer",          x: 30, y: 0,   w: 15, h: 80  },
  { id: "dolar_linked", x: 30, y: 80,  w: 15, h: 40  },
  { id: "bei_pares",    x: 30, y: 120, w: 15, h: 115 },
  // Columna 3 – Panel Líder + TAMAR
  { id: "panel_lider",  x: 45, y: 0,   w: 15, h: 120 },
  { id: "tamar",        x: 45, y: 120, w: 15, h: 115 },
  // Fila inferior full-width – FCI (tabla ancha: filtros + 6 períodos)
  { id: "fci",          x: 0,  y: 235, w: 60, h: 120 },
];

// Paneles ocultos por default (coincide con los no visibles en la foto de referencia).
const DEFAULT_HIDDEN_PANELS = new Set([
  "curva_soberana",
  "bei_tenor",
  "bei_history",
  "curva_cer",
  "curva_tasa_fija",
  "curva_dolar_linked",
  "curva_tamar",
]);

// Todos los paneles del dashboard (orden para el popover).
const ALL_PANELS = [
  { id: "bonares",            label: "Bonares & Globales" },
  { id: "bopreales",          label: "BOPREALES" },
  { id: "curva_soberana",     label: "Curva Soberana (AL/GD + BPR)" },
  { id: "tasa_fija",          label: "LECAPs & BONCAPs" },
  { id: "fci",                label: "FCI · Fondos (CAFCI)" },
  { id: "cer",                label: "Bonos CER" },
  { id: "dolar_linked",       label: "Dolar Linked" },
  { id: "tamar",              label: "TAMAR Puro" },
  { id: "futuros",            label: "Futuros ROFEX" },
  { id: "panel_lider",        label: "Panel Líder (Acciones)" },
  { id: "bei_tenor",          label: "BEI por Tenor" },
  { id: "bei_sendero",        label: "Sendero Mensual BEI vs REM" },
  { id: "bei_pares",          label: "Método de Pares" },
  { id: "bei_history",        label: "Evolución Diaria BEI" },
  { id: "curva_cer",          label: "Curva CER", curve: true },
  { id: "curva_tasa_fija",    label: "Curva LECAPs", curve: true },
  { id: "curva_dolar_linked", label: "Curva Dolar Linked", curve: true },
  { id: "curva_tamar",        label: "Curva TAMAR", curve: true },
];

let panelVisibility = {};

function loadPanelVisibility() {
  try { panelVisibility = JSON.parse(localStorage.getItem(STORAGE.PANEL_VIS) || "{}"); }
  catch { panelVisibility = {}; }
}

function savePanelVisibility() {
  try { localStorage.setItem(STORAGE.PANEL_VIS, JSON.stringify(panelVisibility)); }
  catch (e) { console.warn("No pude guardar visibilidad de paneles:", e); }
}

function isPanelVisible(id) {
  if (panelVisibility[id] !== undefined) return panelVisibility[id];
  return !DEFAULT_HIDDEN_PANELS.has(id);
}

function applyPanelVisibility() {
  // Versión conservadora: solo display:none/"" sin tocar el engine de
  // Gridstack. Trade-off: los paneles ocultos dejan un hueco en la grilla
  // (el espacio que ocupaban no se reaprovecha hasta arrastrar otros).
  // Lo hacíamos con removeWidget/makeWidget antes pero generaba bugs en
  // el init (hipótesis principal: condición de carrera con makeWidget
  // sobre items ya gestionados por Gridstack).
  document.querySelectorAll(".grid-stack-item").forEach((item) => {
    const id = item.getAttribute("gs-id");
    if (!id) return;
    item.style.display = isPanelVisible(id) ? "" : "none";
  });
  // Re-recortar la altura del grid: si oculté algo abajo, el grid quedó largo.
  _recomputeGridHeight();
}

function setPanelVisible(id, visible) {
  panelVisibility[id] = visible;
  savePanelVisibility();
  applyPanelVisibility();
  // Forzar re-render para que las curvas extra se dibujen al mostrarlas.
  if (lastSnapshot) renderAll(lastSnapshot);
}

// ---------- Acciones de layout ----------

function saveAsUserDefault() {
  if (!gridInstance) return false;
  const snap = {
    ts: new Date().toISOString(),
    layout: gridInstance.save(false),
    hiddenCols: hiddenColsByMonitor,
    panelVisibility: panelVisibility,
  };
  try {
    localStorage.setItem(STORAGE.USER_DEFAULT, JSON.stringify(snap));
    return true;
  } catch (e) {
    console.warn("No pude guardar default del usuario:", e);
    return false;
  }
}

function restoreUserDefault() {
  const raw = localStorage.getItem(STORAGE.USER_DEFAULT);
  if (!raw) return false;
  try {
    const snap = JSON.parse(raw);
    if (snap.layout) localStorage.setItem(STORAGE.LAYOUT, JSON.stringify(snap.layout));
    if (snap.hiddenCols) localStorage.setItem(STORAGE.HIDDEN_COLS, JSON.stringify(snap.hiddenCols));
    if (snap.panelVisibility) localStorage.setItem(STORAGE.PANEL_VIS, JSON.stringify(snap.panelVisibility));
    location.reload();
    return true;
  } catch (e) {
    console.warn("Default corrupto:", e);
    return false;
  }
}

function hasUserDefault() {
  return !!localStorage.getItem(STORAGE.USER_DEFAULT);
}

function restoreFactoryLayout() {
  // Escribe el layout de fábrica explícitamente en localStorage para que la
  // recarga lo encuentre ya listo (no depende de que no haya nada guardado).
  const vis = {};
  DEFAULT_HIDDEN_PANELS.forEach((id) => { vis[id] = false; });
  localStorage.setItem(STORAGE.LAYOUT, JSON.stringify(FACTORY_LAYOUT));
  localStorage.setItem(STORAGE.PANEL_VIS, JSON.stringify(vis));
  localStorage.removeItem(STORAGE.HIDDEN_COLS);
  location.reload();
}

// ---------- Settings popover ----------

let settingsPopover = null;

function buildSettingsPopover() {
  const pop = document.createElement("div");
  pop.className = "settings-popover";
  pop.id = "settings-popover";
  pop.setAttribute("role", "dialog");
  pop.setAttribute("aria-label", "Configuración de paneles y layout");
  pop.hidden = true;
  pop.innerHTML =
    '<div class="settings-popover-header">' +
    '<span>⚙ CONFIGURACIÓN</span>' +
    '<button type="button" class="settings-popover-close" aria-label="Cerrar">×</button>' +
    '</div>' +
    '<div class="settings-popover-body"></div>' +
    '<div class="settings-popover-footer">' +
    '<button type="button" class="btn-settings-action" data-action="save-default">Guardar como default</button>' +
    '<button type="button" class="btn-settings-action restore" data-action="restore-default">Restaurar mi default</button>' +
    '<button type="button" class="btn-settings-action factory" data-action="factory">Layout original</button>' +
    '<div class="settings-popover-hint" data-role="hint"></div>' +
    '</div>';
  document.body.appendChild(pop);

  pop.addEventListener("click", (e) => e.stopPropagation());
  pop.querySelector(".settings-popover-close").addEventListener("click", closeSettings);
  pop.querySelectorAll(".btn-settings-action").forEach((btn) => {
    btn.addEventListener("click", (e) => {
      const action = btn.dataset.action;
      const hint = pop.querySelector("[data-role='hint']");
      if (action === "save-default") {
        const ok = saveAsUserDefault();
        hint.textContent = ok ? "✓ Guardado como tu layout default" : "✗ No se pudo guardar";
        hint.className = "settings-popover-hint " + (ok ? "ok" : "err");
      } else if (action === "restore-default") {
        if (!hasUserDefault()) {
          hint.textContent = "No hay un default guardado todavía";
          hint.className = "settings-popover-hint err";
          return;
        }
        if (confirm("Esto va a reemplazar el layout actual con tu default guardado.")) {
          restoreUserDefault();
        }
      } else if (action === "factory") {
        if (confirm("Esto va a borrar la disposición actual y mostrar TODOS los paneles en su posición original. ¿Continuar?")) {
          restoreFactoryLayout();
        }
      }
    });
  });

  refreshSettingsPopover(pop);
  return pop;
}

function refreshSettingsPopover(pop) {
  const body = pop.querySelector(".settings-popover-body");
  const sections = [
    {
      title: "Paneles de datos",
      ids: ALL_PANELS.filter((p) => !p.curve).map((p) => p.id),
    },
    {
      title: "Charts de curva (TIR vs Duration)",
      ids: ALL_PANELS.filter((p) => p.curve).map((p) => p.id),
    },
  ];

  let html = "";
  for (const sec of sections) {
    html += '<div class="settings-section">';
    html += `<div class="settings-section-title">${_escHtml(sec.title)}</div>`;
    for (const id of sec.ids) {
      const meta = ALL_PANELS.find((p) => p.id === id);
      const checked = isPanelVisible(id) ? "checked" : "";
      html +=
        '<label class="settings-item">' +
        `<input type="checkbox" ${checked} data-panel="${_escHtml(id)}"/>` +
        `<span>${_escHtml(meta.label)}</span>` +
        "</label>";
    }
    html += "</div>";
  }
  body.innerHTML = html;

  body.querySelectorAll('input[type="checkbox"]').forEach((cb) => {
    cb.addEventListener("change", (e) => {
      e.stopPropagation();
      setPanelVisible(cb.dataset.panel, cb.checked);
    });
  });
}

function openSettings() {
  if (!settingsPopover) settingsPopover = buildSettingsPopover();
  refreshSettingsPopover(settingsPopover);
  settingsPopover.hidden = false;
  // Anchorear bajo el botón.
  const btn = document.getElementById("btn-settings");
  if (btn) {
    btn.setAttribute("aria-expanded", "true");
    const rect = btn.getBoundingClientRect();
    settingsPopover.style.top = rect.bottom + 6 + "px";
    settingsPopover.style.right = Math.max(8, window.innerWidth - rect.right) + "px";
  }
}

function closeSettings() {
  if (settingsPopover) settingsPopover.hidden = true;
  const btn = document.getElementById("btn-settings");
  if (btn) btn.setAttribute("aria-expanded", "false");
}

// =====================================================================
// Curva genérica de bonos (TIR vs Duration) reusable por cualquier panel.
// Cada panel-curva extra (curva_cer, curva_tasa_fija, etc.) tiene su
// propia instancia de Chart.js cacheada por gs-id.
// =====================================================================

const bondCurveCharts = {};

// LECAPs/BONCAPs se muestran al usuario en TNA (no TIR). El resto de los
// bonos van en TIR — convención del panel. Mapeo source-id → row field.
const CURVE_Y_FIELD_BY_ID = {
  tasa_fija: "tna",
};

function renderBondCurve(panel, sourceMonitor) {
  const canvas = panel.querySelector("[data-role='canvas']");
  if (!canvas) return;
  const sub = panel.querySelector("[data-role='subtitle']");
  const ts = panel.querySelector("[data-role='ts']");

  panel.classList.remove("loading", "error");
  if (!sourceMonitor || sourceMonitor.status !== "ok") {
    if (sourceMonitor && sourceMonitor.status === "loading") panel.classList.add("loading");
    if (sourceMonitor && sourceMonitor.status === "error") panel.classList.add("error");
    return;
  }

  const yField = CURVE_Y_FIELD_BY_ID[sourceMonitor.id] || "tir";
  const yLabel = yField.toUpperCase();

  const points = (sourceMonitor.rows || [])
    .map((r) => ({ ticker: r.ticker, x: r.duration, y: r[yField] }))
    .filter((p) => p.x != null && p.y != null
                 && Number.isFinite(p.x) && Number.isFinite(p.y) && p.x > 0
                 // Sanity filter: TIRs en [-10%, 500%]. Excluye Pesos variants
                 // con precio en ARS y cashflows USD (TIR sale −50% a −100%) +
                 // bonos a vto inminente con TIR distorsionada.
                 && p.y > -10 && p.y < 500)
    .sort((a, b) => a.x - b.x);

  const colored = sourceMonitor.id !== "bonares";
  if (sub) sub.textContent = `${points.length} bonos · ${yLabel} vs DM`
    + (colored ? " · 🟢 barato / 🔴 caro vs curva" : " · regresión logarítmica");
  if (ts) ts.textContent = sourceMonitor.ts
    ? `Act. ${fmt.timeHMS(new Date(sourceMonitor.ts))}`
    : "—";

  const sourceId = panel.getAttribute("data-source") || panel.getAttribute("data-id");
  let datasets;
  if (sourceMonitor.id === "bonares") {
    const { al, gd } = splitBySeries(points);
    datasets = [
      ...curvaDatasets(al, "#6ab4f7", "BONARES (AL/AO)", "top"),
      ...curvaDatasets(gd, "#f0c040", "GLOBALES (GD/AE)", "bottom"),
    ];
  } else {
    // Curvas mono-serie (CER, tasa fija, DL, TAMAR): puntos coloreados rich/cheap.
    datasets = curvaDatasets(points, CHART.ACCENT_BLUE, sourceMonitor.title || sourceId, "top",
                             { colorByResidual: true });
  }

  if (bondCurveCharts[sourceId]) {
    bondCurveCharts[sourceId].data.datasets = datasets;
    bondCurveCharts[sourceId].update("none");
    return;
  }

  if (window.Chart && window.ChartDataLabels) {
    try { Chart.register(ChartDataLabels); } catch {}
  }
  bondCurveCharts[sourceId] = new Chart(canvas.getContext("2d"), {
    type: "scatter",
    data: { datasets },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      layout: { padding: { top: 24, right: 16, bottom: 8, left: 8 } },
      scales: {
        x: {
          title: { display: true, text: "Duration Modificada (años)", color: CHART.TEXT_DIM, font: { weight: 700, size: 12 } },
          ticks: { color: CHART.TEXT_DIM, font: { size: 11 } },
          grid: { color: CHART.GRID },
        },
        y: {
          title: { display: true, text: `Rendimiento (${yLabel} %)`, color: CHART.TEXT_DIM, font: { weight: 700, size: 12 } },
          ticks: { color: CHART.TEXT_DIM, font: { size: 11 }, callback: (v) => `${fmt.number(v, 1)}%` },
          grid: { color: CHART.GRID },
        },
      },
      plugins: {
        legend: {
          display: sourceMonitor.id === "bonares",
          position: "top", align: "end",
          labels: {
            color: CHART.NAVY_DARK, font: { weight: 700, size: 11 },
            boxWidth: 12, boxHeight: 12, usePointStyle: true,
            filter: (item) => !item.text.startsWith("_line_"),
          },
        },
        tooltip: {
          backgroundColor: CHART.NAVY, titleColor: "#fff", bodyColor: "#fff", padding: 10,
          filter: (item) => !item.dataset.label.startsWith("_line_"),
          callbacks: {
            title: (items) => items[0].raw.ticker,
            label: (item) => `${yLabel} ${fmt.number(item.raw.y, 2)}%  ·  DM ${fmt.number(item.raw.x, 2)} años`,
          },
        },
        datalabels: { padding: 4 },
      },
    },
  });
}

// Días calendario de hoy al vencimiento. `vtoIso` es ISO "YYYY-MM-DD"
// (lo que serializa el backend). Devuelve null si no parsea.
function _diasAlVto(vtoIso) {
  if (!vtoIso) return null;
  const d = new Date(String(vtoIso).slice(0, 10) + "T00:00:00");
  if (Number.isNaN(d.getTime())) return null;
  const now = new Date();
  const today = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  return Math.round((d - today) / 86400000);
}

// Curva de futuros DLR: TNA implícita (y) vs días al vto (x). Cada punto es
// un contrato, etiquetado con el mes (sufijo del ticker). Hover → todos los
// datos relevantes (TNA, OP.INT, VOL, último, compra/venta/ajuste).
function renderFuturosChart(panel, sourceMonitor) {
  const canvas = panel.querySelector("[data-role='canvas']");
  if (!canvas) return;
  const sub = panel.querySelector("[data-role='subtitle']");
  const ts = panel.querySelector("[data-role='ts']");

  panel.classList.remove("loading", "error");
  if (!sourceMonitor || sourceMonitor.status !== "ok") {
    if (sourceMonitor && sourceMonitor.status === "loading") panel.classList.add("loading");
    if (sourceMonitor && sourceMonitor.status === "error") panel.classList.add("error");
    return;
  }

  const points = (sourceMonitor.rows || [])
    .map((r) => ({
      ticker: r.ticker,
      label: String(r.ticker || "").split("/").pop(),
      x: _diasAlVto(r.vto),
      y: r.tna,
      last: r.last, bid: r.bid, ask: r.ask, settle: r.settle,
      oi: r.open_interest, vol: r.volume,
    }))
    .filter((p) => p.x != null && p.x > 0 && p.y != null && Number.isFinite(p.y))
    .sort((a, b) => a.x - b.x);

  if (sub) sub.textContent = `${points.length} contratos · regresión logarítmica · TNA implícita vs días al vto`;
  if (ts) ts.textContent = sourceMonitor.ts
    ? `Act. ${fmt.timeHMS(new Date(sourceMonitor.ts))}`
    : "—";

  canvas.setAttribute("aria-label", "Curva TNA implícita vs días al vencimiento");

  const sourceId = panel.getAttribute("data-source") || panel.getAttribute("data-id");
  const color = CHART.ACCENT_BLUE;
  // Puntos reales (sin línea) + curva de regresión logarítmica (TNA = a + b·ln(días)),
  // mismo criterio que las curvas de bonos. La línea `_line_` se excluye del tooltip.
  const datasets = [
    {
      label: "Futuros DLR",
      data: points,
      showLine: false,
      borderColor: color,
      backgroundColor: color,
      pointRadius: 6,
      pointHoverRadius: 9,
      datalabels: {
        align: "top", anchor: "center", offset: 8,
        color,
        font: { weight: 700, size: 11 },
        formatter: (v) => v.label,
      },
    },
    {
      label: "_line_Futuros DLR",
      data: logCurvePoints(points),
      showLine: true,
      borderColor: color,
      borderWidth: 2,
      backgroundColor: "transparent",
      pointRadius: 0,
      pointHoverRadius: 0,
      tension: 0,
      datalabels: { display: false },
    },
  ];

  if (bondCurveCharts[sourceId]) {
    bondCurveCharts[sourceId].data.datasets = datasets;
    bondCurveCharts[sourceId].update("none");
    return;
  }

  if (window.Chart && window.ChartDataLabels) {
    try { Chart.register(ChartDataLabels); } catch {}
  }
  bondCurveCharts[sourceId] = new Chart(canvas.getContext("2d"), {
    type: "scatter",
    data: { datasets },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      layout: { padding: { top: 24, right: 16, bottom: 8, left: 8 } },
      scales: {
        x: {
          title: { display: true, text: "Días al vencimiento", color: CHART.TEXT_DIM, font: { weight: 700, size: 12 } },
          ticks: { color: CHART.TEXT_DIM, font: { size: 11 } },
          grid: { color: CHART.GRID },
        },
        y: {
          title: { display: true, text: "TNA implícita (%)", color: CHART.TEXT_DIM, font: { weight: 700, size: 12 } },
          ticks: { color: CHART.TEXT_DIM, font: { size: 11 }, callback: (v) => `${fmt.number(v, 1)}%` },
          grid: { color: CHART.GRID },
        },
      },
      plugins: {
        legend: { display: false },
        tooltip: {
          backgroundColor: CHART.NAVY, titleColor: "#fff", bodyColor: "#fff", padding: 10,
          filter: (item) => !item.dataset.label.startsWith("_line_"),
          callbacks: {
            title: (items) => items[0].raw.ticker,
            label: (item) => {
              const p = item.raw;
              const out = [
                `TNA: ${fmt.number(p.y, 2)}%`,
                `Días al vto: ${p.x}`,
                `Último: ${p.last != null ? fmt.number(p.last, 2) : "–"}`,
                `OP.INT: ${fmt.volume(p.oi)}`,
                `VOL: ${fmt.volume(p.vol)}`,
              ];
              if (p.bid != null) out.push(`Compra: ${fmt.number(p.bid, 2)}`);
              if (p.ask != null) out.push(`Venta: ${fmt.number(p.ask, 2)}`);
              if (p.settle != null) out.push(`Ajuste: ${fmt.number(p.settle, 2)}`);
              return out;
            },
          },
        },
        datalabels: { padding: 4 },
      },
    },
  });
}

// =====================================================================
// Focus trap para modales (a11y): cicla Tab dentro del diálogo y devuelve
// el foco al elemento que lo abrió al cerrar. Se cablea en las funciones
// close*, que cubren todos los caminos (botón X, backdrop, Escape).
// =====================================================================
const _FOCUSABLE_SEL =
  'a[href], button:not([disabled]), textarea:not([disabled]), input:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex="-1"])';
let _focusTrap = null;

function trapFocusIn(container) {
  releaseFocusTrap();
  if (!container) return;
  const returnTo = document.activeElement;
  const onKey = (e) => {
    if (e.key !== "Tab") return;
    const f = Array.from(container.querySelectorAll(_FOCUSABLE_SEL))
      .filter((el) => el.offsetParent !== null || el === document.activeElement);
    if (!f.length) return;
    const first = f[0], last = f[f.length - 1];
    if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
    else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
  };
  container.addEventListener("keydown", onKey);
  _focusTrap = { container, onKey, returnTo };
  const first = container.querySelector(_FOCUSABLE_SEL);
  if (first) setTimeout(() => { try { first.focus(); } catch (e) {} }, 0);
}

function releaseFocusTrap() {
  if (!_focusTrap) return;
  const { container, onKey, returnTo } = _focusTrap;
  container.removeEventListener("keydown", onKey);
  _focusTrap = null;
  if (returnTo && typeof returnTo.focus === "function") {
    try { returnTo.focus(); } catch (e) {}
  }
}

function initThemeToggle() {
  const btn = document.getElementById("btn-theme");
  if (!btn) return;
  const root = document.documentElement;
  const meta = document.querySelector('meta[name="theme-color"]');
  const sync = () => {
    const dark = root.getAttribute("data-theme") === "dark";
    btn.setAttribute("aria-pressed", dark ? "true" : "false");
    if (meta) meta.setAttribute("content", dark ? "#070f24" : "#0a1d4a");
  };
  sync();
  btn.addEventListener("click", (e) => {
    e.stopPropagation();
    const dark = root.getAttribute("data-theme") === "dark";
    const next = dark ? "light" : "dark";
    root.setAttribute("data-theme", next);
    try { localStorage.setItem("monitor-theme", next); } catch (err) {}
    sync();
    // Redibujar los charts del dashboard con la paleta del tema activo.
    syncChartPalette();
    if (lastSnapshot) { try { renderAll(lastSnapshot); } catch (err) {} }
    if (lastBeiRows)  { try { renderBeiHistory(lastBeiRows); } catch (err) {} }
  });
}

// =====================================================================
// Panel ESCENARIOS / STRESS — interactivo, fetch propio (no snapshot 5s).
// Sliders Δtasa (bps) + ΔFX (%) → POST /api/scenario → ΔP por bono +
// P&L de la cartera. Reprice analítico MD+convexidad en el backend.
// =====================================================================
function initScenarios() {
  const panel = document.querySelector(".panel[data-id='escenarios']");
  if (!panel) return;
  const tir = document.getElementById("esc-tir");
  const fx = document.getElementById("esc-fx");
  const tirVal = document.getElementById("esc-tir-val");
  const fxVal = document.getElementById("esc-fx-val");
  if (!tir || !fx) return;
  let timer = null;
  const signed = (v) => (v > 0 ? "+" : "") + v;
  const sync = () => { tirVal.textContent = signed(+tir.value); fxVal.textContent = signed(+fx.value); };

  async function run() {
    sync();
    try {
      const r = await fetch("/api/scenario", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ d_tir_bps: +tir.value, d_fx_pct: +fx.value }),
      });
      if (!r.ok) return;
      renderScenario(await r.json());
    } catch (e) { /* mantener último render ante error de red */ }
  }
  const schedule = () => { sync(); clearTimeout(timer); timer = setTimeout(run, 180); };
  tir.addEventListener("input", schedule);
  fx.addEventListener("input", schedule);
  const reset = document.getElementById("esc-reset");
  if (reset) reset.addEventListener("click", () => { tir.value = 0; fx.value = 0; run(); });
  run();  // estado inicial (0, 0)
}

function renderScenario(data) {
  const pf = document.getElementById("esc-portfolio");
  const pfVal = document.getElementById("esc-pf-val");
  const p = data && data.portfolio;
  if (pf && p && p.pnl_ars != null) {
    pf.hidden = false;
    pfVal.className = "esc-pf-val " + (p.pnl_ars >= 0 ? "esc-pos" : "esc-neg");
    pfVal.textContent = `$ ${fmt.number(p.pnl_ars, 0)}  (${fmt.number(p.pnl_pct, 2)}%)`;
  } else if (pf) {
    pf.hidden = true;
  }
  const wrap = document.getElementById("esc-table");
  if (!wrap) return;
  const bonds = (data && data.bonds) || [];
  if (bonds.length === 0) { wrap.innerHTML = '<div class="esc-empty">Sin datos.</div>'; return; }
  let html = '<table class="bonds esc-bonds"><thead><tr>'
    + '<th>Ticker</th><th>Tipo</th><th>ΔP&nbsp;%</th><th>ΔP&nbsp;ARS&nbsp;%</th><th>Precio→</th>'
    + '</tr></thead><tbody>';
  for (const b of bonds) {
    const cArs = (b.dp_ars_pct ?? 0) >= 0 ? "esc-pos" : "esc-neg";
    const cP = (b.dp_pct ?? 0) >= 0 ? "esc-pos" : "esc-neg";
    html += `<tr><td class="esc-tk">${b.ticker}</td><td class="esc-grp">${b.grupo || "—"}</td>`
      + `<td class="${cP}">${fmt.number(b.dp_pct, 2)}%</td>`
      + `<td class="${cArs}">${fmt.number(b.dp_ars_pct, 2)}%</td>`
      + `<td>${b.new_price == null ? "—" : fmt.number(b.new_price, 2)}</td></tr>`;
  }
  html += "</tbody></table>";
  wrap.innerHTML = html;
}

// Menú "Análisis ▾" del header: agrupa Valor Relativo / Escenarios (scroll al
// panel) + Cashflows / BCRA (abren su página). Cierra con click afuera o ESC.
function initAnalisisMenu() {
  const btn = document.getElementById("btn-analisis");
  const menu = document.getElementById("analisis-menu");
  if (!btn || !menu) return;
  const close = () => { menu.hidden = true; btn.setAttribute("aria-expanded", "false"); };
  btn.addEventListener("click", (e) => {
    e.stopPropagation();
    const willOpen = menu.hidden;
    menu.hidden = !willOpen;
    btn.setAttribute("aria-expanded", willOpen ? "true" : "false");
  });
  menu.addEventListener("click", (e) => {
    const item = e.target.closest(".menu-item");
    if (!item) return;
    if (item.dataset.action === "open") {
      window.open(item.dataset.href, "_blank");
    } else {
      const gsItem = document.querySelector(`.grid-stack-item[gs-id='${item.dataset.target}']`);
      if (gsItem) {
        gsItem.style.display = "";  // por si estaba oculto vía ⚙ Layout
        const panel = gsItem.querySelector(".panel");
        (panel || gsItem).scrollIntoView({ behavior: "smooth", block: "start" });
        if (panel) { panel.classList.add("flash"); setTimeout(() => panel.classList.remove("flash"), 1200); }
      }
    }
    close();
  });
  document.addEventListener("click", close);
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") close(); });
}

function init() {
  setBrandDate();
  // Refresca la fecha del header cada minuto (por si pasa medianoche)
  setInterval(setBrandDate, 60 * 1000);

  loadHiddenCols();
  loadPanelVisibility();
  gridInstance = initGridstack();
  applyPanelVisibility();
  _loadSupportedTickers();  // poblar HISTORY_SUPPORTED_TICKERS desde el server

  initThemeToggle();
  document.getElementById("btn-capture").addEventListener("click", captureFiel);
  const settingsBtn = document.getElementById("btn-settings");
  if (settingsBtn) {
    settingsBtn.addEventListener("click", (e) => {
      e.stopPropagation();
      if (settingsPopover && !settingsPopover.hidden) closeSettings();
      else openSettings();
    });
  }
  // Click afuera + ESC cierran el popover de settings.
  document.addEventListener("click", () => closeSettings());
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeSettings(); });
  abmInit();
  initFciPanel();   // panel FCI (CAFCI) — fetch propio, independiente del snapshot
  initScenarios();  // panel Escenarios — interactivo, POST /api/scenario por slider
  initAnalisisMenu();  // menú "Análisis ▾" del header

  fetchSnapshot();
  setInterval(fetchSnapshot, REFRESH_MS);

  fetchBeiHistory();
  setInterval(fetchBeiHistory, 5 * 60 * 1000); // BEI history refresca cada 5 min
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", init);
} else {
  init();
}
