"""In-memory projection of complete conversation turns from session events."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any


class ShortTermMemory:
    """Keep recent turns without assuming each turn has exactly two events."""

    def __init__(self, max_turns: int = 10):
        self.max_turns = max_turns
        self.messages: list[dict[str, Any]] = []

    def add_event(self, event: dict[str, Any]) -> None:
        self.messages.append(dict(event))
        turns = list(dict.fromkeys(item.get("turn_id") for item in self.messages if item.get("turn_id")))
        if len(turns) > self.max_turns:
            keep = set(turns[-self.max_turns:])
            self.messages = [item for item in self.messages if item.get("turn_id") in keep]

    def add_message(self, role: str, content: str, metadata: dict | None = None) -> None:
        """Compatibility entry point for older V0 code and manual scripts."""
        latest_user = next((item for item in reversed(self.messages) if item.get("role") == "user" and item.get("scope") == "session"), None)
        turn_id = latest_user["turn_id"] if role == "assistant" and latest_user else uuid.uuid4().hex
        self.add_event({
            "turn_id": turn_id, "scope": "session", "type": "message", "role": role,
            "content": content, "timestamp": datetime.now(timezone.utc).isoformat(),
            "metadata": metadata or {}, "final": role == "assistant",
        })

    def get_events(self, n_turns: int | None = None) -> list[dict[str, Any]]:
        if n_turns is None:
            return [dict(item) for item in self.messages]
        if n_turns <= 0:
            return []
        turns = list(dict.fromkeys(item.get("turn_id") for item in self.messages if item.get("turn_id")))
        keep = set(turns[-n_turns:])
        return [dict(item) for item in self.messages if item.get("turn_id") in keep]

    def get_recent_context(self, n_turns: int | None = None) -> list[dict[str, Any]]:
        """Only user-facing messages are suitable for general Agent context."""
        return [
            item for item in self.get_events(n_turns)
            if item.get("scope") == "session"
            and item.get("type") == "message"
            and item.get("role") in {"user", "assistant"}
        ]

    def get_context_string(self, n_turns: int = 5) -> str:
        messages = self.get_recent_context(n_turns)
        if not messages:
            return "无历史对话"
        return "\n".join(
            f"{'用户' if item['role'] == 'user' else '助手'}: {item.get('content', '')}"
            for item in messages
        )

    def clear(self) -> None:
        self.messages = []

    def get_statistics(self) -> dict[str, Any]:
        return {
            "total_messages": len(self.messages),
            "max_turns": self.max_turns,
            "oldest_message_time": self.messages[0].get("timestamp") if self.messages else None,
            "newest_message_time": self.messages[-1].get("timestamp") if self.messages else None,
        }
