"""Data-source quality & resilience suite.

Run: `python -m scripts.data_quality_check` (from project root).

Verifies every external feed the project depends on against a checklist:
  - connectivity (HTTP/TCP reachable)
  - latency (min / p50 / p95 / max over N samples)
  - schema (expected fields present in payload)
  - value sanity (ranges, monotonicity, freshness)
  - cache behavior (TTL coalesces in-cycle calls)
  - concurrency (thread-safe under contention)
  - persistence (disk hydration + atomic write + offline fallback)

The data feeds are critical: the project has no alternative source for any
of them, so silent corruption or outages have to be caught early. Exit code
is 0 only if every CRITICAL check passed (warnings still allowed).
"""

from __future__ import annotations

import json
import os
import ssl
import sys
import tempfile
import time
import traceback
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from typing import Callable, List, Optional, Tuple

# Project root on sys.path so this script can be run directly (python scripts/...)
# without -m. -m form already handles it.
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

# Windows consoles default to cp1252 which can't print Unicode arrows etc.
# Force UTF-8 (Python 3.7+) so colorized output and non-ASCII detail strings
# survive the trip to stdout.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, OSError):
    pass

# ---------------------------------------------------------------- report state
PASS, WARN, FAIL = "PASS", "WARN", "FAIL"
_results: List[Tuple[str, str, str, str]] = []  # (group, check, status, detail)
_t0 = time.monotonic()


def _record(group: str, check: str, status: str, detail: str = "") -> None:
    _results.append((group, check, status, detail))
    color = {"PASS": "\033[32m", "WARN": "\033[33m", "FAIL": "\033[31m"}.get(status, "")
    reset = "\033[0m" if color else ""
    label = f"{check:.<28}"
    print(f"  {label} {color}{status}{reset}  {detail}")


def _section(title: str) -> None:
    print(f"\n[{title}]")


def _try(group: str, check: str, fn: Callable[[], Optional[str]], critical: bool = True) -> None:
    """Run `fn` — returns detail string on success, raises on failure.
    Status = PASS on success, FAIL (critical) or WARN (non-critical) on exception.
    """
    try:
        detail = fn() or ""
        _record(group, check, PASS, detail)
    except AssertionError as e:
        _record(group, check, FAIL if critical else WARN, str(e))
    except Exception as e:
        msg = f"{type(e).__name__}: {e}"
        _record(group, check, FAIL if critical else WARN, msg)


# ---------------------------------------------------------------- HTTP helper
_SSL_CTX = ssl._create_unverified_context()
_UA = "monitor-diagnostic/1.0"


def _http_get(url: str, timeout: float = 10.0) -> Tuple[int, bytes, float]:
    """GET url, return (status, body, elapsed_seconds)."""
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    start = time.monotonic()
    with urllib.request.urlopen(req, timeout=timeout, context=_SSL_CTX) as r:
        body = r.read()
        status = r.status
    return status, body, time.monotonic() - start


def _latency_samples(url: str, n: int = 5, sleep_s: float = 0.2) -> List[float]:
    """Sample latency N times with small inter-request sleep (avoid hammering)."""
    samples: List[float] = []
    for _ in range(n):
        _, _, dt = _http_get(url)
        samples.append(dt * 1000.0)  # ms
        time.sleep(sleep_s)
    return samples


def _fmt_latency(ms: List[float]) -> str:
    if not ms:
        return "no samples"
    s = sorted(ms)
    p50 = s[len(s) // 2]
    p95 = s[min(len(s) - 1, int(0.95 * len(s)))]
    return f"min={min(s):.0f} p50={p50:.0f} p95={p95:.0f} max={max(s):.0f} ms (n={len(s)})"


# ============================================================================
# 1. DATA912
# ============================================================================
def test_data912() -> None:
    _section("1. DATA912 — precios live de bonos/notas/corporativos")
    from core.infrastructure.repositories import Data912MarketDataProvider as P
    endpoints = P.ENDPOINTS

    # Connectivity per endpoint
    statuses, payloads, latencies = {}, {}, []
    for name, url in endpoints.items():
        def go(u=url, n=name):
            st, body, dt = _http_get(u, timeout=10)
            assert st == 200, f"HTTP {st}"
            data = json.loads(body)
            assert isinstance(data, list), f"expected list, got {type(data).__name__}"
            statuses[n] = st
            payloads[n] = data
            latencies.append(dt * 1000.0)
            return f"{len(data)} rows in {dt*1000:.0f}ms"
        _try("data912", f"GET {name}", go)

    # Aggregate latency over N reps on the busiest endpoint
    if "bonds" in endpoints:
        _try("data912", "latency × 5 (bonds)",
             lambda: _fmt_latency(_latency_samples(endpoints["bonds"], n=5)))

    # Schema: each row must carry the fields fetch_snapshots consumes
    REQUIRED = ("symbol", "c")
    OPTIONAL = ("px_bid", "px_ask", "v", "q_op", "pct_change")
    def schema_check():
        rows = sum(payloads.values(), [])
        assert rows, "no rows in any endpoint"
        missing_req = [r for r in rows if not all(k in r for k in REQUIRED)]
        assert not missing_req, f"{len(missing_req)} rows missing required {REQUIRED}"
        cov = {k: sum(1 for r in rows if k in r) / len(rows) for k in OPTIONAL}
        return f"{len(rows)} rows · coverage " + ", ".join(f"{k}={v:.0%}" for k, v in cov.items())
    _try("data912", "schema", schema_check)

    # Value sanity: prices > 0, in reasonable AR range; known tickers present
    KNOWN = {"AL30", "GD30", "S29Y6", "TZX26"}
    def sanity():
        rows = sum(payloads.values(), [])
        bad_prices = [r for r in rows if r.get("c") is not None and not (0 < float(r["c"]) < 1_000_000)]
        assert not bad_prices, f"{len(bad_prices)} rows with implausible price (e.g. {bad_prices[0]})"
        syms = {str(r.get("symbol", "")).upper() for r in rows}
        found = KNOWN & syms
        assert found, f"none of the known tickers {KNOWN} are present"
        return f"{len(found)}/{len(KNOWN)} known tickers found; price range plausible"
    _try("data912", "value sanity", sanity)

    # Cache TTL — same provider instance called rapidly should coalesce
    def cache_test():
        prov = P()
        prov.fetch_snapshots(["AL30"])
        t1 = time.monotonic()
        for _ in range(8):
            prov.fetch_snapshots(["AL30"])
        elapsed = (time.monotonic() - t1) * 1000.0
        assert elapsed < 100, f"8 cached calls took {elapsed:.0f}ms (>100ms = no cache)"
        return f"8 in-TTL calls served from cache in {elapsed:.0f}ms"
    _try("data912", "cache TTL coalesces", cache_test)

    # Concurrency: 10 threads hammering the provider
    def concurrent():
        prov = P()
        errors = []
        def worker():
            try:
                prov.fetch_snapshots(["AL30", "GD30"])
            except Exception as e:
                errors.append(e)
        with ThreadPoolExecutor(max_workers=10) as ex:
            list(ex.map(lambda _: worker(), range(10)))
        assert not errors, f"{len(errors)} thread errors (e.g. {errors[0]})"
        return "10 threads OK"
    _try("data912", "concurrency × 10", concurrent)


# ============================================================================
# 2. DOLARAPI
# ============================================================================
def test_dolarapi() -> None:
    _section("2. DOLARAPI — USD/ARS quotes")
    from core.infrastructure.fx_provider import DolarAPIProvider

    URL = DolarAPIProvider.URL
    payload = {"data": None}

    def conn():
        st, body, dt = _http_get(URL)
        assert st == 200, f"HTTP {st}"
        data = json.loads(body)
        assert isinstance(data, list), f"expected list"
        payload["data"] = data
        return f"{len(data)} casas in {dt*1000:.0f}ms"
    _try("dolarapi", "GET /v1/dolares", conn)

    _try("dolarapi", "latency × 5",
         lambda: _fmt_latency(_latency_samples(URL, n=5)))

    def schema():
        rows = payload["data"] or []
        REQUIRED = {"casa", "compra", "venta", "fechaActualizacion"}
        for r in rows:
            missing = REQUIRED - set(r.keys())
            assert not missing, f"row for {r.get('casa')} missing {missing}"
        casas = {r["casa"] for r in rows}
        for needed in ("mayorista", "oficial", "blue"):
            assert needed in casas, f"missing critical casa '{needed}'"
        return f"all rows have {REQUIRED}; mayorista/oficial/blue present"
    _try("dolarapi", "schema", schema)

    def sanity():
        rows = payload["data"] or []
        by_casa = {r["casa"]: r for r in rows}
        mayorista_v = float(by_casa["mayorista"]["venta"])
        oficial_v = float(by_casa["oficial"]["venta"])
        blue_v = float(by_casa["blue"]["venta"])
        # Loose ranges that catch decimal-place or unit errors, not market drift.
        for label, v in (("mayorista", mayorista_v), ("oficial", oficial_v), ("blue", blue_v)):
            assert 100 < v < 10_000, f"{label} venta={v} outside [100, 10000]"
        # Blue >= oficial almost always (would be a structural anomaly otherwise).
        if blue_v < oficial_v * 0.95:
            return f"WARN: blue ({blue_v}) << oficial ({oficial_v}) — unusual"
        return f"mayorista={mayorista_v:.2f} oficial={oficial_v:.2f} blue={blue_v:.2f}"
    _try("dolarapi", "value sanity", sanity)

    def freshness():
        rows = payload["data"] or []
        ts = rows[0].get("fechaActualizacion", "")
        # ISO format with timezone
        try:
            t = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            age = datetime.now(t.tzinfo) - t
            assert age < timedelta(hours=24), f"FX quote {age} old"
            return f"freshest quote {age.total_seconds()/60:.0f}min old"
        except ValueError:
            return f"WARN: could not parse fechaActualizacion={ts!r}"
    _try("dolarapi", "freshness < 24h", freshness, critical=False)

    def cache():
        prov = DolarAPIProvider()
        prov.get_all()  # prime
        t1 = time.monotonic()
        for _ in range(8):
            prov.get_all()
        elapsed = (time.monotonic() - t1) * 1000.0
        assert elapsed < 50, f"8 cached calls took {elapsed:.0f}ms"
        return f"8 cached calls in {elapsed:.0f}ms"
    _try("dolarapi", "cache TTL coalesces", cache)


# ============================================================================
# 3. BCRA — CER + TAMAR
# ============================================================================
def test_bcra() -> None:
    _section("3. BCRA — CER (var 30) + TAMAR (var 44)")
    from core.infrastructure.indices_provider import (
        _fetch_series, _CER_CSV, _TAMAR_CSV, BCRAIndicesProvider,
    )

    series = {}

    def fetch(name: str, var_id: int, days: int):
        def go():
            t = time.monotonic()
            data = _fetch_series(var_id, days=days)
            dt = (time.monotonic() - t) * 1000.0
            assert data, f"empty response (BCRA may be down or rate-limited)"
            series[name] = data
            return f"{len(data)} points in {dt:.0f}ms"
        return go

    _try("bcra", "GET CER (30d)", fetch("cer", 30, 30))
    _try("bcra", "GET TAMAR (30d topup)", fetch("tamar", 44, 30))

    def cer_sanity():
        cer = series.get("cer", {})
        assert cer, "no CER data"
        # CER is an accumulated index: must be monotonically non-decreasing.
        dates = sorted(cer.keys())
        last = max(dates)
        # Today might not be published yet (weekend / pre-publication).
        age = (date.today() - last).days
        assert age <= 7, f"latest CER point is {age} days old (date={last})"
        for i in range(1, len(dates)):
            prev, cur = cer[dates[i-1]], cer[dates[i]]
            assert cur >= prev * 0.999, f"CER decreased {prev} → {cur} on {dates[i]}"
        return f"latest={cer[last]:.4f} on {last} (age {age}d), monotonic OK"
    _try("bcra", "CER monotonic + fresh", cer_sanity)

    def tamar_sanity():
        tamar = series.get("tamar", {})
        assert tamar, "no TAMAR data"
        for d, v in tamar.items():
            assert 5 < v < 200, f"TAMAR {v}% on {d} outside [5, 200]%"
        last = max(tamar.keys())
        age = (date.today() - last).days
        assert age <= 7, f"latest TAMAR is {age} days old"
        return f"latest={tamar[last]:.2f}% on {last} (age {age}d)"
    _try("bcra", "TAMAR range + fresh", tamar_sanity)

    # Persistence — atomic write, hydrate, integrity
    def persist():
        from core.infrastructure.indices_provider import _save_csv, _load_csv
        sample = {date(2026, 1, 1): 1234.56, date(2026, 1, 2): 1235.00}
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, "sub", "test.csv")
            _save_csv(p, sample)
            assert os.path.isfile(p), "file not created"
            assert not os.path.exists(p + ".tmp"), ".tmp lingered after success"
            roundtrip = _load_csv(p)
            assert roundtrip == sample, f"roundtrip mismatch: {roundtrip} vs {sample}"
        return "atomic write + roundtrip OK"
    _try("bcra", "disk persistence", persist)

    def disk_present():
        files = []
        for label, path in (("CER", _CER_CSV), ("TAMAR", _TAMAR_CSV)):
            if os.path.isfile(path):
                size = os.path.getsize(path)
                files.append(f"{label}={size}B")
            else:
                files.append(f"{label}=MISSING")
        return " ".join(files)
    _try("bcra", "persistent CSVs on disk", disk_present, critical=False)

    def offline_fallback():
        # Realistic scenario: process restarts with a stale disk snapshot
        # while BCRA is unreachable. The promise is that the project keeps
        # serving with the persisted values, not that it can boot from
        # nothing without ever reaching BCRA.
        import core.infrastructure.indices_provider as mod
        original = mod._fetch_series
        # Step 1: prime the disk with one real fetch.
        BCRAIndicesProvider._cache_cer = {}
        BCRAIndicesProvider._cache_tamar = {}
        BCRAIndicesProvider._disk_loaded = False
        BCRAIndicesProvider._last_attempt = None
        p = BCRAIndicesProvider()
        p.get_cer(date.today())
        p.get_tamar()
        assert os.path.isfile(mod._CER_CSV), "priming failed: CER CSV not on disk"
        assert os.path.isfile(mod._TAMAR_CSV), "priming failed: TAMAR CSV not on disk"
        # Step 2: simulate a fresh process where BCRA returns nothing.
        BCRAIndicesProvider._cache_cer = {}
        BCRAIndicesProvider._cache_tamar = {}
        BCRAIndicesProvider._disk_loaded = False
        BCRAIndicesProvider._last_attempt = None
        try:
            mod._fetch_series = lambda *a, **kw: {}
            p2 = BCRAIndicesProvider()
            cer_today = p2.get_cer(date.today())
            tamar_today = p2.get_tamar()
            assert cer_today is not None, "CER unavailable when BCRA is down (disk failed)"
            assert tamar_today is not None, "TAMAR unavailable when BCRA is down (disk failed)"
            return f"served from disk: CER={cer_today:.2f} TAMAR={tamar_today:.2f}%"
        finally:
            mod._fetch_series = original
            BCRAIndicesProvider._cache_cer = {}
            BCRAIndicesProvider._cache_tamar = {}
            BCRAIndicesProvider._disk_loaded = False
            BCRAIndicesProvider._last_attempt = None
    _try("bcra", "offline fallback (BCRA down)", offline_fallback)

    def concurrent_get():
        # 20 threads hitting the provider concurrently — only 1 HTTP fetch
        # should happen (the rest serialize on the lock and find the cache).
        BCRAIndicesProvider._cache_cer = {}
        BCRAIndicesProvider._cache_tamar = {}
        BCRAIndicesProvider._disk_loaded = False
        BCRAIndicesProvider._last_attempt = None
        import core.infrastructure.indices_provider as mod
        call_count = {"n": 0}
        original = mod._fetch_series
        def counted(var_id, days):
            call_count["n"] += 1
            return original(var_id, days)
        mod._fetch_series = counted
        try:
            errors = []
            def worker(_):
                try:
                    BCRAIndicesProvider().get_cer(date.today())
                except Exception as e:
                    errors.append(e)
            with ThreadPoolExecutor(max_workers=20) as ex:
                list(ex.map(worker, range(20)))
            assert not errors, f"thread errors: {errors[:3]}"
            # 2 series x 1 fetch = 2 calls expected (CER+TAMAR), not 20x2=40
            assert call_count["n"] <= 2, f"{call_count['n']} fetches under contention (expected <=2)"
            return f"20 threads -> {call_count['n']} HTTP fetches (lock works)"
        finally:
            mod._fetch_series = original
    _try("bcra", "concurrency × 20 + lock", concurrent_get)


# ============================================================================
# 4. REM
# ============================================================================
def test_rem() -> None:
    _section("4. REM — Expectativas de Inflación (BCRA via 3rd party)")
    from core.infrastructure.rem_provider import REMProvider, _parse_period

    payload = {"data": None}
    def conn():
        st, body, dt = _http_get(REMProvider.URL_IPC, timeout=15)
        assert st == 200, f"HTTP {st}"
        data = json.loads(body)
        rows = data.get("datos") or []
        assert rows, "empty datos[]"
        payload["data"] = rows
        return f"{len(rows)} rows in {dt*1000:.0f}ms"
    _try("rem", "GET /api/ipc_general", conn, critical=False)

    def schema():
        rows = payload["data"] or []
        if not rows:
            raise AssertionError("no data to check (upstream may be rate-limited)")
        REQUIRED = {"período", "referencia", "mediana"}
        for r in rows[:20]:  # spot-check first 20
            missing = REQUIRED - set(r.keys())
            assert not missing, f"row {r} missing {missing}"
        return f"first 20 rows have {REQUIRED}"
    _try("rem", "schema", schema, critical=False)

    def value_sanity():
        rows = payload["data"] or []
        if not rows:
            raise AssertionError("no data")
        monthly = [r for r in rows if "mensual" in str(r.get("referencia", "")).lower()]
        assert monthly, "no monthly inflation rows"
        for r in monthly:
            med = r.get("mediana")
            if med is None:
                continue
            assert 0 <= float(med) < 20, f"monthly inflation {med}% absurd"
        return f"{len(monthly)} monthly rows, all medians in [0, 20)%"
    _try("rem", "value sanity (monthly < 20%)", value_sanity, critical=False)

    def parse_periods():
        # Unit test for the period parser
        cases = [
            ("2026-04-30 00:00:00", date(2026, 4, 30)),
            ("2026-04-30", date(2026, 4, 30)),
            ("próx. 12 meses", None),
            (2026, date(2026, 12, 31)),
        ]
        for inp, expected in cases:
            got = _parse_period(inp)
            assert got == expected, f"_parse_period({inp!r}) = {got}, want {expected}"
        return f"{len(cases)} cases OK"
    _try("rem", "_parse_period unit test", parse_periods)


# ============================================================================
# 5. ROFEX / Matba — Futuros DLR via WebSocket público (sin auth)
# ============================================================================
def test_rofex() -> None:
    _section("5. Matba/Primary WS — Futuros DLR (público, sin auth)")
    from core.infrastructure.futures_provider import RofexProvider, SPOT_SYMBOL, DEFAULT_SYMBOLS
    def fetch():
        prov = RofexProvider()
        t = time.monotonic()
        quotes = prov.get_quotes(DEFAULT_SYMBOLS + [SPOT_SYMBOL])
        dt = (time.monotonic() - t) * 1000.0
        assert quotes, "empty quotes (WS conexión inicial?)"
        with_settle = sum(1 for q in quotes.values() if q.get("settle"))
        spot = (quotes.get(SPOT_SYMBOL) or {}).get("last")
        assert spot, "spot DLR/A3500 no llegó"
        return (f"{len(quotes)}/{len(DEFAULT_SYMBOLS)+1} symbols, "
                f"{with_settle} con settle, spot={spot:.2f}, {dt:.0f}ms")
    _try("rofex", "ws_get_quotes", fetch, critical=False)


# ============================================================================
# 6. END-TO-END refresh cycle timing
# ============================================================================
def test_e2e_cycle() -> None:
    _section("6. END-TO-END — un ciclo completo del server")
    from core.infrastructure.repositories import (
        Data912MarketDataProvider, ExcelInstrumentsRepository,
    )
    from core.infrastructure.fx_provider import DolarAPIProvider
    from core.infrastructure.indices_provider import BCRAIndicesProvider
    from core.domain.instrument_groups import (
        BOPREALES, CER, DOLAR_LINKED, DUAL_TAMAR, SOBERANOS, TAMAR, TASA_FIJA,
    )
    from core.use_cases.generate_report import GenerateMonitorReport
    from config.settings import MASTER_XLSX
    from apps.web.server import REFRESH_SEC

    def go():
        # Mirror what _refresh_loop actually does in production: invalidate
        # caches at cycle start, then a single batched execute() over every
        # bond type. Init costs (Excel parse, TAMAR 3y bootstrap, first
        # network round) are paid once and don't repeat per cycle, so we
        # warm up once and measure the steady-state cycle.
        repo = ExcelInstrumentsRepository(MASTER_XLSX)
        prov = Data912MarketDataProvider()
        use_case = GenerateMonitorReport(repo, prov)
        fx = DolarAPIProvider()
        bcra = BCRAIndicesProvider()

        ALL = list(dict.fromkeys(
            list(SOBERANOS) + list(CER) + list(BOPREALES) + list(TASA_FIJA)
            + list(DOLAR_LINKED) + list(TAMAR) + list(DUAL_TAMAR)
        ))

        # Warm-up
        fx.get_all()
        bcra.get_tamar()
        use_case.execute(ALL)

        t = time.monotonic()
        prov.invalidate_cache()
        fx.invalidate_cache()
        fx.get_all()
        bcra.get_tamar()
        rows_total = len(use_case.execute(ALL))
        elapsed = time.monotonic() - t

        # Adaptive sleep means cycle MUST fit in REFRESH_SEC, otherwise the
        # loop runs back-to-back with no sleep and freshness silently degrades.
        assert elapsed < REFRESH_SEC, (
            f"steady-state cycle {elapsed:.2f}s exceeds REFRESH_SEC={REFRESH_SEC}s — "
            f"either bump REFRESH_SEC or optimize the cycle further"
        )
        return f"{rows_total} bond metrics in {elapsed*1000:.0f}ms (REFRESH_SEC={REFRESH_SEC}s)"
    _try("e2e", "full cycle within REFRESH_SEC budget", go)


# ============================================================================
# MAIN
# ============================================================================
def _summary() -> int:
    elapsed = time.monotonic() - _t0
    by_status = {"PASS": 0, "WARN": 0, "FAIL": 0}
    for _, _, status, _ in _results:
        by_status[status] = by_status.get(status, 0) + 1

    print("\n" + "=" * 72)
    print(f"SUMMARY  ·  {len(_results)} checks  ·  {elapsed:.1f}s")
    print("=" * 72)
    print(f"  PASS: {by_status['PASS']}")
    print(f"  WARN: {by_status['WARN']}")
    print(f"  FAIL: {by_status['FAIL']}")
    if by_status["FAIL"]:
        print("\nFAILURES:")
        for group, check, status, detail in _results:
            if status == FAIL:
                print(f"  [{group}] {check}: {detail}")
    return 1 if by_status["FAIL"] else 0


def main() -> int:
    print("=" * 72)
    print("DATA SOURCE QUALITY & RESILIENCE SUITE")
    print(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 72)
    for fn in (test_data912, test_dolarapi, test_bcra, test_rem, test_rofex, test_e2e_cycle):
        try:
            fn()
        except Exception:
            traceback.print_exc()
            _record(fn.__name__, "suite setup", FAIL, "unhandled exception (see traceback)")
    return _summary()


if __name__ == "__main__":
    sys.exit(main())
