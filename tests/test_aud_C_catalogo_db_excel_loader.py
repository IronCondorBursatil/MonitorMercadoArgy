"""Auditoría lote C — hallazgo #4: el loader del Excel semilla se traga los errores.

`ExcelInstrumentsRepository._load_all_impl` abre el `try` ANTES del
`for _, row in df.iterrows()`: una sola fila que lance aborta el loop y descarta
LAS RESTANTES de esa hoja, dejando solo un `logger.warning`. El vector de disparo
está en `build_instrument`, que usa `float()` pelado (no `_safe_float`, como sí
hacen sus vecinos `cer_base`/`cer_lag`) para `tasa_fija_mensual`/`tem_licit`,
`spread`/`spread_anual` y `cer_spread`/`spread_cer`: una celda con 'n/d' alcanza.

Y el `except` global deja `self._bonos` sin asignar → `get_all_with_meta()`
devuelve `[]`, con lo que `ingest_from_excel` siembra un catálogo VACÍO sin que
nada aborte (el guard anti-pérdida de `reseed_with_meta` no dispara con la DB
vacía: es exactamente el bootstrap de un droplet nuevo).
"""

import pandas as pd
import pytest

from core.infrastructure.repositories import ExcelInstrumentsRepository, build_instrument


def _xlsx(tmp_path, tamar_rows):
    p = tmp_path / "master.xlsx"
    with pd.ExcelWriter(p) as w:
        pd.DataFrame(tamar_rows).to_excel(w, sheet_name="TAMAR", index=False)
        pd.DataFrame([{"ticker": "TTM26", "fecha_pago": "2026-12-31",
                       "amortizacion": 100.0, "cupon_interes": 0.0}]
                     ).to_excel(w, sheet_name="Cashflows", index=False)
    return p


_ROWS = [
    {"ticker": "TTM26", "tipo": "PURO", "fecha_emision": "2025-01-31",
     "fecha_vencimiento": "2026-12-31", "spread": 0.05},
    {"ticker": "TTJ26", "tipo": "DUAL", "fecha_emision": "2025-01-31",
     "fecha_vencimiento": "2026-06-30", "spread": "n/d"},        # <-- celda basura
    {"ticker": "TTS26", "tipo": "PURO", "fecha_emision": "2025-01-31",
     "fecha_vencimiento": "2026-09-30", "spread": 0.03},
]


def test_build_instrument_no_explota_con_una_celda_de_texto():
    inst = build_instrument({"ticker": "TTJ26", "tipo": "DUAL", "spread": "n/d"},
                            "TAMAR", [])
    assert inst is not None
    assert inst.spread_rate is None


@pytest.mark.parametrize("campo", ["spread", "spread_anual", "cer_spread",
                                   "spread_cer", "tasa_fija_mensual", "tem_licit"])
def test_ningun_campo_numerico_opcional_tira_valueerror(campo):
    inst = build_instrument({"ticker": "X1", "tipo": "DUAL", campo: "s/d"}, "TAMAR", [])
    assert inst is not None


def test_una_fila_mala_no_se_lleva_puesta_la_hoja(tmp_path):
    """Las filas POSTERIORES a la mala tienen que seguir cargando."""
    repo = ExcelInstrumentsRepository(str(_xlsx(tmp_path, _ROWS)))
    tickers = {i.ticker for i in repo.get_all_instruments()}
    assert {"TTM26", "TTJ26", "TTS26"} <= tickers, tickers


def test_una_fila_irrecuperable_descarta_solo_esa_fila(tmp_path, caplog):
    """Si una fila igual explota, se descarta ELLA (con su ticker en el log),
    no el resto de la hoja."""
    rows = [dict(r) for r in _ROWS]
    rows[1]["fecha_emision"] = "2025-01-31"
    rows[1]["frecuencia pagos"] = 2
    path = _xlsx(tmp_path, rows)

    import core.infrastructure.repositories as repos
    real = repos.build_instrument

    def boom(row, sheet, cfs):
        if str(row.get("ticker")) == "TTJ26":
            raise RuntimeError("fila corrupta")
        return real(row, sheet, cfs)

    repos.build_instrument = boom
    try:
        with caplog.at_level("WARNING"):
            repo = ExcelInstrumentsRepository(str(path))
    finally:
        repos.build_instrument = real

    tickers = {i.ticker for i in repo.get_all_instruments()}
    assert {"TTM26", "TTS26"} <= tickers, tickers
    assert "TTJ26" not in tickers
    assert any("TTJ26" in r.getMessage() for r in caplog.records), caplog.text


def test_semilla_ilegible_no_devuelve_un_catalogo_vacio_en_silencio(tmp_path):
    """`get_all_with_meta()` es lo que consume `ingest_from_excel`: si la lectura
    del Excel falló, tiene que LANZAR — nunca sembrar 0 filas sin aviso."""
    repo = ExcelInstrumentsRepository(str(tmp_path / "no-existe.xlsx"))
    with pytest.raises(RuntimeError, match="(?i)semilla"):
        repo.get_all_with_meta()


def test_ingest_from_excel_aborta_con_una_semilla_ilegible(tmp_db, tmp_path):
    from core.infrastructure.db.catalog_repository import ingest_from_excel

    with pytest.raises(RuntimeError, match="(?i)semilla"):
        ingest_from_excel(str(tmp_path / "no-existe.xlsx"))


def test_semilla_valida_no_lanza(tmp_path):
    repo = ExcelInstrumentsRepository(str(_xlsx(tmp_path, _ROWS)))
    assert len(repo.get_all_with_meta()) == 3
