"""Offline input accounting; a model-compatible counter may be injected."""
from dataclasses import dataclass
import json
from typing import Callable


@dataclass(frozen=True)
class TokenEstimate:
    tokens: int
    method: str
    estimated: bool


def count_input_tokens(messages: list[dict], tools: list[dict] | None = None,
                       *, counter: Callable | None = None) -> TokenEstimate:
    payload = {"messages": messages, "tools": tools or []}
    if counter is not None:
        value = counter(payload)
        if isinstance(value, TokenEstimate):
            return value
        return TokenEstimate(int(value), getattr(counter, "__name__", "injected"), False)
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8")
    return TokenEstimate(max(1, (len(encoded) + 1) // 2), "utf8_conservative", True)
