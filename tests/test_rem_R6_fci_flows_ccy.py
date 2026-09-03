"""Remediación lote R6 (FCI) — los flujos se separan POR MONEDA DE CLASE en el server.

Regresión que arregla: el fix del hallazgo 4 convertía la serie de flujos con la moneda
del FONDO (`fci.js::flowsARS`), pero los flujos llegan al front YA MERGEADOS entre clases
por `fci_service._store_lookups.flows_lookup`. Sobre el corte real de CAFCI
(`data/history/cafci_diario.json`, 1.096 fondos unificados) hay **95 fondos con clases en
monedas distintas**; 10 quedan rotulados `moneda='USD'` teniendo clases en pesos. Para
esos, el fix multiplicaba por el MEP pesos que ya eran pesos.

Caso medido, textual del store real del usuario (`fci_history.db`, cortes 2026-06-09..
2026-08-31) — 'Alamerica Renta Fija Argentina': `f.moneda = USD` (sus Clases D y E son en
dólares) pero su **Clase I es en pesos** y aporta +3,124e9 de flujo. Con la conversión por
fondo eso se publicaba como +4,374e12 (×1.400), un solo fondo que daba vuelta el signo del
agregado del mercado. Verificado también el caso simétrico ('Compass Renta Fija': clases
A/B/C/H/I en USD + D/E/F en ARS → el total correcto es +2,307e9, no −1,199e9).
"""

from datetime import date

import pytest

from apps.web.fci_service import _ccy_index, _store_lookups
from core.domain.fci.dataset import build_fci_dataset, flows_by_ccy

MEP = 1400.0


class _FakeStore:
    """Store de flujos con la misma superficie que usa `_store_lookups` (keys/get_series).
    Las claves son nombres de CLASE normalizados, como los de ArgentinaDatos."""

    def __init__(self, series):
        self._s = series

    def keys(self):
        return list(self._s)

    def get_series(self, k):
        return self._s.get(k, {})


def _serie(ccp0, ccp1, precio):
    """2 puntos con Δccp = ccp1-ccp0 valuados a `precio` por cuotaparte."""
    return {
        date(2026, 8, 30): {"vcp": precio * 1000.0, "ccp": ccp0, "patrimonio": ccp0 * precio},
        date(2026, 8, 31): {"vcp": precio * 1000.0, "ccp": ccp1, "patrimonio": ccp1 * precio},
    }


# Réplica mínima del caso real: un fondo cuyas clases NO comparten moneda.
_PARSED = [
    {"fondo_id": 7, "clase_id": 71, "fondo_nombre": "Alamerica Renta Fija Argentina",
     "clase_nombre": "Alamerica Renta Fija Argentina - Clase D",
     "moneda": "Dolar Estadounidense", "tipo_renta": "Renta Fija", "rend": {}},
    {"fondo_id": 7, "clase_id": 72, "fondo_nombre": "Alamerica Renta Fija Argentina",
     "clase_nombre": "Alamerica Renta Fija Argentina - Clase I",
     "moneda": "Peso Argentina", "tipo_renta": "Renta Fija", "rend": {}},
]
_STORE = _FakeStore({
    # Clase D (USD): +1.000 cuotapartes a US$100 → +US$100.000
    "alamerica renta fija argentina - clase d": _serie(1_000.0, 2_000.0, 100.0),
    # Clase I (ARS): +2.000 cuotapartes a $1.562.150 → +$3,1243e9 (el flujo real medido)
    "alamerica renta fija argentina - clase i": _serie(1_000.0, 3_000.0, 1_562_150.0),
})


def _lookup(store=_STORE, parsed=_PARSED):
    by_class, by_fondo = _ccy_index(parsed)
    flows_lookup, _hist, _stats = _store_lookups(store, by_class, by_fondo)
    return flows_lookup


def test_flows_lookup_separa_las_clases_por_moneda():
    """El merge de clases NO puede aplanar la moneda: cada clase entra a su bucket."""
    out = _lookup()("Alamerica Renta Fija Argentina")
    assert set(out) == {"ARS", "USD"}
    assert sum(out["ARS"].values()) == pytest.approx(3.1243e9, rel=1e-9)
    assert sum(out["USD"].values()) == pytest.approx(100_000.0, rel=1e-9)


def test_el_agregado_en_pesos_no_multiplica_por_el_mep_la_clase_en_pesos():
    """El número que muestra la vista Flujos. Correcto = 3,1243e9 + 100.000×1.400.
    Convertir el merge con la moneda del fondo (USD) daba 4,374e12: 1.400× de más."""
    out = _lookup()("Alamerica Renta Fija Argentina")
    total = sum(out["ARS"].values()) + sum(out["USD"].values()) * MEP
    assert total == pytest.approx(3.1243e9 + 1.4e8, rel=1e-9)
    merged_como_usd = (sum(out["ARS"].values()) + sum(out["USD"].values())) * MEP
    assert merged_como_usd > 4.3e12                       # lo que publicaba el fix viejo
    assert total < 4.0e9


def test_clase_sin_match_en_cafci_hereda_la_moneda_del_fondo_mono_moneda():
    """El 7,2% de las claves del store no están en CAFCI (clases sin valuación vigente:
    p.ej. Alamerica publica A..J en ArgentinaDatos y CAFCI solo D/E/I). En un fondo
    mono-moneda esa clave resuelve igual por la moneda del fondo."""
    parsed = [dict(_PARSED[0], clase_id=71,
                   clase_nombre="Fondo USD - Clase D")]
    parsed[0]["fondo_nombre"] = "Fondo USD"
    store = _FakeStore({"fondo usd - clase z": _serie(1_000.0, 2_000.0, 100.0)})
    out = _lookup(store, parsed)("Fondo USD")
    assert set(out) == {"USD"}                            # NO cae a pesos


def test_clase_sin_match_en_fondo_mixto_cae_al_default_conservador_ars():
    """Fondo mixto + clave que CAFCI no publica → no hay forma de saber la moneda. El
    default es ARS a propósito: subestimar una clase en dólares es un error acotado;
    sobreestimar una en pesos (×MEP) es el que hacía explotar el agregado."""
    store = _FakeStore({"alamerica renta fija argentina - clase a": _serie(1_000.0, 2_000.0, 10.0)})
    out = _lookup(store)("Alamerica Renta Fija Argentina")
    assert set(out) == {"ARS"}
    assert sum(out["ARS"].values()) == pytest.approx(10_000.0)


def test_ccy_index_marca_None_el_fondo_con_clases_de_monedas_distintas():
    by_class, by_fondo = _ccy_index(_PARSED)
    assert by_class["alamerica renta fija argentina - clase d"] == "USD"
    assert by_class["alamerica renta fija argentina - clase i"] == "ARS"
    assert by_fondo["alamerica renta fija argentina"] is None      # mixto
    by_class, by_fondo = _ccy_index(_PARSED[:1])
    assert by_fondo["alamerica renta fija argentina"] == "USD"     # mono-moneda


# --------------------------------------------------------------------------- #
# El dataset publica las dos patas separadas (contrato con fci.js)
# --------------------------------------------------------------------------- #
def _fund_rec(fid, fondo, clase, moneda):
    return {"fondo_id": fid, "clase_id": fid * 10, "fondo_nombre": fondo,
            "clase_nombre": clase, "moneda": moneda, "tipo_renta": "Renta Fija",
            "vcp": 100.0, "fecha_valor": "2026-08-31", "rend": {}}


def test_build_fci_dataset_publica_flows_y_flows_usd_separados():
    parsed = [_fund_rec(7, "Alamerica Renta Fija Argentina",
                        "Alamerica Renta Fija Argentina - Clase D", "Dolar Estadounidense"),
              _fund_rec(7, "Alamerica Renta Fija Argentina",
                        "Alamerica Renta Fija Argentina - Clase I", "Peso Argentina")]
    ds = build_fci_dataset(parsed, {}, {"mep": {}, "cer": {}, "mep_now": MEP},
                           flows_lookup=_lookup(), hist_lookup=lambda f: {},
                           fecha_base="2026-08-31")
    f = ds["funds"][0]
    assert len(f["flows"]) == 12 and len(f["flows_usd"]) == 12
    assert f["flows"][-1] == pytest.approx(3.1243e9, rel=1e-6)      # pesos, SIN MEP
    assert f["flows_usd"][-1] == pytest.approx(100_000.0, rel=1e-6)  # dólares, sin convertir
    assert f["flows_real"] is True and ds["meta"]["flows_real"] is True


def test_flows_usd_ausente_cuando_no_hay_flujo_en_dolares():
    """Payload lean: los ~1.000 fondos mono-moneda en pesos no cargan un array de ceros."""
    parsed = [_fund_rec(1, "Fondo Pesos", "Fondo Pesos - Clase A", "Peso Argentina")]
    store = _FakeStore({"fondo pesos - clase a": _serie(1_000.0, 2_000.0, 5.0)})
    ds = build_fci_dataset(parsed, {}, {"mep": {}, "cer": {}, "mep_now": MEP},
                           flows_lookup=_lookup(store, parsed), hist_lookup=lambda f: {},
                           fecha_base="2026-08-31")
    f = ds["funds"][0]
    assert "flows_usd" not in f
    assert f["flows"][-1] == pytest.approx(5_000.0) and f["flows_real"] is True


def test_flows_by_ccy_tolera_la_forma_plana():
    """Compatibilidad: un `flows_lookup` que devuelve `{date: flow}` (lookups de test,
    forma vieja) se lee como pesos, no como una moneda inventada."""
    plano = {date(2026, 8, 31): 10.0}
    assert flows_by_ccy(plano) == {"ARS": plano}
    assert flows_by_ccy({}) == {} and flows_by_ccy(None) == {}
    assert flows_by_ccy({"USD": plano}) == {"USD": plano}
