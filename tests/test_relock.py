"""scripts/relock.py — regenera requirements.lock desde un freeze de Linux (el que publica
el CI) SIN perder la curación a mano del lock: comentarios, secciones y pins de transitivas.

Contrato (agents.md §0.3, regla 2):
  · cada `x==v` del lock se reemplaza por la versión del freeze, conservando lo que haya a
    la derecha (comentario);
  · un paquete pineado en el lock (o declarado en requirements.txt) que NO esté en el freeze
    es error — el freeze no es de un entorno que instaló requirements.txt;
  · una versión del freeze que viole la cota del .txt es error — el CI no debería haber
    pasado con ella;
  · `uvloop` (event loop de prod, sin wheel en Windows) entra con marker
    `; sys_platform != 'win32'` si no estaba;
  · un paquete nuevo del .txt que no tenga línea en el lock se agrega al final;
  · `--check` no escribe: sale 1 si hay cambios (para CI/deps-refresh), 0 si está al día.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _load_relock():
    spec = importlib.util.spec_from_file_location("relock", ROOT / "scripts" / "relock.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


LOCK = """\
# Lockfile — versiones EXACTAS verificadas funcionando (2026-06, Python 3.12).
# comentario de cabecera que debe sobrevivir

# --- web / async ---
fastapi==0.136.3
uvicorn[standard]==0.48.0   # el extra es el que trae httptools
# uvloop: NO se pinea a propósito (comentario viejo que también sobrevive).
httpx==0.28.1
starlette==1.1.0          # transitiva (fastapi/sse-starlette) — pinned: afecta routing/SSE
"""

FREEZE = """\
anyio==4.13.0
fastapi==0.141.1
httptools==0.8.0
httpx==0.28.1
starlette==1.6.0
uvicorn==0.52.4
uvloop==0.21.0
"""

REQS = """\
fastapi>=0.141,<0.150
uvicorn[standard]>=0.52,<1
httpx<1
starlette<2
"""


def test_reemplaza_pins_y_conserva_comentarios_y_extras():
    relock = _load_relock()
    nuevo, cambios = relock.relock(LOCK, relock.parse_freeze(FREEZE), relock.parse_requirements(REQS),
                                   source_label="freeze-test")
    lineas = nuevo.splitlines()
    assert "fastapi==0.141.1" in lineas
    assert "uvicorn[standard]==0.52.4   # el extra es el que trae httptools" in lineas
    assert "starlette==1.6.0          # transitiva (fastapi/sse-starlette) — pinned: afecta routing/SSE" in lineas
    assert "httpx==0.28.1" in lineas                       # sin cambio, queda igual
    assert "# comentario de cabecera que debe sobrevivir" in lineas
    assert "# uvloop: NO se pinea a propósito (comentario viejo que también sobrevive)." in lineas
    assert lineas[0].startswith("# Lockfile — ") and "freeze-test" in lineas[0]


def test_reporta_solo_los_pins_que_cambiaron():
    relock = _load_relock()
    _, cambios = relock.relock(LOCK, relock.parse_freeze(FREEZE), relock.parse_requirements(REQS),
                               source_label="x")
    assert "fastapi 0.136.3 -> 0.141.1" in cambios
    assert "starlette 1.1.0 -> 1.6.0" in cambios
    assert not any(c.startswith("httpx ") for c in cambios)


def test_uvloop_entra_con_marker_de_plataforma():
    relock = _load_relock()
    nuevo, cambios = relock.relock(LOCK, relock.parse_freeze(FREEZE), relock.parse_requirements(REQS),
                                   source_label="x")
    marker = [ln for ln in nuevo.splitlines() if ln.startswith("uvloop==0.21.0")]
    assert len(marker) == 1, nuevo
    assert "sys_platform != 'win32'" in marker[0]
    assert "uvloop (nuevo) -> 0.21.0" in cambios
    # idempotente: una segunda pasada sobre el resultado no duplica la línea
    otra, _ = relock.relock(nuevo, relock.parse_freeze(FREEZE), relock.parse_requirements(REQS),
                            source_label="x")
    assert otra.count("uvloop==") == 1


def test_paquete_del_lock_ausente_en_el_freeze_es_error():
    relock = _load_relock()
    freeze = relock.parse_freeze(FREEZE.replace("starlette==1.6.0\n", ""))
    with pytest.raises(relock.RelockError, match="starlette"):
        relock.relock(LOCK, freeze, relock.parse_requirements(REQS), source_label="x")


def test_version_del_freeze_que_viola_la_cota_es_error():
    relock = _load_relock()
    freeze = relock.parse_freeze(FREEZE.replace("fastapi==0.141.1", "fastapi==0.140.0"))
    with pytest.raises(relock.RelockError, match=r"fastapi.*0\.140\.0"):
        relock.relock(LOCK, freeze, relock.parse_requirements(REQS), source_label="x")


def test_paquete_nuevo_del_txt_se_agrega_al_final():
    relock = _load_relock()
    reqs = relock.parse_requirements(REQS + "anyio>=4\n")
    nuevo, cambios = relock.relock(LOCK, relock.parse_freeze(FREEZE), reqs, source_label="x")
    assert nuevo.rstrip().endswith("anyio==4.13.0")
    assert "anyio (nuevo) -> 4.13.0" in cambios


def test_check_no_escribe_y_sale_1_si_hay_cambios(tmp_path: Path):
    relock = _load_relock()
    lock = tmp_path / "requirements.lock"
    lock.write_text(LOCK, encoding="utf-8")
    (tmp_path / "requirements.txt").write_text(REQS, encoding="utf-8")
    freeze = tmp_path / "freeze.txt"
    freeze.write_text(FREEZE, encoding="utf-8")

    rc = relock.main([str(freeze), "--lock", str(lock), "--requirements",
                      str(tmp_path / "requirements.txt"), "--check"])
    assert rc == 1
    assert lock.read_text(encoding="utf-8") == LOCK, "--check no debe escribir"

    rc = relock.main([str(freeze), "--lock", str(lock), "--requirements",
                      str(tmp_path / "requirements.txt")])
    assert rc == 0
    assert "fastapi==0.141.1" in lock.read_text(encoding="utf-8")

    rc = relock.main([str(freeze), "--lock", str(lock), "--requirements",
                      str(tmp_path / "requirements.txt"), "--check"])
    assert rc == 0, "tras regenerar, --check está al día"
