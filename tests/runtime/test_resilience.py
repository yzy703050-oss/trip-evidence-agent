import pytest
from support.native import FakeClock


def test_window_half_open_and_late_results():
    from trip_assistant.runtime.resilience import CircuitOpenError, CircuitRegistry
    from trip_assistant.settings import Settings

    clock = FakeClock()
    r = CircuitRegistry(Settings(_env_file=None), clock=clock)
    old = r.acquire("a")
    for _ in range(5):
        r.failure("a", r.acquire("a"))
    with pytest.raises(CircuitOpenError):
        r.acquire("a")
    r.success("a", old)
    with pytest.raises(CircuitOpenError):
        r.acquire("a")
    clock.advance(60)
    lease = r.acquire("a")
    with pytest.raises(CircuitOpenError):
        r.acquire("a")
    r.success("a", lease)
    r.success("a", r.acquire("a"))
    assert r.status("a") == "closed"
    for _ in range(4):
        r.failure("b", r.acquire("b"))
    clock.advance(61)
    r.failure("b", r.acquire("b"))
    assert r.status("b") == "closed"


@pytest.mark.asyncio
async def test_retry_only_twice_and_empty_results_not_retried():
    from trip_assistant.runtime.limits import TurnBudget
    from trip_assistant.runtime.resilience import call_with_retry
    from trip_assistant.runtime.scheduler import Scheduler
    from trip_assistant.settings import Settings

    clock = FakeClock()
    s = Settings(_env_file=None)
    scheduler = Scheduler(s, clock=clock)
    budget = TurnBudget(s, clock=clock)
    count = 0

    async def fail():
        nonlocal count
        count += 1
        raise ConnectionError("temporary")

    with pytest.raises(ConnectionError):
        await call_with_retry(
            fail,
            endpoint="a",
            budget=budget,
            scheduler=scheduler,
            clock=clock,
            rng=lambda low, high: high,
        )
    assert count == 3 and clock.sleeps == [1, 2]

    async def empty():
        return {"items": []}

    assert await call_with_retry(
        empty, endpoint="b", budget=budget, scheduler=scheduler, clock=clock
    ) == {"items": []}

    async def invalid():
        raise ValueError("bad params")

    with pytest.raises(ValueError):
        await call_with_retry(
            invalid, endpoint="c", budget=budget, scheduler=scheduler, clock=clock
        )
    assert scheduler.breakers.status("c") == "closed"


def test_half_open_failure_reopens_and_releases_probe():
    from trip_assistant.runtime.resilience import CircuitOpenError, CircuitRegistry
    from trip_assistant.settings import Settings

    clock = FakeClock()
    r = CircuitRegistry(Settings(_env_file=None), clock=clock)
    for _ in range(5):
        r.failure("a", r.acquire("a"))
    clock.advance(60)
    r.failure("a", r.acquire("a"))
    with pytest.raises(CircuitOpenError):
        r.acquire("a")
    clock.advance(60)
    probe = r.acquire("a")
    r.abandon("a", probe)
    assert r.acquire("a")


@pytest.mark.asyncio
async def test_retry_after_header_is_honored_within_shared_deadline():
    import httpx

    from trip_assistant.runtime.limits import TurnBudget
    from trip_assistant.runtime.resilience import call_with_retry
    from trip_assistant.runtime.scheduler import Scheduler
    from trip_assistant.settings import Settings

    clock = FakeClock()
    settings = Settings(_env_file=None)
    scheduler = Scheduler(settings, clock=clock)
    request = httpx.Request("GET", "https://example.com")
    response = httpx.Response(429, headers={"Retry-After": "7"}, request=request)
    attempts = 0

    async def fetch():
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise httpx.HTTPStatusError("rate limited", request=request, response=response)
        return "ok"

    assert (
        await call_with_retry(
            fetch,
            endpoint="provider:search",
            budget=TurnBudget(settings, clock=clock),
            scheduler=scheduler,
            clock=clock,
            rng=lambda *_: 0,
        )
        == "ok"
    )
    assert clock.sleeps == [7] and attempts == 2


@pytest.mark.asyncio
async def test_same_supplier_interfaces_share_concurrency():
    import asyncio

    from trip_assistant.runtime.limits import TurnBudget
    from trip_assistant.runtime.resilience import call_with_retry
    from trip_assistant.runtime.scheduler import Scheduler
    from trip_assistant.settings import Settings

    settings = Settings(_env_file=None)
    scheduler = Scheduler(settings)
    budget = TurnBudget(settings)
    active = peak = 0

    async def fetch():
        nonlocal active, peak
        active += 1
        peak = max(active, peak)
        await asyncio.sleep(0.01)
        active -= 1
        return "ok"

    await asyncio.gather(
        *(
            call_with_retry(fetch, endpoint=endpoint, budget=budget, scheduler=scheduler)
            for endpoint in ["public:weather", "public:weather", "public:web"]
        )
    )
    assert peak == 2
