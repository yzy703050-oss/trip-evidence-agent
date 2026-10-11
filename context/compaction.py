"""Choose deterministic, tool-safe boundaries for V0 context compaction."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any
from agents.context_projection import project_session_message
from context.context_budget import count_input_tokens


def estimate_tokens(event: dict[str, Any]) -> int:
    """Conservative fallback when the configured model exposes no tokenizer."""
    payload = {
        key: event.get(key) for key in ("content", "tool_calls", "name", "status") if key in event
    }
    return count_input_tokens([payload]).tokens


def complete_prefix_boundaries(events: list[dict[str, Any]]) -> list[int]:
    """An unfinished old turn blocks every subsequent prefix boundary."""
    visible = [view for event in events if (view := project_session_message(event)) is not None]
    groups: list[list[dict]] = []
    for event in visible:
        if not groups or groups[-1][-1].get("turn_id") != event.get("turn_id"):
            groups.append([])
        groups[-1].append(event)
    cuts = []
    for group in groups[:-1]:
        last = group[-1]
        if not (last.get("role") == "assistant" and last.get("final")):
            break
        cuts.append(last["seq"])
    return cuts


def select_safe_prefix(
    events: list[dict[str, Any]],
    token_budget: int,
    *,
    token_counter: Callable[[dict[str, Any]], int] = estimate_tokens,
) -> int | None:
    """Return the last seq to summarize, or None when no safe cut exists."""
    events = [view for event in events if (view := project_session_message(event)) is not None]
    if not events or sum(token_counter(event) for event in events) <= token_budget:
        return None
    candidates = complete_prefix_boundaries(events)
    if not candidates:
        return None
    for seq in candidates:
        suffix = [event for event in events if event["seq"] > seq]
        if suffix and sum(token_counter(event) for event in suffix) <= token_budget:
            return seq
    return candidates[-1]
