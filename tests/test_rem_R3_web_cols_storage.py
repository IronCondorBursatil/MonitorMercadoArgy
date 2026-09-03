"""Remediación R3_web (lote D2, cosmético del fix 4) — la clave de localStorage que
guarda las columnas ocultas tiene que versionarse cuando cambian las columnas.

`pages/index.html` guarda el ocultamiento por ÍNDICE de columna
(`panel-cols-vN` = {gs-id: [colIndex]}). Al sacar "Sector" de PROVINCIALES los
índices se corrieron: a un usuario que tuviera columnas ocultas ahí, el deploy le
habría ocultado OTRA columna (silenciosamente, y sin forma de darse cuenta salvo
volviendo a abrir el Config). La preferencia vieja hay que descartarla, no aplicarla
mal: se bumpea la clave.

El test pinea la clave VIVA junto con la huella del layout de columnas: si mañana
alguien vuelve a mover columnas sin bumpear, esto se pone rojo y le dice qué hacer.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from apps.web.panels_rows import panel_columns
from apps.web.routers.panels_schema import PANEL_ORDER

_INDEX = Path(__file__).resolve().parent.parent / "apps" / "web" / "templates" / "pages" / "index.html"

# Clave viva y huella del layout al que corresponde. Para bumpear: subí la versión en
# index.html (las 2 lecturas/escrituras + el removeItem del reset, dejando el
# removeItem de la anterior) y actualizá estas dos constantes.
_CLAVE = "panel-cols-v2"
_HUELLA = "8ccf6bd6d902e94dc730a6a4bd3cf764bdf7b206"


def _huella_layout() -> str:
    layout = {pid: [c["key"] for c in panel_columns(pid)] for pid in PANEL_ORDER}
    return hashlib.sha1(json.dumps(layout, sort_keys=True).encode()).hexdigest()


def test_la_clave_de_columnas_ocultas_corresponde_al_layout_actual():
    assert _huella_layout() == _HUELLA, (
        "cambiaron las columnas de algún panel: el ocultamiento guardado por índice "
        f"le va a ocultar al usuario la columna equivocada. Bumpeá '{_CLAVE}' en "
        "pages/index.html y actualizá _CLAVE/_HUELLA en este test.")


def test_index_usa_esa_clave_para_leer_y_escribir():
    src = _INDEX.read_text(encoding="utf-8")
    usos = re.findall(r'ls(?:Get|Set)\("(panel-cols-v\d+)"', src)
    assert usos, "no encontré el manejo de columnas ocultas en index.html"
    assert set(usos) == {_CLAVE}, (
        f"index.html lee/escribe {set(usos)} y el layout actual corresponde a "
        f"'{_CLAVE}': una preferencia guardada con otro layout oculta otra columna")


def test_el_reset_del_config_limpia_tambien_la_clave_vieja():
    """'Restaurar default' tiene que barrer la versión anterior: si no, queda basura
    en el localStorage del usuario para siempre."""
    src = _INDEX.read_text(encoding="utf-8")
    borradas = set(re.findall(r'removeItem\("(panel-cols-v\d+)"\)', src))
    assert _CLAVE in borradas, "el reset no limpia la clave viva"
    assert len(borradas) >= 2, (
        f"el reset sólo limpia {borradas}: la clave de la versión anterior queda "
        "colgada en el navegador")
