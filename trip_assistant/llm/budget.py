"""Offline input accounting; a model-compatible counter may be injected."""

import json
import logging
from collections.abc import Callable
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass


@dataclass(frozen=True)
class TokenEstimate:
    tokens: int
    method: str
    estimated: bool


def count_input_tokens(
    messages: list[dict], tools: list[dict] | None = None, *, counter: Callable | None = None
) -> TokenEstimate:
    payload = {"messages": messages, "tools": tools or []}
    if counter is not None:
        value = counter(payload)
        if isinstance(value, TokenEstimate):
            return value
        return TokenEstimate(int(value), getattr(counter, "__name__", "injected"), False)
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str).encode(
        "utf-8"
    )
    return TokenEstimate(max(1, (len(encoded) + 2) // 3), "utf8_conservative", True)


_agent_scope = ContextVar("context_budget_scope", default="child")
CONTEXT_BUDGET_MESSAGE = (
    "本次旅程内容超过上下文预算，已停止本轮处理。请将旅程分段规划，例如"
    "先安排第一段，再处理后续路段。"
)


@contextmanager
def budget_scope(name: str):
    token = _agent_scope.set(name)
    try:
        yield
    finally:
        _agent_scope.reset(token)


class ContextBudgetExceeded(Exception):
    def __init__(self, *, budget_tokens, estimate, phase="input", response=None):
        super().__init__(CONTEXT_BUDGET_MESSAGE)
        self.agent_scope = _agent_scope.get()
        self.budget_tokens = budget_tokens
        self.estimate = estimate
        self.phase = phase
        self.response = response


def response_message(response):
    if isinstance(response, dict):
        content = response.get("content", response.get("text", ""))
        calls = response.get("tool_calls")
    else:
        content = getattr(
            response,
            "content",
            getattr(response, "text", response if isinstance(response, str) else ""),
        )
        calls = getattr(response, "tool_calls", None)
    result = {"role": "assistant", "content": content}
    if calls:
        result["tool_calls"] = calls
    return result


class BudgetedModel:
    """Count each request independently and only the final streaming snapshot."""

    def __init__(self, model, *, budget_tokens: int, counter=None, record=None):
        if type(budget_tokens) is not int or budget_tokens < 1:
            raise ValueError("invalid context budget")
        self.model, self.budget_tokens, self.counter, self.record = (
            model,
            budget_tokens,
            counter,
            record,
        )
        self.compaction_trigger_tokens = max(1, budget_tokens * 80 // 100)

    def __getattr__(self, name):
        return getattr(self.model, name)

    def check(self, messages, tools=None, *, phase="input", response=None):
        estimate = count_input_tokens(messages, tools, counter=self.counter)
        from trip_assistant.runtime.logging import log_event
        from trip_assistant.runtime.telemetry import current_model_context

        log_event(
            logging.getLogger(__name__),
            "context_budget",
            phase=phase,
            agent_scope=_agent_scope.get(),
            tokens=estimate.tokens,
            method=estimate.method,
            estimated=estimate.estimated,
            budget_tokens=self.budget_tokens,
            trigger_tokens=self.compaction_trigger_tokens,
            **current_model_context(),
        )
        if self.record:
            from trip_assistant.runtime.telemetry import current_model_context

            entry = {
                "type": "context_budget",
                "agent_scope": _agent_scope.get(),
                "phase": phase,
                "tokens": estimate.tokens,
                "method": estimate.method,
                "estimated": estimate.estimated,
                "budget_tokens": self.budget_tokens,
                **current_model_context(),
            }
            if response is not None:
                entry["response_tokens"] = count_input_tokens(
                    [response_message(response)], counter=self.counter
                ).tokens
            try:
                self.record(entry)
            except Exception:
                logging.getLogger(__name__).exception("Could not persist context budget telemetry")
        if estimate.tokens > self.budget_tokens:
            raise ContextBudgetExceeded(
                budget_tokens=self.budget_tokens, estimate=estimate, phase=phase, response=response
            )
        return estimate

    async def __call__(self, messages, **kwargs):
        tools = kwargs.get("tools")
        self.check(messages, tools)
        response = await self.model(messages, **kwargs)
        if hasattr(response, "__aiter__"):

            async def checked_stream():
                latest = None
                async for chunk in response:
                    latest = chunk
                    yield chunk
                if latest is not None:
                    self.check(
                        [*messages, response_message(latest)],
                        tools,
                        phase="output",
                        response=latest,
                    )

            return checked_stream()
        self.check(
            [*messages, response_message(response)], tools, phase="output", response=response
        )
        return response
