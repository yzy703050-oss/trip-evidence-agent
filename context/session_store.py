"""Append-only, session-scoped records for the V0 CLI."""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


_locks_guard = threading.Lock()
_path_locks: dict[Path, threading.RLock] = {}


def storage_component(value: str) -> str:
    """Reject path traversal while allowing existing Unicode user IDs."""
    if not isinstance(value, str) or not value or value in {".", ".."}:
        raise ValueError("user_id and session_id must be non-empty path components")
    if any(character in value for character in "/\\:\0<>|?*") or value.endswith((".", " ")):
        raise ValueError("user_id and session_id must be safe path components")
    return value


def atomic_json_write(path: Path, payload: dict[str, Any]) -> None:
    """Replace state files only after a complete JSON document exists."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    """Recover only a truncated final line; reject corruption in complete lines."""
    if not path.exists():
        return []
    raw = path.read_bytes()
    lines = raw.splitlines(keepends=True)
    records = []
    offset = 0
    for index, line in enumerate(lines):
        try:
            if line.strip():
                records.append(json.loads(line))
        except (json.JSONDecodeError, UnicodeDecodeError):
            if index != len(lines) - 1 or line.endswith(b"\n"):
                raise
            backup = path.with_suffix(path.suffix + ".corrupt")
            with backup.open("ab") as handle:
                handle.write(line + b"\n")
                handle.flush()
                os.fsync(handle.fileno())
            with path.open("r+b") as handle:
                handle.truncate(offset)
                handle.flush()
                os.fsync(handle.fileno())
            break
        offset += len(line)
    if records and raw and not raw.endswith(b"\n") and offset == len(raw):
        with path.open("ab") as handle:
            handle.write(b"\n")
            handle.flush()
            os.fsync(handle.fileno())
    return records


class SessionStore:
    """One ordered event log and one compacted state for a user session."""

    def __init__(self, root: str | Path, user_id: str, session_id: str):
        self.user_id = storage_component(user_id)
        self.session_id = storage_component(session_id)
        self.session_dir = Path(root) / self.user_id / "sessions" / self.session_id
        self.events_path = self.session_dir / "events.jsonl"
        self.runs_path = self.session_dir / "runs.jsonl"
        self.state_path = self.session_dir / "state.json"
        self.session_dir.mkdir(parents=True, exist_ok=True)
        lock_key = self.events_path.resolve()
        with _locks_guard:
            self._lock = _path_locks.setdefault(lock_key, threading.RLock())

    def read_events(self) -> list[dict[str, Any]]:
        with self._lock:
            return _read_jsonl(self.events_path)

    def append(self, event: dict[str, Any]) -> dict[str, Any]:
        """Persist one event; repeated event_id values are idempotent."""
        with self._lock:
            existing = self.read_events()
            event_id = event.get("event_id")
            if event_id is not None:
                for item in existing:
                    if item.get("event_id") == event_id:
                        return item
            record = dict(event)
            record["seq"] = existing[-1]["seq"] + 1 if existing else 1
            record.setdefault("timestamp", datetime.now(timezone.utc).isoformat())
            record["session_id"] = self.session_id
            encoded = json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
            self.session_dir.mkdir(parents=True, exist_ok=True)
            with self.events_path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            return record

    def read_state(self) -> dict[str, Any]:
        with self._lock:
            if not self.state_path.exists():
                return {}
            with self.state_path.open("r", encoding="utf-8") as handle:
                return json.load(handle)

    def read_runs(self) -> list[dict[str, Any]]:
        with self._lock:
            return _read_jsonl(self.runs_path)

    def append_run(self, event: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            previous = self.read_runs()
            record = dict(event)
            record["run_seq"] = previous[-1]["run_seq"] + 1 if previous else 1
            record["session_id"] = self.session_id
            record.setdefault("timestamp", datetime.now(timezone.utc).isoformat())
            self.session_dir.mkdir(parents=True, exist_ok=True)
            with self.runs_path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            return record

    def write_state(self, state: dict[str, Any]) -> None:
        with self._lock:
            atomic_json_write(self.state_path, state)
