"""Global/supplier concurrency, deadlines and per-trip write serialization."""

import asyncio
from time import monotonic

from trip_assistant.runtime.resilience import CircuitRegistry


async def gather_owned(*awaitables):
    """A batch must finish or cancel all its work before its owner returns."""
    tasks = [asyncio.ensure_future(value) for value in awaitables]
    try:
        return await asyncio.gather(*tasks)
    except BaseException:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise


class Scheduler:
    def __init__(self, settings, clock=monotonic, *, shared=None):
        self.settings = settings
        self.global_slots = (
            shared.global_slots if shared else asyncio.Semaphore(settings.external_concurrency)
        )
        self.suppliers = shared.suppliers if shared else {}
        self.trip_locks = shared.trip_locks if shared else {}
        self.breakers = shared.breakers if shared else CircuitRegistry(settings, clock=clock)

    async def run(self, provider_key, request_key, fetch, budget):
        budget.require_time()
        slots = self.suppliers.setdefault(
            provider_key, asyncio.Semaphore(self.settings.supplier_concurrency)
        )
        async with asyncio.timeout(budget.remaining_seconds()):
            async with slots, self.global_slots:
                budget.require_time()
                async with asyncio.timeout(
                    min(self.settings.request_timeout, budget.remaining_seconds())
                ):
                    value = await fetch()
                budget.require_time()
                return value

    def write_lock(self, trip_id):
        return self.trip_locks.setdefault(trip_id, asyncio.Lock())
