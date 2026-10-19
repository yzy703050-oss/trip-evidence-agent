"""Per-call model telemetry without making model responses stateful."""

from __future__ import annotations

import logging
from collections.abc import Callable
from contextlib import contextmanager
from contextvars import ContextVar
from time import perf_counter
from typing import Any

from trip_assistant.runtime.logging import log_event

logger = logging.getLogger(__name__)
_stage: ContextVar[str] = ContextVar("v0_model_stage", default="unspecified")
_scope: ContextVar[dict | None] = ContextVar("workflow_model_scope", default=None)


def current_model_context():
    return {"stage": _stage.get(), **(_scope.get() or {})}


@contextmanager
def model_scope(**ids):
    token = _scope.set({**(_scope.get() or {}), **ids})
    try:
        yield
    finally:
        _scope.reset(token)


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
        self,
        started: float,
        stage: str,
        usage: Any,
        status: str,
        error: str | None = None,
        ttft_ms: float | None = None,
        diagnostics: dict | None = None,
    ) -> None:
        input_tokens = _usage_value(usage, "input_tokens")
        output_tokens = _usage_value(usage, "output_tokens")
        cached_input_tokens = _usage_value(usage, "cached_input_tokens")
        uncached_input_tokens = _usage_value(usage, "uncached_input_tokens")
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
        log_event(
            logger,
            "model_call",
            stage=stage,
            model=getattr(self.model, "model_name", type(self.model).__name__),
            status=status,
            api_input_tokens=input_tokens,
            api_output_tokens=output_tokens,
            api_cached_input_tokens=cached_input_tokens,
            api_uncached_input_tokens=uncached_input_tokens,
            latency_ms=round((perf_counter() - started) * 1000, 3),
            turn_id=(_scope.get() or {}).get("turn_id"),
            task_id=(_scope.get() or {}).get("task_id"),
        )
        try:
            self.record(
                {
                    "type": "model_call",
                    "stage": stage,
                    "model": getattr(self.model, "model_name", type(self.model).__name__),
                    "status": status,
                    "error": error,
                    "latency_ms": round((perf_counter() - started) * 1000, 3),
                    "ttft_ms": ttft_ms,
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                    "reasoning_tokens": _usage_value(usage, "reasoning_tokens"),
                    "output_diagnostics": diagnostics,
                    "cached_input_tokens": cached_input_tokens,
                    "uncached_input_tokens": uncached_input_tokens,
                    "estimated_cost_usd": cost,
                    **(_scope.get() or {}),
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
            self._write(
                started,
                stage,
                getattr(exc, "usage", None),
                "error",
                str(exc),
                diagnostics=getattr(exc, "diagnostics", None),
            )
            raise
        if hasattr(response, "__aiter__"):

            async def measured_stream():
                usage = None
                first_chunk_ms = None
                status = "success"
                error = None
                completed = False
                try:
                    async for chunk in response:
                        if first_chunk_ms is None:
                            first_chunk_ms = round((perf_counter() - started) * 1000, 3)
                        usage = getattr(chunk, "usage", None) or usage
                        yield chunk
                    completed = True
                except Exception as exc:
                    status = "error"
                    error = str(exc)
                    raise
                finally:
                    if not completed and status == "success":
                        status, error = "cancelled", "stream not fully consumed"
                    self._write(started, stage, usage, status, error, first_chunk_ms)

            return measured_stream()
        self._write(
            started,
            stage,
            getattr(response, "usage", None),
            "success",
            ttft_ms=getattr(response, "ttft_ms", None),
            diagnostics=getattr(response, "diagnostics", None),
        )
        return response
