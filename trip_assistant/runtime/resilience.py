"""Endpoint-scoped sliding-window breakers and exactly one retry owner."""

import asyncio
import random
from collections import deque
from time import monotonic


class CircuitOpenError(RuntimeError):
    pass


class CircuitRegistry:
    def __init__(self, settings, clock=monotonic):
        self.settings = settings
        self.clock = clock
        self.entries = {}

    def _entry(self, key):
        r = self.entries.setdefault(
            key,
            {
                "state": "closed",
                "failures": deque(),
                "opened": None,
                "busy": False,
                "successes": 0,
                "generation": 0,
            },
        )
        now = self.clock()
        while r["failures"] and now - r["failures"][0] >= self.settings.failure_window:
            r["failures"].popleft()
        if r["state"] == "open" and now - r["opened"] >= self.settings.cooldown:
            r.update(state="half_open", busy=False, successes=0)
        return r

    def status(self, key):
        return self._entry(key)["state"]

    def acquire(self, key):
        r = self._entry(key)
        if r["state"] == "open" or r["state"] == "half_open" and r["busy"]:
            raise CircuitOpenError("circuit_open:" + key)
        if r["state"] == "half_open":
            r["busy"] = True
        return (r["state"], r["generation"])

    def success(self, key, lease):
        r = self._entry(key)
        if lease == ("half_open", r["generation"]) and r["state"] == "half_open":
            r["busy"] = False
            r["successes"] += 1
            if r["successes"] >= self.settings.recovery_successes:
                r.update(state="closed", successes=0)
                r["failures"].clear()

    def abandon(self, key, lease):
        r = self._entry(key)
        if lease == ("half_open", r["generation"]):
            r["busy"] = False

    def failure(self, key, lease):
        r = self._entry(key)
        if lease[1] != r["generation"]:
            return
        if r["state"] == "half_open" or r["state"] == "closed":
            r["failures"].append(self.clock())
            if r["state"] == "half_open" or len(r["failures"]) >= self.settings.failure_threshold:
                r.update(
                    state="open",
                    opened=self.clock(),
                    busy=False,
                    successes=0,
                    generation=r["generation"] + 1,
                )


def retryable(error):
    from httpx import TransportError
    from openai import APIConnectionError

    if hasattr(error, "retryable"):
        return error.retryable
    status = getattr(getattr(error, "response", None), "status_code", None) or getattr(
        error, "status_code", None
    )
    return isinstance(
        error, ConnectionError | TimeoutError | APIConnectionError | TransportError
    ) or status in {429, 500, 502, 503, 504}


def retry_after_seconds(error):
    from datetime import UTC, datetime
    from email.utils import parsedate_to_datetime
    from math import isfinite

    value = getattr(error, "retry_after", None)
    if value is None:
        headers = getattr(getattr(error, "response", None), "headers", {})
        value = headers.get("Retry-After")
    if value is None:
        return None
    try:
        seconds = float(value)
    except (ValueError, TypeError):
        try:
            stamp = parsedate_to_datetime(value)
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=UTC)
            seconds = (stamp - datetime.now(UTC)).total_seconds()
        except (ValueError, TypeError, OverflowError):
            return None
    return max(0, seconds) if isfinite(seconds) else None


async def call_with_retry(fetch, *, endpoint, budget, scheduler, clock=None, rng=random.uniform):
    for attempt in range(scheduler.settings.extra_retries + 1):
        lease = None

        async def invoke(attempt=attempt):
            nonlocal lease
            lease = scheduler.breakers.acquire(endpoint)
            record = getattr(scheduler, "record", None)
            if record:
                from trip_assistant.runtime.telemetry import current_model_context

                record(
                    {
                        "type": "external_attempt",
                        "endpoint": endpoint,
                        "attempt": attempt + 1,
                        **current_model_context(),
                    }
                )
            return await fetch()

        try:
            value = await scheduler.run(endpoint.partition(":")[0], endpoint, invoke, budget)
        except BaseException as error:
            can_retry = retryable(error)
            if lease is not None:
                (scheduler.breakers.failure if can_retry else scheduler.breakers.abandon)(
                    endpoint, lease
                )
            if not can_retry or attempt == scheduler.settings.extra_retries:
                raise
            if scheduler.breakers.status(endpoint) == "open":
                raise CircuitOpenError("circuit_open:" + endpoint) from error
            delay = rng(0, min(30, 2**attempt))
            retry_after = retry_after_seconds(error)
            if isinstance(retry_after, int | float) and retry_after >= 0:
                delay = max(delay, retry_after)
            if delay >= budget.remaining_seconds():
                budget.require_time()
                raise TimeoutError("retry_exceeds_deadline") from error
            await (
                clock.sleep(delay)
                if clock is not None and hasattr(clock, "sleep")
                else asyncio.sleep(delay)
            )
        else:
            scheduler.breakers.success(endpoint, lease)
            return value
