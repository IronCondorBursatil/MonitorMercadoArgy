"""Las escrituras de runtime salen del working tree de git.

La auditoría de 2026-09 sacó las `.db`, los backups y el estado de las series a
`db_dir` porque un `git clean -xfd` —el reflejo para destrabar un `git pull`
conflictivo— se llevaba la fuente de verdad. Quedaron adentro tres:

  * `data/cartera.json` — las TENENCIAS del usuario. Gitignorado, así que `git clean`
    lo borra sin preguntar, y `_check_db_paths` no lo miraba.
  * `monitores_global.log` — rota con **rename en la raíz del repo**, que es lo que
    impide poner `ProtectSystem=strict` en el unit de systemd.
  * `.env` — se queda a propósito: `_load_dotenv` corre ANTES de que exista
    `Settings`, y la UI de credenciales BYMA lo reescribe. Está en el bundle de backup.

Este archivo fija que los dos primeros cuelguen de `db_dir` y que el guard los cubra.
"""

from datetime import date, timedelta
from pathlib import Path

import pytest


# ── 1. Los campos derivan de db_dir como el resto ──────────────────────────
def test_cartera_y_log_cuelgan_de_db_dir():
    """Un `MONITOR_DB_DIR` reubica TODO el conjunto. Si estos dos no están en
    `_DB_DERIVED`, siguen cayendo en el árbol aunque el operador mueva el resto."""
    from config.settings import _DB_DERIVED

    assert _DB_DERIVED.get("cartera_json") == "cartera.json"
    assert _DB_DERIVED.get("log_file") == "monitores_global.log"


def test_un_solo_env_reubica_cartera_y_log(monkeypatch, tmp_path):
    from config.settings import Settings

    monkeypatch.setenv("MONITOR_DB_DIR", str(tmp_path))
    for var in ("MONITOR_CARTERA_JSON", "MONITOR_LOG_FILE"):
        monkeypatch.delenv(var, raising=False)
    s = Settings()
    assert s.cartera_json == tmp_path / "cartera.json"
    assert s.log_file == tmp_path / "monitores_global.log"


def test_el_guard_de_arbol_los_denuncia(monkeypatch, tmp_path, caplog):
    """`_check_db_paths` recorre `_DB_DERIVED`: al entrar ahí, cartera y log quedan
    cubiertos gratis. Sin esto, el aviso de 'bases adentro del árbol' seguía siendo
    ciego justo para el archivo con los datos del usuario."""
    import logging

    from config.settings import Settings

    monkeypatch.setenv("MONITOR_DB_DIR", str(tmp_path))
    # `base_dir` es la raíz del repo; apuntar db_dir adentro tiene que gritar.
    repo = Path(__file__).resolve().parent.parent
    monkeypatch.setenv("MONITOR_CARTERA_JSON", str(repo / "data" / "cartera.json"))
    with caplog.at_level(logging.ERROR):
        Settings()
    assert any("working tree" in r.message or "working tree" in str(r.args)
               for r in caplog.records), "el guard no denunció cartera.json en el árbol"


# ── 2. La migración del archivo viejo ──────────────────────────────────────
def test_la_cartera_vieja_se_migra_sola(monkeypatch, tmp_path):
    """Al mover la ruta, la cartera que ya existe en `data/` tiene que aparecer en la
    nueva ubicación sin que el usuario haga nada — si no, abre el panel y no tiene
    tenencias. Se COPIA (no se mueve): un rollback a la versión anterior tiene que
    seguir encontrando el archivo donde lo dejó."""
    import json

    from apps.web import cartera_store

    legacy = tmp_path / "data" / "cartera.json"
    legacy.parent.mkdir(parents=True)
    legacy.write_text(json.dumps({"holdings": [{"ticker": "AL30", "nominales": 100}]}),
                      encoding="utf-8")
    nuevo = tmp_path / "db" / "cartera.json"
    nuevo.parent.mkdir(parents=True)

    monkeypatch.setattr(cartera_store, "_PATH", str(nuevo))
    monkeypatch.setattr(cartera_store, "_LEGACY_PATH", str(legacy))

    assert cartera_store.list_holdings() == [{"ticker": "AL30", "nominales": 100}]
    assert nuevo.is_file(), "no se creó el archivo en la ubicación nueva"
    assert legacy.is_file(), "el legacy se movió en vez de copiarse (rompe el rollback)"


def test_sin_legacy_no_inventa_nada(monkeypatch, tmp_path):
    from apps.web import cartera_store

    monkeypatch.setattr(cartera_store, "_PATH", str(tmp_path / "cartera.json"))
    monkeypatch.setattr(cartera_store, "_LEGACY_PATH", str(tmp_path / "no-existe.json"))
    assert cartera_store.list_holdings() == []
    assert not (tmp_path / "cartera.json").exists()


# ── 3. Poda de fci_history (§1.7) ──────────────────────────────────────────
def test_fci_history_tiene_ventana():
    """Era el único store sin poda: se carga ENTERO en RAM y crecía sin techo
    (~4.700 filas/día). El read-path usa 12 meses (`monthly_net_flows(n=12)`)."""
    from config.settings import settings

    assert settings.fci_history_keep_days >= 366, (
        "la ventana tiene que cubrir los 12 meses que lee `monthly_net_flows` más el "
        "punto previo que necesita `net_flow_series`")


def test_la_poda_borra_lo_viejo_y_conserva_el_dia_limite(tmp_path):
    from core.infrastructure.fci_history import FCIHistoryStore

    store = FCIHistoryStore(tmp_path / "fci.db")
    hoy = date(2026, 9, 4)
    corte = hoy - timedelta(days=400)
    fila = [{"fondo": "F", "vcp": 1.0, "ccp": 10.0, "patrimonio": 10.0}]
    store.record_snapshot(fila, corte - timedelta(days=1))
    store.record_snapshot(fila, corte)
    store.record_snapshot(fila, hoy)
    assert store.prune(corte) == 1, "tenía que borrar sólo la fila anterior al corte"
    dias = set(store.get_series("F"))
    assert corte in dias, "borró el día límite (el corte es inclusivo hacia adelante)"
    assert hoy in dias
    assert (corte - timedelta(days=1)) not in dias


def test_la_poda_invalida_el_cache(tmp_path):
    """El store cachea la tabla entera en RAM; sin invalidar, la lectura siguiente
    devuelve las filas que se acaban de borrar."""
    from core.infrastructure.fci_history import FCIHistoryStore

    store = FCIHistoryStore(tmp_path / "fci.db")
    store.record_snapshot([{"fondo": "F", "vcp": 1.0, "ccp": 1.0, "patrimonio": 1.0}],
                          date(2020, 1, 1))
    assert store.get_series("F")                  # puebla el cache
    store.prune(date(2026, 1, 1))
    assert store.get_series("F") == {}, "el cache sobrevivió a la poda"


def test_la_poda_no_lanza_si_la_base_esta_rota(tmp_path):
    """Corre adentro de un loop supervisado: un error de SQLite tiene que devolver 0,
    no tumbar el `_price_history_loop`."""
    from core.infrastructure.fci_history import FCIHistoryStore

    roto = tmp_path / "roto.db"
    roto.write_bytes(b"esto no es una base sqlite")
    assert FCIHistoryStore(roto).prune(date(2026, 1, 1)) == 0


@pytest.mark.parametrize("modulo,fn", [("apps.web.app", "_price_history_loop")])
def test_el_loop_poda_fci_ademas_de_precios(modulo, fn):
    """La poda tiene que estar CABLEADA, no sólo existir."""
    import inspect

    src = inspect.getsource(__import__(modulo, fromlist=[fn]).__dict__[fn])
    assert "fci_history_keep_days" in src, "el loop no poda fci_history"
