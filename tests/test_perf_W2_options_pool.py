"""La chain de opciones en varios cores.

Es el único trabajo del sistema que se beneficia de tener 4 cores: CPU puro bajo el
GIL, medido en **6,08 s para 458 contratos** en el ARM del servidor, con cada contrato
independiente del resto (~20 valuaciones CRR para la IV + 6 para los griegos, sobre
funciones puras). Mientras corre, compite con el event loop que sirve el SSE y con el
ciclo de precios — de ahí que estuviera espaciado a 60 s.

Lo que estos tests protegen, en orden de importancia:
  1. que el resultado paralelo sea EXACTAMENTE el serial (mismo contenido, mismo orden);
  2. que un pool roto no pierda el ciclo;
  3. que `executor=None` deje el camino de siempre intacto, byte por byte.
"""

from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path

import pytest

from core.domain.options import chain as C
from core.infrastructure.schemas import Data912Row

ROOT = Path(__file__).resolve().parent.parent


def _row(symbol, **kw):
    return Data912Row(symbol=symbol, c=kw.get("c", 0.0), px_bid=kw.get("px_bid", 0.0),
                      px_ask=kw.get("px_ask", 0.0), v=kw.get("v", 0.0),
                      q_op=kw.get("q_op", 0), pct_change=kw.get("pct_change", 0.0),
                      oi=kw.get("oi"), opt_kind=kw.get("opt_kind"),
                      opt_underlying=kw.get("opt_underlying"),
                      opt_expiry=kw.get("opt_expiry"))


def _chain(n: int):
    """`n` contratos de GGAL con strikes distintos, en el formato REAL de ticker BYMA
    (`GFG` + C/V + strike con decimal implícito + código de mes)."""
    opciones = {}
    spot = 7220.0
    for i in range(n):
        strike_cod = 60000 + i * 500          # -> 6000.0, 6050.0, ...
        for kind in ("C", "V"):
            sym = f"GFG{kind}{strike_cod}J"
            opciones[sym] = _row(sym, c=480.0, px_bid=475.0, px_ask=480.0,
                                 v=1000.0, q_op=10)
    return opciones, {"GGAL": _row("GGAL", c=spot)}


@pytest.fixture(scope="module")
def datos():
    opciones, acciones = _chain(20)          # 20 strikes x 2 tipos = 40 contratos
    serial = C.build_options(opciones, acciones, r=0.0, N=20)
    assert len(serial) >= 30, f"la fixture no produjo chain: {len(serial)}"
    return opciones, acciones, serial


# ── 1. equivalencia ────────────────────────────────────────────────────────
def test_el_paralelo_da_exactamente_lo_mismo_que_el_serial(datos):
    """Con un `ThreadPoolExecutor` (mismo ABC, sin pickling) para que el test sea
    rápido: lo que se verifica acá es la ORQUESTACIÓN, no el transporte."""
    opciones, acciones, serial = datos
    with ThreadPoolExecutor(2) as ex:
        paralelo = C.build_options(opciones, acciones, r=0.0, N=20, executor=ex)
    assert paralelo == serial


def test_el_orden_se_preserva(datos):
    """`executor.map` respeta el orden de los lotes; si alguien lo cambiara por
    `as_completed` la chain saldría barajada y el scanner ordenaría sobre otra cosa."""
    opciones, acciones, serial = datos
    with ThreadPoolExecutor(3) as ex:
        paralelo = C.build_options(opciones, acciones, r=0.0, N=20, executor=ex, chunk_size=3)
    assert [i.contract.ticker for i in paralelo] == [i.contract.ticker for i in serial]


@pytest.mark.parametrize("chunk", [1, 3, 7, 1000])
def test_el_tamano_de_lote_no_cambia_el_resultado(datos, chunk):
    opciones, acciones, serial = datos
    with ThreadPoolExecutor(2) as ex:
        assert C.build_options(opciones, acciones, r=0.0, N=20, executor=ex, chunk_size=chunk) == serial


# ── 2. el pool roto no pierde el ciclo ─────────────────────────────────────
class _ExecutorRoto:
    def __init__(self, fallar_en=0):
        self._max_workers = 2
        self.fallar_en = fallar_en

    def map(self, fn, iterable):
        from concurrent.futures.process import BrokenProcessPool

        for i, x in enumerate(iterable):
            if i >= self.fallar_en:
                raise BrokenProcessPool("worker muerto")
            yield fn(x)


def test_un_pool_roto_recalcula_en_serie(datos, caplog):
    """Un worker que muere no puede costar el ciclo entero: el resultado tiene que ser
    el mismo, sólo tardando lo que tardaba antes."""
    opciones, acciones, serial = datos
    assert C.build_options(opciones, acciones, r=0.0, N=20, executor=_ExecutorRoto()) == serial


def test_el_pool_roto_queda_registrado(datos, caplog):
    """Silenciarlo dejaría al sistema corriendo en serie para siempre sin que nadie
    se entere de por qué la chain volvió a tardar 6 segundos."""
    import logging

    opciones, acciones, _ = datos
    with caplog.at_level(logging.WARNING):
        C.build_options(opciones, acciones, r=0.0, N=20, executor=_ExecutorRoto())
    assert any("pool" in r.getMessage().lower() for r in caplog.records)


# ── 3. el camino serial intacto ────────────────────────────────────────────
def test_sin_executor_no_se_reparte_nada(datos, monkeypatch):
    """`executor=None` es el default y tiene que ser el código de siempre: si alguien
    lo rompe, TODAS las llamadas existentes (tests, bench, el arranque con el pool
    desactivado) cambian de comportamiento."""
    opciones, acciones, serial = datos
    llamado = []
    monkeypatch.setattr(C, "_enriquecer_en_paralelo",
                        lambda *a, **k: llamado.append(1) or [])
    assert C.build_options(opciones, acciones, r=0.0, N=20) == serial
    assert not llamado


def test_una_chain_chica_no_paga_el_reparto(datos, monkeypatch):
    """Debajo del umbral, repartir entre procesos cuesta más que calcular."""
    opciones, acciones, _ = datos
    pocas = dict(list(opciones.items())[:4])
    llamado = []
    monkeypatch.setattr(C, "_enriquecer_en_paralelo",
                        lambda *a, **k: llamado.append(1) or [])
    with ThreadPoolExecutor(2) as ex:
        C.build_options(pocas, acciones, r=0.0, N=20, executor=ex)
    assert not llamado, "repartió una chain de 4 contratos"


# ── 4. lo que hace falta para que el pool de PROCESOS funcione ─────────────
def test_lo_que_cruza_el_limite_de_proceso_es_picklable(datos):
    """`_Prep` es de primitivos a propósito: si volviera a llevar un `Data912Row`
    (pydantic), cada contrato pagaría serializar un modelo entero — y los workers
    tendrían que importar el modelo de ingesta."""
    import pickle

    opciones, acciones, _ = datos
    preps, _skip = C._preparar(opciones, acciones, date.today())
    assert preps
    ida_y_vuelta = pickle.loads(pickle.dumps(preps))
    assert ida_y_vuelta == preps
    items = C._enriquecer_lote(preps[:2], 0.0, 0.0, 20)
    assert pickle.loads(pickle.dumps(items)) == items


def test_el_worker_no_hereda_los_handlers_de_log():
    """Bajo `spawn` el hijo re-importa `__main__` (o sea `run.py`) y vuelve a llamar a
    `setup_logging()`: quedarían DOS procesos con un RotatingFileHandler sobre el mismo
    archivo, rotándolo a la vez."""
    import logging

    from apps.web.app import _init_worker_de_opciones

    raiz = logging.getLogger()
    previos = list(raiz.handlers)
    try:
        raiz.addHandler(logging.StreamHandler())
        _init_worker_de_opciones()
        assert all(isinstance(h, logging.NullHandler) for h in raiz.handlers)
    finally:
        for h in list(raiz.handlers):
            raiz.removeHandler(h)
        for h in previos:
            raiz.addHandler(h)


def test_el_pool_usa_spawn_y_no_fork():
    """`fork` desde un proceso multithread (anyio, to_thread, SQLite, httpx) arrastra
    locks tomados por otros hilos; Python 3.12 avisa del riesgo de deadlock."""
    import inspect

    from apps.web import app as A

    # Mirar el CÓDIGO, no la prosa: el docstring de la función explica justamente por
    # qué no usa fork, y un `assert "fork" not in src` se dispara con la explicación.
    src = inspect.getsource(A._crear_pool_de_opciones)
    _, _, cuerpo = src.partition('"""')
    _, _, codigo = cuerpo.partition('"""')          # todo lo que sigue al docstring
    codigo = "\n".join(linea.split("#", 1)[0] for linea in codigo.splitlines())
    assert 'get_context("spawn")' in codigo
    assert "fork" not in codigo, "el pool forkea desde un proceso multithread"


def test_la_perilla_apaga_el_pool(monkeypatch):
    from config.settings import settings

    from apps.web import app as A

    monkeypatch.setattr(settings, "options_workers", 0)
    monkeypatch.delenv("MONITOR_DISABLE_LOOPS", raising=False)
    assert A._crear_pool_de_opciones() is None


# ── Hallazgos de la auditoría 2026-09-04 ─────────────────────────────────────

def test_el_worker_no_arrastra_la_app_web():
    """`multiprocessing` picklea el `initializer=` por nombre calificado, así que cada
    hijo `spawn` importa el módulo que lo DEFINE. Con el initializer en
    `apps/web/app.py`, cada worker importaba FastAPI, los 16 routers, SQLAlchemy, httpx
    y pydantic para después correr código de dominio que no toca nada de eso — lo
    contrario de la razón declarada del split prepare/enrich.

    Se comprueba en un intérprete limpio: importar el módulo que define el initializer
    NO puede traer `fastapi`."""
    import subprocess
    import sys

    codigo = (
        "import sys;"
        "import core.domain.options.chain as c;"
        "assert callable(c.init_worker);"
        "web = [m for m in sys.modules if m.startswith(('fastapi', 'apps.web'))];"
        "print('WEB=%d' % len(web))"
    )
    r = subprocess.run([sys.executable, "-c", codigo], capture_output=True, text=True,
                       cwd=str(ROOT))
    assert r.returncode == 0, r.stderr
    assert "WEB=0" in r.stdout, (
        f"el módulo del worker arrastra la app web: {r.stdout.strip()}")


def test_el_initializer_de_la_app_delega_en_el_del_dominio():
    """El nombre viejo se conserva (lo referencian `_crear_pool_de_opciones` y sus
    tests), pero tiene que ser una cáscara: si el cuerpo vuelve a `app.py`, vuelve el
    import de 162 MB por worker."""
    import inspect

    from apps.web.app import _init_worker_de_opciones

    fuente = inspect.getsource(_init_worker_de_opciones)
    assert "from core.domain.options.chain import init_worker" in fuente
    assert "removeHandler" not in fuente, "el cuerpo volvió a la app web"


def test_un_pool_CERRADO_no_recalcula_lo_que_se_va_a_tirar():
    """En el `finally` del lifespan las tasks se cancelan y se awaitean ANTES del
    `pool.shutdown(cancel_futures=True)`. Pero cancelar un `asyncio.to_thread` NO
    detiene el hilo: `build_options` sigue corriendo huérfano. Cuando llega el
    `cancel_futures`, los lotes pendientes se cancelan y —sin este corte— el fallback
    recalculaba los 458 contratos EN SERIE, segundos, para tirar el resultado y
    demorar el stop.

    Un pool roto de verdad SÍ tiene que recalcular: `BrokenProcessPool` hereda de
    RuntimeError, así que la distinción es por tipo y el orden importa."""
    from concurrent.futures import CancelledError
    from concurrent.futures.process import BrokenProcessPool

    from core.domain.options.chain import _es_apagado

    assert _es_apagado(CancelledError(), None) is True
    assert _es_apagado(RuntimeError("cannot schedule new futures after shutdown"),
                       None) is True
    assert _es_apagado(BrokenProcessPool("worker muerto"), None) is False, (
        "un pool roto de verdad tiene que seguir recalculando en serie")
    assert _es_apagado(ValueError("otra cosa"), None) is False


def test_el_apagado_no_pasa_por_el_camino_serial(monkeypatch):
    """El extremo a extremo del anterior: con el pool cerrándose, `_enriquecer` no se
    llama ni una vez."""
    from concurrent.futures import CancelledError

    from core.domain.options import chain

    llamadas = []
    monkeypatch.setattr(chain, "_enriquecer",
                        lambda *a, **k: llamadas.append(1) or {})

    class _Cerrado:
        _max_workers = 3

        def map(self, *a, **kw):
            raise CancelledError()

    with pytest.raises(CancelledError):
        chain._enriquecer_en_paralelo([object()] * 50, 0.5, 0.0, 80, _Cerrado(), None)
    assert llamadas == [], f"recalculó {len(llamadas)} contratos para tirarlos"
