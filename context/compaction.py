"""Choose deterministic, tool-safe boundaries for V0 context compaction."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any


def estimate_tokens(event: dict[str, Any]) -> int:
    """Conservative fallback when the configured model exposes no tokenizer."""
    payload = {
        key: event.get(key) for key in ("content", "tool_calls", "name", "status") if key in event
    }
    return max(1, len(json.dumps(payload, ensure_ascii=False)) // 2)


def select_safe_prefix(
    events: list[dict[str, Any]],
    token_budget: int,
    *,
    token_counter: Callable[[dict[str, Any]], int] = estimate_tokens,
    keep_recent_turns: int = 2,
) -> int | None:
    """Return the last seq to summarize, or None when no safe cut exists."""
    if not events or sum(token_counter(event) for event in events) <= token_budget:
        return None
    turns = list(dict.fromkeys(event.get("turn_id") for event in events if event.get("turn_id")))
    protected = set(turns[-keep_recent_turns:]) if keep_recent_turns else set()
    pending: set[str] = set()
    candidates: list[int] = []
    for event in events:
        for call in event.get("tool_calls", []):
            pending.add(call["id"])
        if event.get("type") == "tool_result":
            pending.discard(event.get("tool_call_id"))
        if pending:
            continue
        completed_turn = (
            event.get("type") == "message"
            and event.get("scope") == "session"
            and event.get("role") == "assistant"
            and event.get("final")
            and event.get("turn_id") not in protected
        )
        completed_stage = event.get("type") in {"stage_complete", "tool_result"}
        if completed_turn or completed_stage:
            candidates.append(event["seq"])
    if not candidates:
        return None
    for seq in candidates:
        suffix = [event for event in events if event["seq"] > seq]
        if suffix and sum(token_counter(event) for event in suffix) <= token_budget:
            return seq
    return candidates[-1]
