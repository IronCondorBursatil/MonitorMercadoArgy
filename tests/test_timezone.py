"""Zona horaria del proceso (`config.settings.apply_timezone`).

El droplet corre en Etc/UTC y la app usa `datetime.now()` / `date.today()` naive: sin
fijar la TZ del proceso el header mostraba 11:09 en vez de 08:09 y, peor, entre las
21:00 y las 24:00 de Buenos Aires el "hoy" del dominio ya era el día siguiente.
"""

from __future__ import annotations

import os
import time
from datetime import datetime

import pytest

from config.settings import apply_timezone, settings

_UNIX = hasattr(time, "tzset")
_solo_unix = pytest.mark.skipif(not _UNIX, reason="time.tzset() es sólo Unix")
_solo_windows = pytest.mark.skipif(_UNIX, reason="específico de Windows (sin tzset)")


def test_default_es_buenos_aires():
    assert settings.timezone == "America/Argentina/Buenos_Aires"


@_solo_unix
def test_exporta_tz_al_entorno():
    """En Unix `TZ` es lo que lee libc — y lo que heredan los subprocesos."""
    apply_timezone()
    assert os.environ["TZ"] == settings.timezone


@_solo_unix
def test_es_idempotente():
    apply_timezone()
    first = os.environ["TZ"]
    apply_timezone()
    assert os.environ["TZ"] == first


@_solo_unix
def test_hora_local_es_utc_menos_3():
    """El efecto real: en el droplet (UTC) `datetime.now()` tiene que dar la hora de
    Buenos Aires. Argentina no aplica DST, así que el offset es -03:00 fijo."""
    apply_timezone()
    epoch = 1788433771.0        # 2026-09-02T11:09:31Z → 08:09:31 en Buenos Aires
    local = datetime.fromtimestamp(epoch)
    utc = datetime.utcfromtimestamp(epoch)
    assert (utc - local).total_seconds() == 3 * 3600


@_solo_windows
def test_en_windows_no_toca_tz():
    """REGRESIÓN: el CRT de MSVC no parsea nombres IANA y ante un `TZ` inválido cae a
    UTC — exportarla adelantaba 3hs la hora local de la máquina de desarrollo. En
    Windows la TZ del SO ya es la correcta y `apply_timezone()` no debe tocar nada."""
    before = os.environ.get("TZ")
    apply_timezone()
    assert os.environ.get("TZ") == before


def test_timezone_vacia_desactiva_el_override(monkeypatch):
    """`MONITOR_TIMEZONE=""` deja la TZ del sistema intacta."""
    monkeypatch.setattr(settings, "timezone", "")
    before = os.environ.get("TZ")
    apply_timezone()
    assert os.environ.get("TZ") == before
