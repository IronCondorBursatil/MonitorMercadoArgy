"""Hallazgo A-7: `save_credentials` escribía `f"{k}={v}"` sin rechazar separadores
de registro, así que un valor con un salto de línea embebido inyectaba una línea
KEY=VALUE arbitraria en el `.env` (que `config.settings._load_dotenv` carga a
`os.environ` en el próximo arranque, y que `clear_credentials` no limpia).

Hardening de defensa en profundidad: el único caller (`POST /source/credentials`)
valida el login contra BYMA antes de persistir, así que no es explotable
end-to-end, pero la función no debe aceptar input que rompa su propio formato.

Ojo con `\x85` / `\u2028`: `str.splitlines()` (que usa el propio round-trip de
`save_credentials` al releer el archivo) los trata como fin de línea, así que
filtrar solo `\n`/`\r` deja el agujero abierto en dos pasos.
"""

import os

import pytest

from core.infrastructure.byma import credentials as cred

_PAYLOADS = [
    "realpass\nMONITOR_JWT_SECRET_KEY=aaaaaaaa",
    "realpass\rMONITOR_JWT_SECRET_KEY=aaaaaaaa",
    "realpass\x85MONITOR_JWT_SECRET_KEY=aaaaaaaa",
    "realpass\u2028MONITOR_JWT_SECRET_KEY=aaaaaaaa",
    "realpass\x00MONITOR_JWT_SECRET_KEY=aaaaaaaa",
]


@pytest.mark.parametrize("payload", _PAYLOADS)
def test_password_con_separador_de_registro_es_rechazada(tmp_path, payload):
    envp = tmp_path / ".env"
    envp.write_text("OTRA=1\n", encoding="utf-8")
    with pytest.raises(ValueError):
        cred.save_credentials("user", payload, path=envp)
    txt = envp.read_text(encoding="utf-8")
    assert "MONITOR_JWT_SECRET_KEY" not in txt
    assert txt == "OTRA=1\n"          # el archivo no se tocó


@pytest.mark.parametrize("payload", _PAYLOADS)
def test_usuario_con_separador_de_registro_es_rechazado(tmp_path, payload):
    envp = tmp_path / ".env"
    envp.write_text("OTRA=1\n", encoding="utf-8")
    with pytest.raises(ValueError):
        cred.save_credentials(payload, "pass", path=envp)
    assert envp.read_text(encoding="utf-8") == "OTRA=1\n"


def test_usuario_con_igual_es_rechazado(tmp_path):
    """`=` en el usuario corre el límite clave/valor del `.env`."""
    envp = tmp_path / ".env"
    envp.write_text("OTRA=1\n", encoding="utf-8")
    with pytest.raises(ValueError):
        cred.save_credentials("u=X", "pass", path=envp)
    assert envp.read_text(encoding="utf-8") == "OTRA=1\n"


def test_password_valida_con_simbolos_sigue_andando(tmp_path, monkeypatch):
    """No romper passwords legítimas: `=`, espacios internos y símbolos van OK.

    `save_credentials` escribe en `os.environ` como efecto de diseño (aplica las
    credenciales EN CALIENTE). Sin `monkeypatch.setenv` previo, ese efecto sobrevivía
    al test y contaminaba el resto de la sesión de pytest: cinco tests dependen de
    que BYMADATA_USER/PASS NO estén (test_byma_credentials, test_source_router,
    test_source_switch) y hoy zafan solo porque cada uno hace su propio `delenv`.
    Registrar las dos claves en monkeypatch ANTES de la llamada hace que pytest las
    restaure al valor previo (o las saque) al terminar."""
    for k in (cred.USER_KEY, cred.PASS_KEY):
        monkeypatch.setenv(k, "__sentinel_pre_test__")

    envp = tmp_path / ".env"
    cred.save_credentials("user", "p4$$= w0rd!#", path=envp)
    txt = envp.read_text(encoding="utf-8")
    assert "BYMADATA_USER=user" in txt
    assert "BYMADATA_PASS=p4$$= w0rd!#" in txt
    # el efecto en caliente sigue siendo parte del contrato (no lo neutralizamos)
    assert os.environ[cred.USER_KEY] == "user"
    assert os.environ[cred.PASS_KEY] == "p4$$= w0rd!#"


def test_el_modulo_no_deja_credenciales_en_el_entorno_de_la_suite():
    """Guard de higiene: ningún test de este archivo puede terminar dejando
    BYMADATA_USER/PASS colgadas en `os.environ` para el resto de la sesión.

    Corre último (orden de definición) y falla si el valor que quedó es el que
    escribe `test_password_valida_con_simbolos_sigue_andando`."""
    assert os.environ.get(cred.USER_KEY) != "user"
    assert os.environ.get(cred.PASS_KEY) != "p4$$= w0rd!#"
