"""Per-call model telemetry without making model responses stateful."""

from __future__ import annotations

import logging
from contextlib import contextmanager
from contextvars import ContextVar
from time import perf_counter
from typing import Any, Callable


logger = logging.getLogger(__name__)
_stage: ContextVar[str] = ContextVar("v0_model_stage", default="unspecified")


@contextmanager
def model_stage(name: str):
    token = _stage.set(name)
    try:
        yield
    finally:
        _stage.reset(token)


def _usage_value(usage: Any, name: str) -> int | None:
    if usage is None:
        return None
    value = usage.get(name) if isinstance(usage, dict) else getattr(usage, name, None)
    return int(value) if value is not None else None


class MeteredModel:
    """Delegate model calls while recording measured usage and latency."""

    def __init__(
        self,
        model: Any,
        record: Callable[[dict[str, Any]], Any],
        *,
        input_usd_per_million: float | None = None,
        output_usd_per_million: float | None = None,
    ):
        self.model = model
        self.record = record
        self.input_usd_per_million = input_usd_per_million
        self.output_usd_per_million = output_usd_per_million

    def __getattr__(self, name: str) -> Any:
        return getattr(self.model, name)

    def _write(
        self, started: float, stage: str, usage: Any, status: str, error: str | None = None
    ) -> None:
        input_tokens = _usage_value(usage, "input_tokens")
        output_tokens = _usage_value(usage, "output_tokens")
        cost = None
        if (
            input_tokens is not None
            and output_tokens is not None
            and self.input_usd_per_million is not None
            and self.output_usd_per_million is not None
        ):
            cost = (
                input_tokens * self.input_usd_per_million
                + output_tokens * self.output_usd_per_million
            ) / 1_000_000
        try:
            self.record(
                {
                    "type": "model_call",
                    "stage": stage,
                    "model": getattr(self.model, "model_name", type(self.model).__name__),
                    "status": status,
                    "error": error,
                    "latency_ms": round((perf_counter() - started) * 1000, 3),
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                    "estimated_cost_usd": cost,
                }
            )
        except Exception:
            logger.exception("Could not persist model telemetry")

    async def __call__(self, *args: Any, **kwargs: Any) -> Any:
        started = perf_counter()
        stage = _stage.get()
        try:
            response = await self.model(*args, **kwargs)
        except Exception as exc:
            self._write(started, stage, None, "error", str(exc))
            raise
        if hasattr(response, "__aiter__"):

            async def measured_stream():
                usage = None
                status = "success"
                error = None
                try:
                    async for chunk in response:
                        usage = getattr(chunk, "usage", None) or usage
                        yield chunk
                except Exception as exc:
                    status = "error"
                    error = str(exc)
                    raise
                finally:
                    self._write(started, stage, usage, status, error)

            return measured_stream()
        self._write(started, stage, getattr(response, "usage", None), "success")
        return response
