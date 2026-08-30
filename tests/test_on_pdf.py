"""Generador del PDF del panel ON (`apps/web/on_pdf.py`).

`fpdf2` está en requirements.txt/lock, así que estos tests NO skipean: el módulo son
>400 líneas que sólo se ejercitaban por el endpoint, y el `importorskip` del test viejo
hacía que en un entorno sin la dep el feature quedara con cobertura CERO (y el endpoint
tirando 500 en runtime sin que nada lo detectara).
"""
from __future__ import annotations

import threading
from pathlib import Path

from apps.web.on_pdf import (
    build_on_pdf,
    dedupe_mep,
    pick_font,
    render_chart,
    sector_summaries,
    tir_of,
)


def _bond(ticker, ccy, **kw):
    b = {"ticker": ticker, "ccy": ccy, "ley": "AR", "tipo": "HD", "emisor": "ACME",
         "clase": "Clase 1", "sector": "energia", "emision": "2024-01-15",
         "vto": "2030-01-15", "cupon": 8.5, "frec": 2, "dias_cupon": 90,
         "price": 98.0, "paridad": 99.1, "tir": 11.2, "cy": 8.7, "md": 3.4,
         "convex": 14.2, "change_pct": 0.4, "volume": 1000.0}
    b.update(kw)
    return b


def _dataset(bonds):
    return {"today": "2026-08-07", "bonds": bonds,
            "sectors_meta": [{"key": "energia", "short": "Energía", "color": "#3a5fcf",
                              "icon": "⚡"}],
            "meta": {"ratings_source": "FIX SCR", "ratings_as_of": "2026-06-01"}}


# ---------------------------------------------------------------- dedupe de patas
def test_dedupe_mep_prefiere_mep_luego_cable():
    rows = dedupe_mep([_bond("ACMEO", "ARS"), _bond("ACMED", "MEP"), _bond("ACMEC", "CABLE")])
    assert [b["ticker"] for b in rows] == ["ACMED"]


def test_dedupe_mep_cae_a_cable_sin_pata_mep():
    rows = dedupe_mep([_bond("ACMEO", "ARS"), _bond("ACMEC", "CABLE")])
    assert [b["ticker"] for b in rows] == ["ACMEC"]


def test_dedupe_mep_no_pierde_la_on_que_solo_opero_en_pesos():
    """`on_service` descarta las patas sin cotización: una ON que hoy sólo operó en
    pesos llega acá sin …D ni …C. Sin fallback a ARS desaparecía del PDF y los conteos
    '{n} ONs' dejaban de coincidir con el panel."""
    rows = dedupe_mep([_bond("ACMEO", "ARS")])
    assert [b["ticker"] for b in rows] == ["ACMEO"]


def test_dedupe_mep_conserva_bonos_con_moneda_desconocida():
    rows = dedupe_mep([_bond("XXXX", None)])
    assert len(rows) == 1


# --------------------------------------------------------------- TIR = 0.0 es real
def test_tir_cero_no_se_ordena_como_faltante():
    """`b.get("tir") or -99` mandaba una TIR de 0.00% al fondo, indistinguible de un
    bono sin TIR. Gemelo del fix ya presente en static/js/on.js."""
    assert tir_of({"tir": 0.0}) > tir_of({"tir": None})
    assert tir_of({"tir": 0.0}) < tir_of({"tir": 0.1})
    assert tir_of({"tir": -1.5}) > tir_of({})


def test_top5_y_orden_de_sectores_con_tir_cero():
    bonds = [_bond("AAAD", "MEP", tir=0.0), _bond("BBBD", "MEP", tir=None),
             _bond("CCCD", "MEP", tir=5.0)]
    smeta = {"energia": {"short": "Energía", "color": "#3a5fcf"}}
    summ = sector_summaries(bonds, smeta)
    assert [b["ticker"] for b in summ[0]["top5"]] == ["CCCD", "AAAD", "BBBD"]

    # un sector cuya TIR promedio es 0.0 va ANTES que uno sin TIR, no al final de todo
    cero = _bond("ZZZD", "MEP", tir=0.0, sector="banco")
    nada = _bond("YYYD", "MEP", tir=None, sector="otros")
    orden = [s["key"] for s in sector_summaries([cero, nada], {})]
    assert orden == ["banco", "otros"]


# --------------------------------------------------------------------- armado PDF
def test_build_on_pdf_genera_un_pdf_valido():
    blob, meta = build_on_pdf(_dataset([_bond("ACMED", "MEP"), _bond("OTRD", "MEP")]),
                              charts=False)
    assert blob[:5] == b"%PDF-"
    assert meta["bonds"] == 2 and meta["sectors"] == 1 and meta["pages"] >= 2


def test_build_on_pdf_filtra_por_tickers():
    """El botón 🖨️ manda los tickers visibles: el PDF tiene que quedarse SÓLO con esos
    (y, con lista explícita, no volver a aplicar el filtro HD por su cuenta)."""
    data = _dataset([_bond("ACMED", "MEP"), _bond("OTRD", "MEP"),
                     _bond("DLD", "MEP", tipo="DL")])
    _, todo = build_on_pdf(data, charts=False)
    _, filtrado = build_on_pdf(data, charts=False, tickers=["ACMED", "DLD"])
    assert todo["bonds"] == 2          # sin tickers: sólo HD (DL excluido)
    assert filtrado["bonds"] == 2      # con tickers: los pedidos, DL incluido
    _, uno = build_on_pdf(data, charts=False, tickers=["acmed"])   # case-insensitive
    assert uno["bonds"] == 1


def test_build_on_pdf_sin_bonos_no_explota():
    blob, meta = build_on_pdf(_dataset([]), charts=False)
    assert blob[:5] == b"%PDF-" and meta["bonds"] == 0


def test_pick_font_devuelve_un_ttf_existente():
    reg, bold = pick_font()
    assert Path(reg).exists() and Path(bold).exists()


# ------------------------------------------------------------------ charts (Agg)
def test_render_chart_es_thread_safe():
    """`/on/pdf` es una ruta sync → corre en el threadpool de FastAPI. Con `pyplot` el
    gestor de figuras es estado global del proceso y dos descargas simultáneas podían
    entrelazarse; con la API `Figure`+Agg cada llamada es independiente."""
    import tempfile

    bonds = [_bond(f"B{i}D", "MEP", md=1.0 + i, tir=8.0 + i) for i in range(6)]
    tmp = Path(tempfile.mkdtemp(prefix="onpdf_test_"))
    errors, paths = [], [tmp / f"c{i}.png" for i in range(8)]

    def one(p):
        try:
            render_chart(bonds, "#3a5fcf", p)
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=one, args=(p,)) for p in paths]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, errors
    for p in paths:
        assert p.exists() and p.stat().st_size > 500      # PNG con contenido
        assert p.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"  # y no truncado/corrupto


def test_on_pdf_no_usa_el_estado_global_de_pyplot():
    """Guarda contra reintroducir `pyplot` (el comentario del módulo lo explica)."""
    src = (Path(__file__).resolve().parent.parent / "apps" / "web" / "on_pdf.py").read_text(
        encoding="utf-8")
    assert "import matplotlib.pyplot" not in src
    assert "plt." not in src
