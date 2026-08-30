"""Generador del PDF del panel de Obligaciones Negociables (/on) — listo para cliente.

`build_on_pdf(data) -> (bytes, meta)` toma el dataset de `/on/data` (mismo shape que
`get_on_dataset`), deduplica a la pata MEP (…D), y arma un PDF landscape con dos
secciones: RESUMEN POR SECTOR (tarjetas con TIR/MD prom, scatter TIR-vs-MD y TOP 5) y
DETALLE Sector › Emisor › Título (tabla completa).

Lo usan tanto el endpoint `GET /on/pdf` (descarga del botón) como el script CLI
`scripts/export_on_pdf.py`. `fpdf2` es requerido; `matplotlib` (charts) es opcional —
si falta, el PDF se genera sin los scatter.
"""
from __future__ import annotations

import logging
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from fpdf import FPDF

logger = logging.getLogger(__name__)

# Fuente TrueType con soporte Unicode (las core fonts de fpdf son latin-1 y rompen
# con "ñ"/tildes). Se elige la primera presente: Arial en Windows, DejaVu/Liberation
# en los contenedores Linux. Sin ninguna, `_pick_font` levanta un error explícito en
# vez de que `add_font` tire un FileNotFoundError críptico.
_FONT_CANDIDATES = [
    (r"C:\Windows\Fonts\arial.ttf", r"C:\Windows\Fonts\arialbd.ttf"),
    ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
     "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
    ("/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
     "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"),
]
FREC = {1: "Anual", 2: "Semestral", 3: "Cuatrim.", 4: "Trimestral", 6: "Bimestral", 12: "Mensual"}

NAVY = (16, 36, 82)
INK = (24, 30, 46)
GREY = (120, 128, 150)
FAINT = (140, 147, 168)
ALT = (244, 247, 252)
ACCENT = (58, 95, 207)
POS = (20, 138, 74)
POS_DARK = (6, 74, 38)
_GRADE_RGB = {"strong": (20, 138, 74), "good": (58, 95, 207), "mid": (176, 138, 20),
              "weak": (192, 112, 32), "distress": (200, 56, 50)}
CHART_FIG = (4.0, 0.95)

DISCLAIMER = (
    "La información contenida en este documento es de carácter exclusivamente informativo y se "
    "basa en fuentes consideradas confiables, sin que se garantice su exactitud, integridad o "
    "vigencia. Los precios, rendimientos (TIR), paridades y demás métricas son indicativos, "
    "surgen de cálculos propios sobre datos de mercado y pueden diferir de los valores de pantalla "
    "o de cierre; están sujetos a cambios sin previo aviso. Este material no constituye una oferta, "
    "cotización en firme, asesoramiento financiero, recomendación ni invitación a comprar, vender o "
    "suscribir los instrumentos mencionados. Los rendimientos pasados no garantizan resultados "
    "futuros. Toda decisión de inversión es de exclusiva responsabilidad del inversor, quien deberá "
    "evaluar su conveniencia según su perfil de riesgo y objetivos y consultar a sus asesores "
    "profesionales. Ni el autor ni las entidades vinculadas asumen responsabilidad alguna por "
    "pérdidas o daños, directos o indirectos, derivados del uso de esta información."
)


# ---- formato AR (coma decimal, punto de miles) ----
def ar(x, dec=2):
    if x in (None, ""):
        return "—"
    try:
        s = f"{float(x):,.{dec}f}"
    except (TypeError, ValueError):
        return "—"
    return s.replace(",", "§").replace(".", ",").replace("§", ".")


def dt(s):
    if not s:
        return "—"
    p = str(s)[:10].split("-")
    return f"{p[2]}/{p[1]}/{p[0][2:]}" if len(p) == 3 else str(s)


def pct(x, dec=2):
    return ar(x, dec) + "%" if x not in (None, "") else "—"


def spct(x):
    if x in (None, ""):
        return "—"
    return ("+" if x >= 0 else "") + ar(x, 2) + "%"


COLS = [
    ("Título",   lambda b: b["ticker"],                12, "L"),
    ("Clase",    lambda b: b.get("clase") or "—",       30, "L"),
    ("Ley",      lambda b: b.get("ley") or "",           9, "C"),
    ("Tipo",     lambda b: b.get("tipo") or "",          9, "C"),
    ("Emisión",  lambda b: dt(b.get("emision")),        15, "C"),
    ("Vto",      lambda b: dt(b.get("vto")),             15, "C"),
    ("Cupón",    lambda b: pct(b.get("cupon")),          15, "R"),
    ("Frec.",    lambda b: FREC.get(b.get("frec"), "—"), 18, "C"),
    ("Días cup.", lambda b: str(b.get("dias_cupon") or "—"), 14, "R"),
    ("Precio",   lambda b: ar(b.get("price")),           16, "R"),
    ("Paridad",  lambda b: pct(b.get("paridad"), 1),     16, "R"),
    ("TIR",      lambda b: pct(b.get("tir")),            15, "R"),
    ("CY",       lambda b: pct(b.get("cy")),             14, "R"),
    ("MD",       lambda b: ar(b.get("md")),              12, "R"),
    ("Convex.",  lambda b: ar(b.get("convex")),          14, "R"),
    ("%Día",     lambda b: spct(b.get("change_pct")),    14, "R"),
    ("Vol",      lambda b: ar(b.get("volume"), 0),       12, "R"),
]
TABLE_W = sum(c[2] for c in COLS)
MARGIN = (297 - TABLE_W) / 2


def dedupe_mep(bonds):
    """1 fila por bono a la pata MEP (…D); fallback CABLE (…C) y, en último término, la
    pata en pesos. El fallback a ARS importa: `on_service` descarta las patas sin
    cotización, así que una ON que hoy sólo operó en pesos llegaría acá sin …D ni …C y
    desaparecería del PDF (y los conteos "{n} ONs" no cuadrarían con el panel)."""
    pref = ["MEP", "CABLE", "ARS"]
    by_base: Dict[str, Dict] = {}
    for b in bonds:
        t = b.get("ticker") or ""
        base = t[:-1] if (len(t) > 1 and t[-1] in "ODC") else t
        by_base.setdefault(base, {})[b.get("ccy")] = b
    out = []
    for legs in by_base.values():
        leg = next((legs[p] for p in pref if p in legs), None)
        if leg is None and legs:          # moneda desconocida: no perder el bono
            leg = next(iter(legs.values()))
        if leg is not None:
            out.append(leg)
    return out


def pick_font() -> Tuple[str, str]:
    """(regular, bold) del primer par de TTF presente. Si sólo falta la bold, se usa la
    regular en su lugar; si no hay ninguna familia, error explícito (lo traduce a 503
    el router, en vez de un 500 con traceback)."""
    for reg, bold in _FONT_CANDIDATES:
        if Path(reg).exists():
            return reg, (bold if Path(bold).exists() else reg)
    raise FileNotFoundError(
        "No se encontró una fuente TrueType para el PDF. Probadas: "
        + ", ".join(r for r, _ in _FONT_CANDIDATES))


def _avg(xs):
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else None


def _emi_short(name, n=44):
    name = (name or "").strip()
    return name if len(name) <= n else name[: n - 1].rstrip() + "…"


def tir_of(b) -> float:
    """TIR ordenable: null-coalesce EXPLÍCITO. `b.get("tir") or -99` mandaría al fondo
    una TIR de 0.00% (legítima), indistinguible de un bono sin TIR — el mismo bug que
    `static/js/on.js` ya evita en su ordenamiento gemelo."""
    v = b.get("tir")
    return v if v is not None else float("-inf")


def render_chart(bonds, hexcolor, path):
    """Scatter TIR(y) vs MD(x): AR círculos, EXT triángulos, + curva log-fit.

    Usa la API orientada a objetos (`Figure` + canvas Agg) en vez de `pyplot`: este
    módulo corre en el threadpool de FastAPI (la ruta `/on/pdf` es sync) y el gestor de
    figuras de pyplot es estado GLOBAL del proceso, no thread-safe — dos descargas
    simultáneas podían entrelazar figuras y emitir PNGs corruptos o vacíos."""
    import numpy as np
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.figure import Figure

    col = "#" + hexcolor.lstrip("#")
    ar_pts = [(b["md"], b["tir"]) for b in bonds
              if b.get("ley") == "AR" and b.get("md") is not None and b.get("tir") is not None]
    ext_pts = [(b["md"], b["tir"]) for b in bonds
               if b.get("ley") == "EXT" and b.get("md") is not None and b.get("tir") is not None]
    fig = Figure(figsize=CHART_FIG, dpi=150)
    FigureCanvasAgg(fig)
    axx = fig.add_subplot(111)
    if ar_pts:
        axx.scatter(*zip(*ar_pts), c=col, marker="o", s=13, edgecolors="white", linewidths=0.4, alpha=0.92, zorder=3)
    if ext_pts:
        axx.scatter(*zip(*ext_pts), facecolors="none", edgecolors=col, marker="^", s=17, linewidths=0.8, alpha=0.85, zorder=3)
    pts = ar_pts + ext_pts
    if len(pts) >= 3:
        xs = np.array([p[0] for p in pts], float)
        ys = np.array([p[1] for p in pts], float)
        m = xs > 0
        if m.sum() >= 3:
            b1, b0 = np.polyfit(np.log(xs[m]), ys[m], 1)
            xx = np.linspace(max(xs[m].min(), 0.05), xs.max(), 50)
            axx.plot(xx, b0 + b1 * np.log(xx), color=col, lw=1.1, alpha=0.65, zorder=2)
    axx.set_xlabel("MD (años)", fontsize=5.5, labelpad=1)
    axx.set_ylabel("TIR %", fontsize=5.5, labelpad=1)
    axx.tick_params(labelsize=4.8, length=2, pad=1)
    axx.grid(True, lw=0.3, alpha=0.35)
    axx.margins(0.06)
    for sp in axx.spines.values():
        sp.set_linewidth(0.4)
        sp.set_color("#b8c0d4")
    fig.tight_layout(pad=0.2)
    fig.savefig(path, facecolor="white")


def sector_summaries(rows, smeta):
    by_sec: Dict[str, List] = {}
    for b in rows:
        by_sec.setdefault(b.get("sector") or "Otros", []).append(b)
    out = []
    for key, bonds in by_sec.items():
        meta = smeta.get(key, {})
        col = (meta.get("color") or "#3a5fcf").lstrip("#")
        rgb = tuple(int(col[i:i + 2], 16) for i in (0, 2, 4))
        n_ar = sum(1 for b in bonds if b.get("ley") == "AR")
        top5 = sorted(bonds, key=tir_of, reverse=True)[:5]
        out.append({
            "key": key, "short": meta.get("short", key), "hex": col, "rgb": rgb,
            "bonds": bonds, "n": len(bonds), "n_ar": n_ar, "n_ext": len(bonds) - n_ar,
            "tir": _avg([b.get("tir") for b in bonds]), "md": _avg([b.get("md") for b in bonds]),
            "vol": sum(b.get("volume") or 0 for b in bonds), "top5": top5,
        })
    # por TIR prom desc (vista TIR▼); None al final, pero un 0.0 real NO es None
    out.sort(key=lambda s: s["tir"] if s["tir"] is not None else float("-inf"), reverse=True)
    return out


class _PDF(FPDF):
    as_of = ""
    mode = "summary"
    ratings_line = ""

    def header(self):
        self.set_fill_color(*NAVY)
        self.rect(MARGIN, 6, TABLE_W, 8.5, "F")
        self.set_text_color(255, 255, 255)
        self.set_xy(MARGIN + 1.5, 6)
        self.set_font("Arial", "B", 12)
        self.cell(TABLE_W - 3, 8.5, "Obligaciones Negociables  ·  Renta Fija Argentina", align="L")
        self.set_xy(MARGIN, 6)
        self.set_font("Arial", "", 8)
        self.cell(TABLE_W - 2, 8.5, f"Precios Dólar MEP  ·  {self.as_of}", align="R")
        if self.mode == "summary":
            self.set_xy(MARGIN, 15.2)
            self.set_font("Arial", "B", 7.5)
            self.set_text_color(*ACCENT)
            self.cell(TABLE_W, 4, "RESUMEN POR SECTOR  ·  ¿Qué rinde cada sector?", align="L")
            self.set_y(20)
        else:
            self.set_y(15)
            self.set_font("Arial", "B", 6.6)
            self.set_fill_color(*NAVY)
            self.set_text_color(255, 255, 255)
            x = MARGIN
            for label, _, w, align in COLS:
                self.set_xy(x, 15)
                self.cell(w, 6, " " + label if align == "L" else label, align=align, fill=True)
                x += w
            self.set_y(21)
        self.set_text_color(*INK)

    def footer(self):
        self.set_y(-16)
        self.set_draw_color(210, 216, 228)
        self.set_line_width(0.2)
        self.line(MARGIN, self.get_y(), MARGIN + TABLE_W, self.get_y())
        self.set_xy(MARGIN + TABLE_W - 26, -14.5)
        self.set_font("Arial", "B", 13)
        self.set_text_color(*NAVY)
        self.cell(26, 12, f"{self.page_no()} / {{nb}}", align="R")
        self.set_xy(MARGIN, -14.2)
        self.set_font("Arial", "", 5.2)
        self.set_text_color(*GREY)
        self.multi_cell(TABLE_W - 30, 2.3, "Fuente: BYMA.  " + self.ratings_line + DISCLAIMER, align="J")


def _draw_card(pdf, x, y, w, h, sd, chart):
    pdf.set_draw_color(214, 220, 232)
    pdf.set_line_width(0.3)
    pdf.rect(x, y, w, h)
    pdf.set_fill_color(*sd["rgb"])
    pdf.rect(x, y, w, 2.2, "F")
    pdf.set_xy(x + 3, y + 3.5)
    pdf.set_font("Arial", "B", 10.5)
    pdf.set_text_color(*sd["rgb"])
    pdf.cell(w * 0.55, 6, sd["short"], align="L")
    pdf.set_xy(x, y + 4)
    pdf.set_font("Arial", "", 7)
    pdf.set_text_color(*GREY)
    pdf.cell(w - 3, 5, f"AR {sd['n_ar']}    EXT {sd['n_ext']}    {sd['n']} ONs", align="R")
    ky = y + 11.5
    kpis = [("TIR PROM", pct(sd["tir"]), "anual"), ("MD PROM", ar(sd["md"]), "años"),
            ("VOL HOY", ar(sd["vol"], 0), "nominal")]
    kw = w / 3
    for i, (lbl, val, sub) in enumerate(kpis):
        kx = x + i * kw
        pdf.set_xy(kx, ky); pdf.set_font("Arial", "", 6.3); pdf.set_text_color(*FAINT)
        pdf.cell(kw, 3, lbl, align="C")
        pdf.set_xy(kx, ky + 3); pdf.set_font("Arial", "B", 11); pdf.set_text_color(*INK)
        pdf.cell(kw, 5, val, align="C")
        pdf.set_xy(kx, ky + 8); pdf.set_font("Arial", "", 5.8); pdf.set_text_color(*FAINT)
        pdf.cell(kw, 3, sub, align="C")
    cy = ky + 12.5
    ch_w = w - 8
    ch_h = ch_w * (CHART_FIG[1] / CHART_FIG[0])
    if chart and Path(chart).exists():
        pdf.image(chart, x=x + 4, y=cy, w=ch_w, h=ch_h)
    ty = cy + ch_h + 1.5
    pdf.set_xy(x + 3, ty); pdf.set_font("Arial", "B", 6.3); pdf.set_text_color(*FAINT)
    pdf.cell(20, 4, "TOP 5  ·  TIR", align="L")
    ty += 4.4
    # Fila de encabezados de columnas
    # Layout: # (5) | Ticker (14.5) | Clase (20) | Ley (9) | Emisor (flex) | TIR (15)
    _hdr = [
        (x + 3,      5,       "#",      "R", FAINT),
        (x + 9.5,    14.5,    "Ticker", "L", FAINT),
        (x + 24.5,   20,      "Clase",  "L", FAINT),
        (x + 45,     9,       "Ley",    "C", FAINT),
        (x + 55,     w - 73,  "Emisor", "L", FAINT),
        (x + w - 18, 15,      "TIR",    "R", POS_DARK),
    ]
    pdf.set_font("Arial", "B", 5.5)
    for hx, hw, hlbl, halign, hcol in _hdr:
        pdf.set_xy(hx, ty); pdf.set_text_color(*hcol)
        pdf.cell(hw, 3, hlbl, align=halign)
    pdf.set_draw_color(200, 206, 220); pdf.set_line_width(0.2)
    pdf.line(x + 3, ty + 3.2, x + w - 3, ty + 3.2)
    ty += 4.0
    for j, b in enumerate(sd["top5"]):
        pdf.set_xy(x + 3, ty); pdf.set_font("Arial", "", 6.8); pdf.set_text_color(*FAINT)
        pdf.cell(5, 4, str(j + 1), align="R")
        pdf.set_xy(x + 9.5, ty); pdf.set_font("Arial", "B", 6.8); pdf.set_text_color(*ACCENT)
        pdf.cell(14.5, 4, b["ticker"], align="L")
        pdf.set_xy(x + 24.5, ty); pdf.set_font("Arial", "", 6.2); pdf.set_text_color(*GREY)
        pdf.cell(20, 4, (b.get("clase") or "—")[:14], align="L")
        pdf.set_xy(x + 45, ty); pdf.set_font("Arial", "B", 5.6)
        pdf.set_text_color(*(ACCENT if b.get("ley") == "AR" else (192, 112, 32)))
        pdf.cell(9, 4, b.get("ley") or "", align="L")
        pdf.set_xy(x + 55, ty); pdf.set_font("Arial", "", 6.6); pdf.set_text_color(70, 76, 96)
        pdf.cell(w - 73, 4, _emi_short(b.get("emisor"), int((w - 73) / 1.45)), align="L")
        pdf.set_xy(x + w - 18, ty); pdf.set_font("Arial", "B", 6.8); pdf.set_text_color(*POS_DARK)
        pdf.cell(15, 4, pct(b.get("tir")), align="R")
        ty += 4.1


def build_on_pdf(data: Dict[str, Any], *, include_dl: bool = False, charts: bool = True,
                 tickers: Optional[List[str]] = None) -> Tuple[bytes, Dict[str, int]]:
    """Arma el PDF y devuelve (bytes, {bonds, sectors, pages}).

    `tickers` restringe el documento a esos símbolos: es lo que manda el botón 🖨️ del
    panel con las facetas del sidebar YA aplicadas, para que el PDF coincida con lo que
    el usuario tiene en pantalla. Cuando se pasa, `include_dl` se ignora — la faceta
    HD/DL del cliente ya decidió qué entra."""
    bonds = data.get("bonds", [])
    if tickers is not None:
        want = {str(t).strip().upper() for t in tickers}
        bonds = [b for b in bonds if (b.get("ticker") or "").upper() in want]
    rows = dedupe_mep(bonds)
    if tickers is None and not include_dl:
        rows = [b for b in rows if (b.get("tipo") or "HD") == "HD"]
    smeta = {s["key"]: s for s in data.get("sectors_meta", [])}
    summaries = sector_summaries(rows, smeta)
    as_of = data.get("today") or datetime.now(timezone.utc).strftime("%Y-%m-%d")

    font_reg, font_bold = pick_font()   # antes del trabajo caro: falla rápido y claro
    tmp = Path(tempfile.mkdtemp(prefix="onpdf_"))
    chart_paths: Dict[str, str] = {}
    if charts:
        for sd in summaries:
            p = tmp / (f"{sd['key'][:12]}.png".replace("/", "_").replace(" ", ""))
            try:
                render_chart(sd["bonds"], sd["hex"], p)
                chart_paths[sd["key"]] = str(p)
            except ImportError:      # sin matplotlib → PDF sin scatter (dep opcional)
                break
            except Exception:        # noqa: BLE001 — un sector sin chart no tumba el PDF
                logger.warning("scatter del sector %s falló", sd["key"], exc_info=True)
    try:
        pdf = _PDF(orientation="L", unit="mm", format="A4")
        pdf.as_of = as_of
        meta = data.get("meta", {})
        pdf.ratings_line = (f"Calificaciones: {meta.get('ratings_source', 'FIX SCR')} "
                            f"al {meta.get('ratings_as_of', as_of)}.  ")
        pdf.add_font("Arial", "", font_reg)
        pdf.add_font("Arial", "B", font_bold)
        pdf.set_auto_page_break(False)
        pdf.set_margins(MARGIN, 6, MARGIN)
        pdf.alias_nb_pages()

        # SECCIÓN 1 — tarjetas de resumen (2 por fila, 4 por página)
        pdf.mode = "summary"
        pdf.add_page()
        cardw = (TABLE_W - 6) / 2
        cardh = 84.0
        for i, sd in enumerate(summaries):
            slot = i % 4
            if i and slot == 0:
                pdf.add_page()
            col, row = slot % 2, slot // 2
            _draw_card(pdf, MARGIN + col * (cardw + 6), 20 + row * (cardh + 4),
                       cardw, cardh, sd, chart_paths.get(sd["key"]))

        # SECCIÓN 2 — detalle Sector › Emisor › Título
        pdf.mode = "detail"
        pdf.add_page()
        BOT, H = 210 - 18, 4.5

        def ensure(hh):
            if pdf.get_y() + hh > BOT:
                pdf.add_page()

        for sd in summaries:
            ensure(6.6 + 5 + H)
            pdf.set_fill_color(*sd["rgb"]); pdf.set_text_color(255, 255, 255)
            pdf.set_font("Arial", "B", 8.5); pdf.set_x(MARGIN)
            pdf.cell(TABLE_W, 6.6,
                     f"  {sd['short'].upper()}   ·   {sd['n']} ONs   (AR {sd['n_ar']} · EXT {sd['n_ext']})"
                     f"      TIR prom {pct(sd['tir'])}   ·   MD prom {ar(sd['md'])}",
                     align="L", fill=True, new_x="LMARGIN", new_y="NEXT")
            by_emi: Dict[str, List] = {}
            for b in sd["bonds"]:
                by_emi.setdefault(b.get("emisor") or "—", []).append(b)
            def _emi_tir(kv):
                v = _avg([x.get("tir") for x in kv[1]])
                return v if v is not None else float("-inf")

            for emi, ebonds in sorted(by_emi.items(), key=_emi_tir, reverse=True):
                ensure(5 + H)
                y_em = pdf.get_y()
                pdf.set_fill_color(229, 234, 244); pdf.set_text_color(*INK)
                pdf.set_font("Arial", "B", 7.4); pdf.set_x(MARGIN)
                pdf.cell(TABLE_W, 5, f"  {emi}   ({len(ebonds)})", border="L", align="L",
                         fill=True, new_x="LMARGIN", new_y="NEXT")
                rt = ebonds[0]
                if rt.get("rating"):           # calificación FIX SCR del emisor (derecha del banner)
                    persp = rt.get("rating_persp")
                    txt = "Calif.  " + rt["rating"] + (f"   ·   {persp}" if persp and persp != "N/A" else "")
                    pdf.set_xy(MARGIN, y_em)
                    pdf.set_font("Arial", "B", 7.2)
                    pdf.set_text_color(*_GRADE_RGB.get(rt.get("rating_grade"), INK))
                    pdf.cell(TABLE_W - 4, 5, txt + "  ", align="R")
                    pdf.set_xy(MARGIN, y_em + 5)
                y0 = pdf.get_y() - 5
                pdf.set_draw_color(*sd["rgb"]); pdf.set_line_width(1.1)
                pdf.line(MARGIN + 0.3, y0, MARGIN + 0.3, y0 + 5)
                pdf.set_line_width(0.2); pdf.set_draw_color(220, 224, 234)
                for i, b in enumerate(sorted(ebonds, key=tir_of, reverse=True)):
                    ensure(H)
                    pdf.set_fill_color(*ALT); pdf.set_text_color(*INK); pdf.set_font("Arial", "", 6.6)
                    x, y = MARGIN, pdf.get_y()
                    for label, fn, w, align in COLS:
                        pdf.set_xy(x, y)
                        pad = " " if align == "L" else ""
                        pdf.cell(w, H, pad + fn(b) + (" " if align == "R" else ""),
                                 border="B", align=align, fill=(i % 2 == 1))
                        x += w
                    pdf.set_draw_color(*sd["rgb"]); pdf.set_line_width(1.1)
                    pdf.line(MARGIN + 0.3, y, MARGIN + 0.3, y + H)
                    pdf.set_line_width(0.2); pdf.set_draw_color(220, 224, 234)
                    pdf.set_xy(MARGIN, y + H)

        blob = bytes(pdf.output())
        return blob, {"bonds": len(rows), "sectors": len(summaries), "pages": pdf.page_no()}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
