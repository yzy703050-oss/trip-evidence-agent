"""Read-only legacy parsing and one atomic import per user."""

import hashlib
import json
import logging
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path

from trip_assistant.storage.index import workflow_summary
from trip_assistant.storage.sqlite import encode, ensure_session, metadata, set_metadata


def read_legacy_jsonl(path: Path):
    return _parse_jsonl(path, path.read_bytes())


def _parse_jsonl(path, content):
    rows, diagnostics = [], []
    lines = content.splitlines(keepends=True)
    for number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except (ValueError, UnicodeError) as error:
            if number == len(lines) and not line.endswith((b"\n", b"\r")):
                diagnostics.append(f"truncated tail excluded: {path}:{number}")
                break
            raise ValueError(f"invalid legacy JSONL {path}:{number}") from error
        if not isinstance(row, dict):
            raise ValueError(f"invalid legacy record {path}:{number}")
        rows.append(row)
    return rows, diagnostics


def verify_sources(sources):
    for path, (digest, _) in sources.items():
        if not path.exists() or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise ValueError(f"migration source changed: {path}")


def history_identity(row, index=None):
    if row.get("workflow_id"):
        identity = ["workflow", row["workflow_id"]]
    elif row.get("trip_id"):
        identity = ["trip", row["trip_id"]]
    else:
        identity = ["legacy", index, row]
    return (
        "history_"
        + hashlib.sha256(
            json.dumps(identity, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()
    )


def normalize_preferences(value):
    if isinstance(value, dict):
        value = [{"type": key, "value": item} for key, item in value.items() if item is not None]
    if not isinstance(value, list):
        raise ValueError("invalid preferences")
    rows = []
    for row in value:
        if not isinstance(row, dict) or "type" not in row:
            raise ValueError("invalid preference record")
        if row["type"] == "preferences" and isinstance(row.get("value"), list):
            rows.extend(normalize_preferences(row["value"]))
        else:
            rows.append(row)
    return rows


def insert_event(conn, row):
    """Insert an owned, numbered record and tool pairing in the same transaction."""
    session, seq = row["session_id"], row["seq"]
    ensure_session(conn, session)
    conn.execute(
        "INSERT INTO events VALUES (?,?,?,?,?,?,?,?,?)",
        (
            session,
            seq,
            row.get("event_id"),
            row.get("turn_id"),
            row.get("scope"),
            row.get("type"),
            row.get("role"),
            row.get("timestamp", ""),
            encode(row),
        ),
    )
    for call in row.get("tool_calls", []):
        if not call.get("id") or not call.get("name"):
            raise ValueError("Tool calls require an id and name")
        if conn.execute(
            "SELECT 1 FROM tool_calls WHERE session_id=? AND call_id=?", (session, call["id"])
        ).fetchone():
            raise ValueError("Tool call ids must be unique within a session")
        conn.execute(
            "INSERT INTO tool_calls VALUES (?,?,?,NULL,?,?,?)",
            (session, call["id"], seq, row["turn_id"], row["scope"], encode(call)),
        )
    if row.get("type") == "tool_result":
        pending = conn.execute(
            (
                "SELECT turn_id,scope,call_seq FROM tool_calls WHERE session_id=?"
                " AND call_id=? AND result_seq IS NULL"
            ),
            (session, row.get("tool_call_id")),
        ).fetchone()
        state = json.loads(
            conn.execute("SELECT state_json FROM sessions WHERE id=?", (session,)).fetchone()[0]
        )
        if (
            not pending
            or pending[0] != row.get("turn_id")
            or pending[1] != row.get("scope")
            or pending[2] <= state.get("cleared_through_seq", 0)
        ):
            raise ValueError("Tool result has no matching pending call")
        conn.execute(
            "UPDATE tool_calls SET result_seq=? WHERE session_id=? AND call_id=?",
            (seq, session, row["tool_call_id"]),
        )
    conn.execute("UPDATE sessions SET last_seq=MAX(last_seq,?) WHERE id=?", (seq, session))
    if (
        row.get("type") == "message"
        and row.get("scope") == "session"
        and row.get("role") in {"user", "assistant"}
    ):
        timestamp = row.get("timestamp", "")
        try:
            instant = datetime.fromisoformat(timestamp)
            if instant.tzinfo is None:
                instant = instant.replace(tzinfo=UTC)
            timestamp = instant.astimezone(UTC).isoformat()
        except (ValueError, TypeError):
            timestamp = ""
        conn.execute(
            "UPDATE sessions SET updated_at=MAX(updated_at,?) WHERE id=?", (timestamp, session)
        )
        if row["role"] == "user" and str(row.get("content", "")).strip():
            content = str(row.get("content", ""))
            normalized = " ".join(content.split())
            title = normalized[:36] + ("…" if len(normalized) > 36 else "")
            conn.execute(
                (
                    "UPDATE sessions SET title=CASE WHEN title='' THEN ? ELSE title E"
                    "ND,last_user_message=? WHERE id=?"
                ),
                (title, content, session),
            )


def migrate_user(storage):
    with storage.connection() as conn:
        if metadata(conn, "migration_complete") == "1":
            return
    sources = {}

    def read_json(path):
        content = path.read_bytes()
        try:
            value = json.loads(content)
        except (ValueError, UnicodeError) as error:
            raise ValueError(f"invalid legacy JSON {path}") from error
        if not isinstance(value, dict):
            raise ValueError(f"invalid legacy object {path}")
        sources[path] = (hashlib.sha256(content).hexdigest(), [])
        return value

    def owned_json(path, *, strict):
        if not path.exists():
            return None
        if not strict:
            try:
                record = json.loads(path.read_bytes())
            except (ValueError, UnicodeError):
                return None
            if not isinstance(record, dict) or record.get("user_id") != storage.user_id:
                return None
        record = read_json(path)
        if record.get("user_id") != storage.user_id:
            raise ValueError(f"legacy owner mismatch: {path}")
        return record

    with storage.transaction() as conn:
        if metadata(conn, "migration_complete") == "1":
            return
        directory = storage.directory
        old_directory = storage.root / storage.user_id
        identity_path = directory / "identity.json"
        identity = read_json(identity_path) if identity_path.exists() else {}
        if identity_path.exists() and (
            identity.get("user_id") != storage.user_id or identity.get("version") != 1
        ):
            raise ValueError("user storage identity mismatch")
        legacy = owned_json(storage.root / f"{storage.user_id}.json", strict=False) or {}
        profile = owned_json(directory / "profile.json", strict=True)
        if profile is None:
            profile = owned_json(old_directory / "profile.json", strict=False)
        profile = deepcopy(profile if profile is not None else legacy)
        preferences = normalize_preferences(profile.pop("preferences", []))
        profile.pop("chat_history", None)
        profile.pop("trip_history", None)
        profile.setdefault("user_id", storage.user_id)
        profile.setdefault("created_at", datetime.now(UTC).isoformat())
        profile.setdefault("statistics", {})
        conn.execute("INSERT INTO profiles VALUES (1,?)", (encode(profile),))
        for row in preferences:
            conn.execute(
                (
                    "INSERT INTO preferences VALUES (?,?,?,?) ON CONFLICT(type) DO UP"
                    "DATE SET value_json=excluded.value_json,source=excluded.source,d"
                    "eleted=excluded.deleted"
                ),
                (
                    row["type"],
                    encode(row.get("value")),
                    row.get("source", "agent"),
                    int(bool(row.get("deleted"))),
                ),
            )

        history_state = {}
        state_path = directory / "workflows/.history-state.json"
        if (
            not state_path.exists()
            and owned_json(old_directory / "profile.json", strict=False) is not None
        ):
            state_path = old_directory / "workflows/.history-state.json"
        if state_path.exists():
            history_state = read_json(state_path)
        if history_state.get("cleared_before"):
            set_metadata(conn, "history_cleared_before", history_state["cleared_before"])
        for base, strict in ((directory, True), (old_directory, False)):
            for path in sorted((base / "workflows").glob("*.json")):
                if path.name.startswith("."):
                    continue
                value = owned_json(path, strict=strict)
                if value is None:
                    continue
                if value.get("id") != path.stem or type(value.get("revision")) is not int:
                    raise ValueError(f"invalid workflow identity or revision: {path}")
                if value.get("record_type") == "travel_history":
                    if not isinstance(value.get("history"), dict):
                        raise ValueError(f"invalid travel history: {path}")
                    conn.execute(
                        "INSERT OR IGNORE INTO travel_history VALUES (?,?,?,?)",
                        (
                            value["id"],
                            value["revision"],
                            value.get("updated_at", ""),
                            encode(value["history"]),
                        ),
                    )
                else:
                    if not isinstance(value.get("tasks"), list):
                        raise ValueError(f"invalid workflow snapshot: {path}")
                    conn.execute(
                        "INSERT OR IGNORE INTO trip_plans VALUES (?,?,?,?,?,?,?)",
                        (
                            value["id"],
                            value["revision"],
                            value.get("status"),
                            value.get("created_at", ""),
                            value.get("updated_at", ""),
                            encode(workflow_summary(value)),
                            encode(value),
                        ),
                    )

            for session_dir in sorted((base / "sessions").glob("*")):
                if not session_dir.is_dir():
                    continue
                event_path = session_dir / "events.jsonl"
                if not strict:
                    if (
                        session_dir.name in identity.get("imported_legacy_sessions", [])
                        or conn.execute(
                            "SELECT 1 FROM sessions WHERE id=?", (session_dir.name,)
                        ).fetchone()
                    ):
                        continue
                    try:
                        rows, _ = read_legacy_jsonl(event_path)
                    except (ValueError, OSError):
                        continue
                    if not rows or any(
                        r.get("user_id") != storage.user_id
                        or r.get("session_id") != session_dir.name
                        for r in rows
                    ):
                        continue
                if profile.get("legacy_chat_cleared") and not strict:
                    continue
                session = session_dir.name
                from trip_assistant.storage.sessions import storage_component

                storage_component(session)
                ensure_session(conn, session)
                for name, kind in (("events.jsonl", "events"), ("runs.jsonl", "runs")):
                    path = session_dir / name
                    if not path.exists():
                        continue
                    # Capture digest before parsing and check it again before commit.
                    content = path.read_bytes()
                    rows, diagnostics = _parse_jsonl(path, content)
                    sources[path] = (hashlib.sha256(content).hexdigest(), diagnostics)
                    last = 0
                    for row in rows:
                        if (
                            row.get("user_id") != storage.user_id
                            or row.get("session_id") != session
                        ):
                            raise ValueError(f"legacy session owner mismatch: {path}")
                        seq = row.get("seq" if kind == "events" else "run_seq")
                        if type(seq) is not int or seq <= last:
                            raise ValueError(f"invalid legacy sequence: {path}")
                        last = seq
                        if kind == "events":
                            insert_event(conn, row)
                        else:
                            conn.execute(
                                "INSERT INTO runs VALUES (?,?,?,?,?,?,?)",
                                (
                                    session,
                                    seq,
                                    row.get("event_id"),
                                    row.get("turn_id"),
                                    row.get("type"),
                                    row.get("task_id"),
                                    encode(row),
                                ),
                            )
                            conn.execute(
                                "UPDATE sessions SET last_run_seq=MAX(last_run_seq,?) WHERE id=?",
                                (seq, session),
                            )
                path = session_dir / "state.json"
                if path.exists():
                    state = read_json(path)
                    if (
                        state.get("user_id") != storage.user_id
                        or state.get("session_id") != session
                    ):
                        raise ValueError(f"legacy state owner mismatch: {path}")
                    conn.execute(
                        "UPDATE sessions SET state_json=?,state_revision=? WHERE id=?",
                        (encode(state), state.get("state_revision", 1), session),
                    )

        if not profile.get("legacy_chat_cleared"):
            turns = {}
            for index, message in enumerate(legacy.get("chat_history", [])):
                session = message.get("session_id") or "legacy"
                if message.get("role") == "user" or session not in turns:
                    turns[session] = f"legacy-turn-{index}"
                event_id = f"legacy-message-{index}"
                if conn.execute(
                    "SELECT 1 FROM events WHERE session_id=? AND event_id=?", (session, event_id)
                ).fetchone():
                    continue
                current = conn.execute(
                    "SELECT last_seq FROM sessions WHERE id=?", (session,)
                ).fetchone()
                insert_event(
                    conn,
                    {
                        "user_id": storage.user_id,
                        "session_id": session,
                        "seq": (current[0] if current else 0) + 1,
                        "event_id": event_id,
                        "turn_id": turns[session],
                        "scope": "session",
                        "type": "message",
                        "role": message.get("role", "assistant"),
                        "content": message.get("content", ""),
                        "timestamp": message.get("timestamp", ""),
                        "final": message.get("role") == "assistant",
                    },
                )

        trips = owned_json(directory / "trips.json", strict=True)
        if trips is None:
            trips = owned_json(old_directory / "trips.json", strict=False)
        source_name = "trips.json" if trips is not None else "legacy-user.json"
        if source_name not in history_state.get("imported_sources", []):
            records = (trips if trips is not None else legacy).get("trip_history", [])
            if not isinstance(records, list):
                raise ValueError("invalid legacy travel history")
            latest = {}
            for index, row in enumerate(records):
                if not isinstance(row, dict):
                    raise ValueError("invalid legacy travel record")
                key = history_identity(row, index)
                revision = row.get("workflow_revision", 0)
                revision = revision if type(revision) is int else 0
                if key not in latest or revision >= latest[key][1]:
                    latest[key] = (row, revision)
            for key, (row, _) in latest.items():
                if not conn.execute(
                    "SELECT 1 FROM trip_plans WHERE id=?", (row.get("workflow_id"),)
                ).fetchone():
                    conn.execute(
                        "INSERT OR IGNORE INTO travel_history VALUES (?,1,?,?)",
                        (key, datetime.now(UTC).isoformat(), encode(row)),
                    )

        max_revision = conn.execute(
            "SELECT COALESCE(MAX(state_revision),0) FROM sessions"
        ).fetchone()[0]
        set_metadata(
            conn, "state_clock", max(max_revision, int(metadata(conn, "state_clock", "0")))
        )
        verify_sources(sources)
        for path, (digest, diagnostics) in sources.items():
            conn.execute(
                "INSERT INTO migration_sources VALUES (?,?,?)",
                (str(path.resolve()), digest, encode({"diagnostics": diagnostics})),
            )
            for diagnostic in diagnostics:
                logging.getLogger(__name__).warning(diagnostic)
        set_metadata(conn, "migration_complete", "1")
