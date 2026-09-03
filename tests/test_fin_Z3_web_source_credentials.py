"""Cierre Z3 (ítem 1) — `POST /source/credentials` devolvía **500** por el ValueError
de `save_credentials`.

`apps/web/routers/source.py` validaba el login contra BYMA adentro de un `try/except`
(feedback lindo si la clave está mal) y **después** llamaba a `save_credentials(user,
password)` FUERA de todo `try`. Ese módulo valida su propio contrato —no-vacíos, sin
separadores de línea embebidos (inyección de líneas al `.env`) y sin `=` en el
usuario— y **propaga `ValueError`**. Como `apps/web/app.py` no registra
`exception_handler(ValueError)`, la excepción salía por arriba: **500 con traza**, para
lo que es entrada del usuario.

No es un camino inalcanzable: un usuario BYMA con `=` en el nombre **pasa el probe
OAuth** (login válido, la clave existe de verdad) y recién muere en la persistencia.
Idem un usuario/clave pegado desde un mail con un salto de línea adentro.

Se testean el status, que la respuesta explique el motivo, que el rechazo no persista
nada, y que el otro modo de falla de la misma línea (`.env` no escribible) tampoco
filtre un 500. El contrato del módulo (propagar, no traducir) lo ata
`tests/test_rem_R2_infra_credentials_contrato.py` vía la marca `STATUS-HTTP-REAL`.
"""

from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

from apps.web.app import app
from config.settings import settings
from core.infrastructure.byma.sources import BymaRealtimeSource

pytestmark = pytest.mark.usefixtures("byma_env_limpio")


async def _login_ok(self, stale_token=None):
    """Probe OAuth que ANDA: es lo que hace alcanzable el bug (la clave es válida;
    lo que rompe es la persistencia posterior)."""
    self._token = "tok"
    self._expires_at = float("inf")
    return "tok"


@pytest.fixture
def byma_env_limpio(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "base_dir", tmp_path)   # .env → tmp, no el real
    monkeypatch.setattr(BymaRealtimeSource, "_ensure_token", _login_ok)
    monkeypatch.delenv("BYMADATA_USER", raising=False)
    monkeypatch.delenv("BYMADATA_PASS", raising=False)
    try:
        yield tmp_path
    finally:
        os.environ.pop("BYMADATA_USER", None)
        os.environ.pop("BYMADATA_PASS", None)


def _client():
    # `raise_server_exceptions=False`: sin esto TestClient RE-LANZA la excepción del
    # handler y el test no puede distinguir un 500 de un 400 — que es justo el ítem.
    return TestClient(app, raise_server_exceptions=False)


@pytest.mark.parametrize("usuario, password", [
    pytest.param("us=er", "clave", id="usuario-con-igual"),
    pytest.param("usuario", "cla\nve", id="clave-con-newline"),
    pytest.param("us\u2028er", "clave", id="usuario-con-line-separator"),
])
def test_credenciales_rechazadas_dan_400_y_no_500(usuario, password):
    with _client() as client:
        r = client.post("/source/credentials", data={"user": usuario, "password": password})

    assert r.status_code == 400, (
        f"el ValueError de save_credentials salió como {r.status_code}: el router lo "
        "llama fuera de try/except y apps/web/app.py no registra "
        "exception_handler(ValueError) → 500 con traza para entrada de usuario")
    assert "no permitidos" in r.text, (
        f"la respuesta no dice el motivo del rechazo: {r.text[:300]!r}")


def test_el_rechazo_no_persiste_nada(byma_env_limpio):
    """El 400 tiene que ser además limpio: ni `.env` ni `os.environ` tocados."""
    with _client() as client:
        assert client.post("/source/credentials",
                           data={"user": "us=er", "password": "clave"}).status_code == 400
        assert client.get("/source/status").json()["realtime_ready"] is False

    env = byma_env_limpio / ".env"
    assert not env.is_file() or "BYMADATA_USER" not in env.read_text(encoding="utf-8")
    assert "BYMADATA_USER" not in os.environ


def test_un_env_no_escribible_tampoco_es_un_500(monkeypatch):
    """El otro modo de falla de la MISMA línea: la clave es válida y el contrato pasa,
    pero el `.env` no se puede escribir (permisos, disco lleno, montaje read-only).
    Sin el try/except ese `OSError` sale por arriba igual que el ValueError."""
    def _explota(*a, **kw):
        raise OSError(13, "Permission denied")

    # El router hace `from ... import save_credentials`: se parchea SU nombre, no el
    # del módulo de origen (que ya no se consulta en runtime).
    monkeypatch.setattr("apps.web.routers.source.save_credentials", _explota)

    with _client() as client:
        r = client.post("/source/credentials", data={"user": "u", "password": "p"})

    assert r.status_code == 400, (
        f"un `.env` no escribible salió como {r.status_code} (traza en el log) en vez de "
        "un 400 con el motivo")
    assert "no se pudo guardar" in r.text.lower()


def test_una_clave_valida_sigue_guardando(byma_env_limpio):
    """Ancla: el try/except NO puede tragarse el camino feliz — si `save_credentials`
    dejara de escribir, los tests de arriba seguirían verdes solos."""
    with _client() as client:
        r = client.post("/source/credentials", data={"user": "u", "password": "p"})
        assert r.status_code == 200, r.text
        assert client.get("/source/status").json()["active"] == "byma_realtime"

    assert "BYMADATA_USER=u" in (byma_env_limpio / ".env").read_text(encoding="utf-8")
