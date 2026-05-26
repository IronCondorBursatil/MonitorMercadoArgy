"""CircuitBreaker (Fase 3): transiciones CLOSED → OPEN → HALF_OPEN."""

import asyncio

import pytest

from core.infrastructure.circuit_breaker import CircuitBreaker, CircuitOpenError


def test_closed_to_open_after_fail_max():
    async def run():
        b = CircuitBreaker(fail_max=3, reset_timeout=10.0, name="t")
        assert b.state == "closed"
        for _ in range(3):
            with pytest.raises(ValueError):
                async with b:
                    raise ValueError("boom")
        assert b.state == "open"
        with pytest.raises(CircuitOpenError):
            async with b:
                pass  # rechazado sin ejecutar el body
    asyncio.run(run())


def test_open_to_halfopen_to_closed_on_success():
    async def run():
        b = CircuitBreaker(fail_max=1, reset_timeout=0.0, name="t")
        with pytest.raises(ValueError):
            async with b:
                raise ValueError("boom")
        assert b.state == "open"
        # reset_timeout=0 → la próxima entrada pasa a HALF_OPEN; éxito CIERRA.
        async with b:
            pass
        assert b.state == "closed"
    asyncio.run(run())


def test_halfopen_reopens_on_failure():
    async def run():
        b = CircuitBreaker(fail_max=1, reset_timeout=0.0, name="t")
        with pytest.raises(ValueError):
            async with b:
                raise ValueError("boom")
        assert b.state == "open"
        with pytest.raises(ValueError):
            async with b:               # HALF_OPEN probe…
                raise ValueError("again")  # …falla → reabre
        assert b.state == "open"
    asyncio.run(run())


def test_success_resets_fail_count():
    async def run():
        b = CircuitBreaker(fail_max=3, reset_timeout=10.0)
        with pytest.raises(ValueError):
            async with b:
                raise ValueError()
        async with b:  # éxito resetea el contador
            pass
        assert b.state == "closed"
        for _ in range(2):  # 2 fallos no alcanzan fail_max=3 tras el reset
            with pytest.raises(ValueError):
                async with b:
                    raise ValueError()
        assert b.state == "closed"
    asyncio.run(run())
