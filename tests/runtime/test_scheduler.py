import asyncio

import pytest


@pytest.mark.asyncio
async def test_shared_and_supplier_peaks():
    from trip_assistant.runtime.limits import TurnBudget
    from trip_assistant.runtime.scheduler import Scheduler
    from trip_assistant.settings import Settings

    s = Settings(_env_file=None)
    scheduler = Scheduler(s)
    budget = TurnBudget(s)
    active = 0
    peak = 0
    vendors = {}
    peaks = {}

    async def fetch(vendor):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        vendors[vendor] = vendors.get(vendor, 0) + 1
        peaks[vendor] = max(peaks.get(vendor, 0), vendors[vendor])
        await asyncio.sleep(0.001)
        active -= 1
        vendors[vendor] -= 1
        return vendor

    results = await asyncio.gather(
        *(
            scheduler.run(v, str(i), lambda v=v: fetch(v), budget)
            for i, v in enumerate(["a"] * 5 + ["b"] * 5)
        )
    )
    assert len(results) == 10 and peak == 3 and max(peaks.values()) == 2


@pytest.mark.asyncio
async def test_deadline_rechecked_after_queue_and_trip_writes_serialized():
    from support.native import FakeClock

    from trip_assistant.runtime.limits import LimitError, TurnBudget
    from trip_assistant.runtime.scheduler import Scheduler
    from trip_assistant.settings import Settings

    clock = FakeClock()
    s = Settings(_env_file=None)
    b = TurnBudget(s, clock=clock)
    scheduler = Scheduler(s)
    count = 0

    async def fetch():
        nonlocal count
        count += 1
        return count

    clock.advance(601)
    with pytest.raises(LimitError):
        await scheduler.run("a", "x", fetch, b)
    assert count == 0
    active = 0
    peak = 0

    async def write():
        nonlocal active, peak
        async with scheduler.write_lock("trip"):
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0)
            active -= 1

    await asyncio.gather(write(), write())
    assert peak == 1
